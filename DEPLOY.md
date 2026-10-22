# PanganCast Backend — Panduan Deploy

## Struktur File
```
PanganCast_flask/
├── app.py
├── core.py
├── requirements.txt
├── Procfile              ← untuk Railway / Heroku
├── gunicorn.conf.py      ← konfigurasi Gunicorn
└── models/               ← default folder model tersimpan
```

---

## Opsi 1: Railway (Rekomendasi, Gratis)

1. Buat project untuk backend dan tambahkan **Volume** dengan mount path `/data`.
2. Set environment variable berikut pada service backend:
   - `DATABASE_URL`: URL MySQL dari Aiven, misalnya `mysql://user:password@host:port/database?ssl-mode=REQUIRED`.
   - `MYSQL_SSL_CA`: path sertifikat CA Aiven yang tersedia di service (disarankan agar TLS memverifikasi sertifikat server).
   - `PANGANCAST_MODEL_DIR`: `/data/models` agar file model berada di volume persisten Railway.
3. Deploy service Flask dari GitHub. Pastikan volume terpasang pada service yang menjalankan Gunicorn.

Tabel MySQL akan dibuat otomatis ketika backend mulai. Buat database di Aiven terlebih dahulu dan pastikan URL/CA cocok dengan konfigurasi Aiven. Password dengan karakter khusus harus di-URL-encode.

File `Procfile` sudah disertakan:
```
web: gunicorn app:app -c gunicorn.conf.py
```

Setelah deploy, Railway beri URL seperti:
```
https://pangancast-backend.up.railway.app
```

Atur URL backend di Flutter:
```dart
const String kPrimaryBaseUrl = 'https://pangancast-backend.up.railway.app';
```

---

## Opsi 2: Render (Gratis)

1. Buat akun di https://render.com
2. New → Web Service → Connect GitHub repo
3. Settings:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app -c gunicorn.conf.py`
4. Deploy

---

## Opsi 3: VPS / Server Sendiri

```bash
# Install dependencies
pip install -r requirements.txt
pip install gunicorn

# Jalankan dengan Gunicorn (production)
gunicorn app:app -c gunicorn.conf.py

# Atau dengan systemd service (agar auto-start)
# Buat file: /etc/systemd/system/pangancast.service
```

Contoh systemd service:
```ini
[Unit]
Description=PanganCast Flask Backend
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/PanganCast_flask
ExecStart=/usr/bin/gunicorn app:app -c gunicorn.conf.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable pangancast
sudo systemctl start pangancast
```

---

## Opsi 4: Lokal (Development)

```bash
cd PanganCast_flask
pip install -r requirements.txt
# Atur DATABASE_URL ke database MySQL Aiven dan PANGANCAST_MODEL_DIR
# ke direktori yang persisten/tersedia untuk menyimpan file model.
python app.py
# Server berjalan di http://localhost:5000
```

Di PowerShell, contoh konfigurasi lokal:
```powershell
$env:DATABASE_URL = "mysql://user:password@host:port/database?ssl-mode=REQUIRED"
$env:MYSQL_SSL_CA = "C:\path\to\ca.pem"
$env:PANGANCAST_MODEL_DIR = "C:\PanganCast\models"
python app.py
```

---

## Catatan Penting

- Setiap model disimpan sebagai file `.joblib` dengan ID unik pada `PANGANCAST_MODEL_DIR`; metadata, nama, konfigurasi, dan tanggal training disimpan pada MySQL.
- Gunakan Railway Volume atau storage persisten lain untuk `PANGANCAST_MODEL_DIR`. Tanpa volume, file model dapat hilang saat service dibuat ulang.
- Semua backend (termasuk URL fallback Flutter) harus memakai database MySQL dan penyimpanan model yang sama agar failover tetap dapat menemukan file model.
- Data lama pada SQLite lokal tidak dipindahkan otomatis ke Aiven. Model file lama perlu disalin ke volume dan metadata lama dimigrasikan secara terpisah jika ingin tetap digunakan.
- Untuk production, tambahkan Nginx sebagai reverse proxy
- Status proses upload/training yang sedang berjalan masih berada di memori; selesaikan training sebelum restart/deploy. Model training yang sudah selesai tetap dapat dipilih dan dipakai ulang setelah restart.

---

## Test Endpoint Setelah Deploy

```bash
# Backend/API dapat dijangkau
curl https://YOUR_URL/health

# Memastikan aplikasi berjalan di Railway (menampilkan nama service/environment jika tersedia)
curl https://YOUR_URL/health/railway

# Menguji koneksi database MySQL Aiven
curl -i https://YOUR_URL/health/database

# Upload dataset
curl -X POST https://YOUR_URL/upload \
  -F "file=@dataset.csv" \
  -F "date_column=Tanggal" \
  -F "commodity_column=Harga"
```

Endpoint health dapat dipanggil bot Telegram dengan HTTP GET. `/health/railway`
memeriksa bahwa service backend merespons; `/health/database` benar-benar
menjalankan `SELECT 1` ke MySQL. Keduanya tidak mengembalikan kredensial. Respons
database memakai HTTP 200 ketika tersambung dan HTTP 503 ketika koneksi gagal.
