# mydownloader

Web downloader untuk YouTube, Spotify, SoundCloud, X, Facebook. Deploy gratis di Render.

## Endpoints

- `GET /` — Halaman web
- `GET /download?url=<URL>` — Download video/audio

## Deploy ke Render

1. Push ke GitHub
2. Buka https://dashboard.render.com/blueprints
3. Connect repo ini
4. Klik "Apply"

Atau manual:
1. Web Service baru → Public Git Repo
2. Environment: Docker
3. Free plan

## Lokal

```bash
uv run app.py
```
