# mydownloader

Web downloader untuk YouTube, Spotify, SoundCloud, X, Facebook. Deploy gratis di Render.

## Endpoints

- `GET /` — Halaman web
- `GET /robots.txt` — Arahan crawler (larang `/api/` & `/file/`)
- `GET /health` — Health check (active jobs, file count, cookies)
- `GET /api/download/start?url=<URL>&mode=<auto|video|audio>&format=<best|1080|720|480>` — Mulai job, return `job_id`
- `GET /api/download/stream/<job_id>` — SSE progress + result event
- `GET /api/metadata?url=<URL>` — Preview metadata (judul, thumbnail, durasi)
- `GET /file/<token>/<filename>` — Unduh hasil (token expired 1 jam, HMAC-signed)
- `POST /api/cookies` — Upload `cookies.txt` (untuk bypass YouTube bot check)

## Environment variables

Lihat `.env.example` untuk dokumentasi lengkap. Wajib di-set di Render:

- `SECRET_KEY` — HMAC secret untuk signed download URLs (generate via `python3 -c "import secrets; print(secrets.token_hex(32))"`).

Opsional (ada default):
- `FILE_TOKEN_TTL` (default 3600)
- `RATE_LIMIT_MAX` (default 10)
- `RATE_LIMIT_WINDOW` (default 60)
- `JOB_TTL` (default 1800)

## Deploy ke Render

1. Push ke GitHub
2. Buka https://dashboard.render.com/blueprints
3. Connect repo ini
4. Klik "Apply"

Atau manual:
1. Web Service baru → Public Git Repo
2. Environment: Docker
3. Free plan
4. Set env `SECRET_KEY` di tab Environment

## Lokal

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
SECRET_KEY=dev-secret .venv/bin/python app.py
# buka http://localhost:3000
```

## Test

```bash
.venv/bin/python -m pytest tests/ -v
```

## Catatan

- `/file/<token>/<name>` pakai HMAC token yang di-sign dengan `SECRET_KEY`. Kalau env tidak di-set, secret di-generate ulang tiap restart → link unduhan lama invalid (cukup re-klik dari riwayat).
- Rate limit: 10 request/menit per-IP untuk start job dan metadata (tweak via env).
- File hasil temporary di `/tmp/downloads`, hilang saat restart container.
