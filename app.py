import io
import json
import base64
import hashlib
import os
import threading
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager
from urllib.parse import parse_qs, unquote, urlparse

import joblib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pymysql
from pymysql.cursors import DictCursor
from flask import Flask, jsonify, request
from flask_cors import CORS

from core import (
    MODEL_DIR,
    MAX_LAG,
    clean_commodity_series,
    descriptive_analysis,
    make_time_series_features,
    parse_date_series,
    train_model,
)

app = Flask(__name__)
CORS(app)

# In-memory store: session_id -> data
_store: dict = {}
_lock = threading.Lock()
_PLACEHOLDER_COLUMNS = {"_dummy_", "dummy", ""}
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
ACTIVE_DATASET_PATH = os.path.join(DATA_DIR, "active_dataset.json")
os.makedirs(DATA_DIR, exist_ok=True)


class _DatabaseConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, query, parameters=()):
        cursor = self.connection.cursor()
        cursor.execute(query.replace("?", "%s"), parameters)
        return cursor


@contextmanager
def _db():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL MySQL belum dikonfigurasi.")
    parsed = urlparse(database_url)
    if parsed.scheme not in {"mysql", "mysql+pymysql"}:
        raise RuntimeError("DATABASE_URL harus menggunakan MySQL.")
    query = parse_qs(parsed.query)
    ssl_ca = os.environ.get("MYSQL_SSL_CA") or query.get("ssl_ca", [None])[0]
    ssl_options = {"ca": ssl_ca} if ssl_ca else {}
    connection = pymysql.connect(
        host=parsed.hostname,
        port=parsed.port or 3306,
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        database=unquote(parsed.path.lstrip("/")),
        charset="utf8mb4",
        cursorclass=DictCursor,
        connect_timeout=10,
        ssl=ssl_options,
    )
    try:
        yield _DatabaseConnection(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _init_database():
    with _db() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS training_sessions (
                id VARCHAR(36) PRIMARY KEY,
                model_name VARCHAR(255) NOT NULL,
                dataset_name VARCHAR(255) NOT NULL,
                dataset_hash VARCHAR(64) NOT NULL,
                parameters LONGTEXT NOT NULL,
                result LONGTEXT NOT NULL,
                created_at VARCHAR(40) NOT NULL,
                is_selected TINYINT(1) NOT NULL DEFAULT 0
            )
        """)
        columns = connection.execute("SHOW COLUMNS FROM training_sessions").fetchall()
        column_names = {column["Field"] for column in columns}
        if "model_name" not in column_names:
            connection.execute(
                "ALTER TABLE training_sessions ADD COLUMN model_name VARCHAR(255) "
                "NOT NULL DEFAULT 'Model tersimpan'"
            )
        if connection.execute(
            "SELECT 1 FROM training_sessions WHERE is_selected = 1 LIMIT 1"
        ).fetchone() is None:
            newest = connection.execute(
                "SELECT id FROM training_sessions ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if newest:
                connection.execute(
                    "UPDATE training_sessions SET is_selected = 1 WHERE id = ?",
                    (newest["id"],),
                )


_init_database()


def _save_active_dataset(df, filename, date_column, commodity_column):
    payload = {
        "df": df.to_json(date_format="iso"),
        "filename": filename,
        "date_column": date_column,
        "commodity_column": commodity_column,
    }
    temporary_path = f"{ACTIVE_DATASET_PATH}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as active_file:
        json.dump(payload, active_file)
    os.replace(temporary_path, ACTIVE_DATASET_PATH)
    return hashlib.sha256(payload["df"].encode("utf-8")).hexdigest()


def _load_session_result(session_id):
    with _db() as connection:
        row = connection.execute(
            "SELECT result FROM training_sessions WHERE id = ?", (session_id,)
        ).fetchone()
    return json.loads(row["result"]) if row else None


def _session_summary(row):
    result = json.loads(row["result"])
    model_path = result.get("model_path")
    return {
        "id": row["id"],
        "model_name": row["model_name"],
        "dataset_name": row["dataset_name"],
        "dataset_hash": row["dataset_hash"],
        "parameters": json.loads(row["parameters"]),
        "metrics": result.get("metrics", {}),
        "commodity_column": result.get("commodity_column"),
        "created_at": row["created_at"],
        "is_selected": bool(row["is_selected"]),
        "available": bool(model_path and os.path.isfile(model_path)),
    }


def _get_store(session_id: str) -> dict:
    with _lock:
        return _store.get(session_id, {})


def _set_store(session_id: str, data: dict):
    with _lock:
        _store[session_id] = data


def _resolve_upload_columns(df: pd.DataFrame, date_column: str, commodity_column: str):
    """Resolve placeholder column names sent by older clients."""
    if date_column not in _PLACEHOLDER_COLUMNS and commodity_column not in _PLACEHOLDER_COLUMNS:
        return date_column, commodity_column

    date_candidates = []
    for column in df.columns:
        parsed = parse_date_series(df[column])
        if parsed.notna().mean() >= 0.8:
            date_candidates.append(column)

    date_column = date_column if date_column not in _PLACEHOLDER_COLUMNS else (
        date_candidates[0] if date_candidates else None
    )

    commodity_candidates = []
    for column in df.columns:
        if column == date_column:
            continue
        cleaned = (
            df[column]
            .astype(str)
            .str.replace("Rp", "", regex=False)
            .str.replace(".", "", regex=False)
            .str.replace(",", ".", regex=False)
            .str.strip()
        )
        if pd.to_numeric(cleaned, errors="coerce").notna().mean() >= 0.8:
            commodity_candidates.append(column)

    commodity_column = commodity_column if commodity_column not in _PLACEHOLDER_COLUMNS else (
        commodity_candidates[0] if commodity_candidates else None
    )

    if not date_column or not commodity_column:
        raise ValueError(
            "Kolom tanggal dan harga tidak dapat dideteksi otomatis. "
            "Pilih kolom tanggal dan kolom harga dari dataset."
        )

    return date_column, commodity_column


def _render_lag_chart(df: pd.DataFrame, commodity_column: str) -> str:
    prices = clean_commodity_series(df, commodity_column).dropna()
    lag_data = pd.DataFrame({"y": prices, "lag_1": prices.shift(1)}).dropna()

    figure, axis = plt.subplots(figsize=(9, 5), dpi=120)
    if not lag_data.empty:
        x = lag_data["lag_1"]
        y = lag_data["y"]
        axis.scatter(x, y, alpha=0.45, color="#1976B9")
        if len(lag_data) >= 2 and x.nunique() > 1:
            coefficient = np.polyfit(x, y, 1)
            trend_x = np.linspace(x.min(), x.max(), 200)
            axis.plot(trend_x, np.polyval(coefficient, trend_x), color="#1976B9", linewidth=2)
    axis.set_xlabel("Harga t-1")
    axis.set_ylabel("Harga t")
    axis.grid(alpha=0.20)
    figure.tight_layout()
    output = io.BytesIO()
    figure.savefig(output, format="png", bbox_inches="tight")
    plt.close(figure)
    return base64.b64encode(output.getvalue()).decode("ascii")


# ======================================================================
# GET COLUMNS ONLY
# POST /columns
# Form-data: file (CSV/Excel)
# ======================================================================

@app.route("/columns", methods=["POST"])
def get_columns():
    if "file" not in request.files:
        return jsonify({"error": "File tidak ditemukan."}), 400
    file = request.files["file"]
    try:
        filename = file.filename.lower()
        if filename.endswith(".csv"):
            df = pd.read_csv(file)
        elif filename.endswith((".xlsx", ".xls")):
            df = pd.read_excel(file)
        else:
            return jsonify({"error": "Format tidak didukung."}), 400
        response = {
            "columns": list(df.columns),
            "preview": json.loads(df.head(10).to_json(orient="records", date_format="iso")),
            "preview_rows": len(df.head(10)),
            "total_rows": len(df),
        }
        for column in df.columns:
            parsed = parse_date_series(df[column])
            if parsed.notna().mean() >= 0.8:
                response["date_column"] = column
                values = parsed.dropna()
                response["date_range"] = {
                    "start": str(values.min().date()),
                    "end": str(values.max().date()),
                }
                break
        return jsonify(response)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ======================================================================
# UPLOAD DATASET
# POST /upload
# Form-data: file (CSV/Excel), date_column, commodity_column,
#            start_date (optional), end_date (optional)
# ======================================================================

@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "File tidak ditemukan."}), 400

    file = request.files["file"]
    date_column = request.form.get("date_column")
    commodity_column = request.form.get("commodity_column")

    if not date_column or not commodity_column:
        return jsonify({"error": "date_column dan commodity_column wajib diisi."}), 400

    try:
        filename = file.filename.lower()
        if filename.endswith(".csv"):
            df = pd.read_csv(file)
        elif filename.endswith((".xlsx", ".xls")):
            df = pd.read_excel(file)
        else:
            return jsonify({"error": "Format file tidak didukung. Gunakan CSV atau Excel."}), 400

        date_column, commodity_column = _resolve_upload_columns(
            df, date_column, commodity_column
        )

        if date_column not in df.columns:
            return jsonify({"error": f"Kolom '{date_column}' tidak ditemukan."}), 400
        if commodity_column not in df.columns:
            return jsonify({"error": f"Kolom '{commodity_column}' tidak ditemukan."}), 400

        df[date_column] = parse_date_series(df[date_column])
        df = df.dropna(subset=[date_column]).sort_values(date_column).reset_index(drop=True)

        start_date = request.form.get("start_date")
        end_date = request.form.get("end_date")

        if start_date:
            df = df[df[date_column].dt.date >= pd.Timestamp(start_date).date()]
        if end_date:
            df = df[df[date_column].dt.date <= pd.Timestamp(end_date).date()]

        df = df.reset_index(drop=True)
        lag_chart = _render_lag_chart(df, commodity_column)
        dataset_hash = _save_active_dataset(
            df, file.filename, date_column, commodity_column
        )

        session_id = str(uuid.uuid4())
        _set_store(session_id, {
            "df": df.to_json(date_format="iso"),
            "date_column": date_column,
            "commodity_column": commodity_column,
            "lag_chart_base64": lag_chart,
            "dataset_name": file.filename,
            "dataset_hash": dataset_hash,
        })

        return jsonify({
            "session_id": session_id,
            "rows": len(df),
            "columns": list(df.columns),
            "missing_values": int(df.isna().sum().sum()),
            "date_range": {
                "start": str(df[date_column].min().date()),
                "end": str(df[date_column].max().date()),
            },
            "lag_chart_base64": lag_chart,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ======================================================================
# DESCRIPTIVE ANALYSIS
# GET /descriptive?session_id=...
# ======================================================================

@app.route("/descriptive", methods=["GET"])
def descriptive():
    session_id = request.args.get("session_id")
    store = _get_store(session_id)

    if not store:
        return jsonify({"error": "Session tidak ditemukan. Silakan upload dataset terlebih dahulu."}), 404

    try:
        df = pd.read_json(io.StringIO(store["df"]))
        date_column = store["date_column"]
        commodity_column = store["commodity_column"]

        result = descriptive_analysis(df, date_column, commodity_column)
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ======================================================================
# TRAIN MODEL (background thread)
# POST /train
# JSON body:
#   session_id, split_method ("percentage"|"date"),
#   train_ratio (int, default 80), train_end_date (str, optional),
#   rf_profile ("Fast"|"Balanced"|"Thorough"), cv_splits (int)
# ======================================================================

def _run_training(session_id: str, model_id: str, model_name: str, df, date_column, commodity_column, kwargs):
    try:
        result = train_model(
            df=df,
            date_column=date_column,
            commodity_column=commodity_column,
            session_id=model_id,
            **kwargs,
        )
        store = _get_store(session_id)
        store["train_result"] = {k: v for k, v in result.items() if k != "model"}
        store["train_status"] = "done"
        result_for_db = store["train_result"].copy()
        result_for_db["model_path"] = os.path.abspath(result["model_path"])
        parameters = {
            "split_method": kwargs["split_method"],
            "train_ratio": kwargs["train_ratio"],
            "train_end_date": kwargs["train_end_date"],
            "rf_profile": kwargs["rf_profile"],
            "cv_splits": kwargs["cv_splits"],
        }
        created_at = datetime.now(timezone.utc).isoformat()
        with _db() as connection:
            connection.execute(
                "UPDATE training_sessions SET is_selected = 0"
            )
            connection.execute(
                "INSERT INTO training_sessions "
                "(id, model_name, dataset_name, dataset_hash, parameters, result, created_at, is_selected) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (
                model_id,
                model_name,
                store.get("dataset_name", "dataset aktif"),
                    store.get("dataset_hash", ""),
                    json.dumps(parameters, default=str),
                    json.dumps(result_for_db, default=str),
                    created_at,
                ),
            )
        store["model_session_id"] = model_id
        _set_store(session_id, store)
    except Exception as e:
        model_path = os.path.join(MODEL_DIR, f"{model_id}.joblib")
        if os.path.isfile(model_path):
            os.remove(model_path)
        store = _get_store(session_id)
        store["train_status"] = "error"
        store["train_error"] = str(e)
        _set_store(session_id, store)


@app.route("/train", methods=["POST"])
def train():
    body = request.get_json(force=True) or {}
    session_id = body.get("session_id")
    store = _get_store(session_id)

    if not store and os.path.isfile(ACTIVE_DATASET_PATH):
        with open(ACTIVE_DATASET_PATH, encoding="utf-8") as active_file:
            active = json.load(active_file)
        store = {
            "df": active["df"],
            "date_column": active["date_column"],
            "commodity_column": active["commodity_column"],
            "dataset_name": active["filename"],
            "dataset_hash": hashlib.sha256(
                active["df"].encode("utf-8")
            ).hexdigest(),
        }
        _set_store(session_id, store)

    if not store:
        return jsonify({"error": "Session tidak ditemukan. Silakan upload dataset terlebih dahulu."}), 404

    if store.get("train_status") == "running":
        return jsonify({"message": "Training sedang berjalan.", "status": "running"}), 202

    model_name = str(body.get("model_name", "")).strip()
    if not model_name or len(model_name) > 255:
        return jsonify({"error": "Nama model wajib diisi (maksimal 255 karakter)."}), 400

    try:
        df = pd.read_json(io.StringIO(store["df"]))
        date_column = store["date_column"]
        commodity_column = store["commodity_column"]

        store["train_status"] = "running"
        store["train_result"] = None
        store["train_error"] = None
        _set_store(session_id, store)

        kwargs = {
            "split_method": body.get("split_method", "percentage"),
            "train_ratio": int(body.get("train_ratio", 80)),
            "train_end_date": body.get("train_end_date"),
            "rf_profile": body.get("rf_profile", "Balanced"),
            "cv_splits": int(body.get("cv_splits", 5)),
        }
        model_id = str(uuid.uuid4())

        t = threading.Thread(
            target=_run_training,
            args=(session_id, model_id, model_name, df, date_column, commodity_column, kwargs),
            daemon=True,
        )
        t.start()

        return jsonify({"message": "Training dimulai.", "status": "running", "session_id": session_id}), 202

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ======================================================================
# TRAINING SESSIONS
# ======================================================================

@app.route("/sessions", methods=["GET"])
def sessions():
    with _db() as connection:
        rows = connection.execute(
            "SELECT * FROM training_sessions ORDER BY created_at DESC"
        ).fetchall()
    return jsonify({"sessions": [_session_summary(row) for row in rows]})


@app.route("/sessions/<session_id>", methods=["GET"])
def session_detail(session_id):
    with _db() as connection:
        row = connection.execute(
            "SELECT * FROM training_sessions WHERE id = ?", (session_id,)
        ).fetchone()
    if not row:
        return jsonify({"error": "Session training tidak ditemukan."}), 404
    return jsonify(_session_summary(row) | {"result": json.loads(row["result"])})


@app.route("/sessions/<session_id>/select", methods=["POST"])
def select_session(session_id):
    result = _load_session_result(session_id)
    if result is None:
        return jsonify({"error": "Session training tidak ditemukan."}), 404
    model_path = result.get("model_path")
    if not model_path or not os.path.isfile(model_path):
        return jsonify({"error": "File model tidak ditemukan di penyimpanan."}), 404
    with _db() as connection:
        connection.execute("UPDATE training_sessions SET is_selected = 0")
        connection.execute(
            "UPDATE training_sessions SET is_selected = 1 WHERE id = ?",
            (session_id,),
        )
    return jsonify({"status": "selected", "session_id": session_id, "result": result})


@app.route("/sessions/<session_id>", methods=["DELETE"])
def delete_session(session_id):
    model_path = None
    with _db() as connection:
        row = connection.execute(
            "SELECT result FROM training_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Session training tidak ditemukan."}), 404
        connection.execute("DELETE FROM training_sessions WHERE id = ?", (session_id,))
        model_path = json.loads(row["result"]).get("model_path")
        selected = connection.execute(
            "SELECT 1 FROM training_sessions WHERE is_selected = 1 LIMIT 1"
        ).fetchone()
        if not selected:
            newest = connection.execute(
                "SELECT id FROM training_sessions ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if newest:
                connection.execute(
                    "UPDATE training_sessions SET is_selected = 1 WHERE id = ?",
                    (newest["id"],),
                )
    with _lock:
        for upload_id, store in list(_store.items()):
            if store.get("model_session_id") == session_id:
                _store.pop(upload_id, None)
    if model_path and os.path.isfile(model_path):
        os.remove(model_path)
    return jsonify({"status": "deleted", "session_id": session_id})


@app.route("/sessions/<session_id>/rename", methods=["POST"])
def rename_session(session_id):
    body = request.get_json(force=True) or {}
    model_name = str(body.get("model_name", "")).strip()
    if not model_name or len(model_name) > 255:
        return jsonify({"error": "Nama model wajib diisi (maksimal 255 karakter)."}), 400
    with _db() as connection:
        existing = connection.execute(
            "SELECT id FROM training_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not existing:
            return jsonify({"error": "Model tidak ditemukan."}), 404
        connection.execute(
            "UPDATE training_sessions SET model_name = ? WHERE id = ?",
            (model_name, session_id),
        )
    return jsonify({"status": "renamed", "session_id": session_id, "model_name": model_name})


# ======================================================================
# TRAIN STATUS
# GET /train/status?session_id=...
# ======================================================================

@app.route("/train/status", methods=["GET"])
def train_status():
    session_id = request.args.get("session_id")
    store = _get_store(session_id)

    if not store:
        return jsonify({"error": "Session tidak ditemukan."}), 404

    status = store.get("train_status", "idle")

    if status == "done":
        return jsonify({"status": "done", "result": store.get("train_result")})
    elif status == "error":
        return jsonify({"status": "error", "error": store.get("train_error")}), 500
    else:
        return jsonify({"status": status})


# ======================================================================
# OUTPUT (hasil training yang sudah tersimpan)
# GET /output?session_id=...
# ======================================================================

@app.route("/output", methods=["GET"])
def output():
    session_id = request.args.get("session_id")
    store = _get_store(session_id)

    if not store:
        return jsonify({"error": "Session tidak ditemukan."}), 404

    status = store.get("train_status", "idle")
    if status != "done":
        return jsonify({"error": "Model belum selesai dilatih.", "status": status}), 404

    return jsonify(store.get("train_result"))


# ======================================================================
# PREDICT (pakai model tersimpan di disk)
# POST /predict
# JSON body: session_id, data (list of {date, price})
# ======================================================================

@app.route("/models/<session_id>/predict", methods=["POST"])
def predict(session_id):
    try:
        with _db() as connection:
            row = connection.execute(
                "SELECT result, model_name FROM training_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if not row:
            return jsonify({"error": "Model tidak ditemukan."}), 404
        model_path = json.loads(row["result"]).get("model_path")
        if not model_path or not os.path.isfile(model_path):
            return jsonify({"error": "File model tidak ditemukan di penyimpanan."}), 404
        if "file" not in request.files:
            return jsonify({"error": "File dataset prediksi wajib diunggah."}), 400
        file = request.files["file"]
        filename = (file.filename or "").lower()
        if filename.endswith(".csv"):
            df = pd.read_csv(file)
        elif filename.endswith((".xlsx", ".xls")):
            df = pd.read_excel(file)
        else:
            return jsonify({"error": "Format file tidak didukung. Gunakan CSV atau Excel."}), 400
        date_column = request.form.get("date_column")
        commodity_column = request.form.get("commodity_column")
        if date_column not in df.columns or commodity_column not in df.columns:
            return jsonify({"error": "Kolom tanggal atau harga tidak ditemukan."}), 400
        df[date_column] = parse_date_series(df[date_column])
        df[commodity_column] = clean_commodity_series(df, commodity_column)
        df = df.dropna(subset=[date_column, commodity_column]).sort_values(date_column)
        series = df.set_index(date_column)[commodity_column].astype(float)
        series = series[~series.index.duplicated(keep="last")]
        features = make_time_series_features(series, max_lag=MAX_LAG)
        X = features.drop(columns="target")
        if X.empty:
            return jsonify({
                "error": f"Data tidak cukup untuk membentuk fitur. Unggah minimal {MAX_LAG + 1} observasi valid."
            }), 400
        model = joblib.load(model_path)
        preds = model.predict(X)

        return jsonify({
            "model_id": session_id,
            "model_name": row["model_name"],
            "rows_used": len(X),
            "predictions": [
                {
                    "date": str(idx.date()),
                    "actual": float(features.loc[idx, "target"]),
                    "predicted": float(prediction),
                }
                for idx, prediction in zip(X.index, preds)
            ]
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ======================================================================
# HEALTH CHECK
# ======================================================================

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "PanganCast API",
        "platform": "railway" if os.environ.get("RAILWAY_SERVICE_NAME") else "unknown",
    })


@app.route("/health/railway", methods=["GET"])
def railway_health():
    service_name = os.environ.get("RAILWAY_SERVICE_NAME")
    environment_name = os.environ.get("RAILWAY_ENVIRONMENT_NAME")
    return jsonify({
        "status": "ok",
        "service": service_name or "PanganCast API",
        "platform": "railway" if service_name else "unknown",
        "environment": environment_name,
    })


@app.route("/health/database", methods=["GET"])
def database_health():
    try:
        with _db() as connection:
            result = connection.execute("SELECT 1 AS healthy").fetchone()
        if not result or result.get("healthy") != 1:
            raise RuntimeError("Database health query returned an unexpected result.")
        return jsonify({"status": "ok", "service": "mysql"})
    except Exception:
        app.logger.exception("MySQL health check failed")
        return jsonify({
            "status": "error",
            "service": "mysql",
            "message": "Koneksi database gagal. Periksa konfigurasi dan log backend.",
        }), 503


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
