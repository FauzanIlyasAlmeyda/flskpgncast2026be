# PanganCast Backend — Panduan Deploy

## Struktur File
```
PanganCast_flask/
├── app.py
├── core.py
├── requirements.txt
├── Procfile              ← untuk Railway / Heroku
├── gunicorn.conf.py      ← konfigurasi Gunicorn
├── Dockerfile
├── docker-compose.yml
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

## Opsi 3: VPS dengan Docker Compose

Backend dan MySQL dijalankan sebagai container terpisah. Data MySQL, file model,
dan dataset aktif memakai Docker named volume sehingga tetap ada setelah
container dibuat ulang.

1. Pasang Docker Engine dan Docker Compose plugin di VPS.
2. Salin folder `PanganCast_flask` ke VPS, lalu masuk ke folder tersebut.
3. Buat file `.env` dari template dan ganti kedua password dengan nilai acak
   yang panjang, berbeda, dan hanya memakai huruf/angka agar aman dimasukkan ke
   `DATABASE_URL`:

   ```bash
   cp .env.example .env
   nano .env
   ```

4. Jalankan layanan:

   ```bash
   docker compose up -d --build
   docker compose ps
   docker compose logs -f api
   ```

5. Verifikasi backend dan database dari VPS:

   ```bash
   curl http://127.0.0.1:8080/health
   curl -i http://127.0.0.1:8080/health/database
   ```

Port API hanya di-bind ke `127.0.0.1`, sedangkan MySQL tidak dipublikasikan
ke internet. Pasang Nginx atau Caddy di VPS sebagai reverse proxy HTTPS ke
`127.0.0.1:8080`, lalu gunakan URL HTTPS publik tersebut di aplikasi Flutter.
Jangan membuka port MySQL `3306` ke publik.

Volume yang digunakan:

- `mysql_data`: database MySQL dan tabel `training_sessions`.
- `model_data`: file model `.joblib`.
- `app_data`: dataset aktif yang disimpan backend.

Untuk melihat nama volume aktual: `docker volume ls`. Backup volume database dan
model secara berkala; jangan menghapus volume saat memperbarui container. Model
lama hanya bisa digunakan jika file `.joblib` beserta metadata barisnya pada
`training_sessions` ikut dipindahkan. Path file model tersimpan di metadata,
jadi pemindahan model ke direktori berbeda mungkin memerlukan pembaruan metadata.

Perintah operasional:

```bash
docker compose logs -f api
docker compose restart api
docker compose down
```

`docker compose down` mempertahankan volume. Hindari `docker compose down -v`
karena opsi `-v` menghapus database, model, dan dataset persisten.

## Opsi 4: VPS / Server Sendiri (tanpa Docker)

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

## Opsi 5: Lokal (Development)

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
