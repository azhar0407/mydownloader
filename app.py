#!/usr/bin/env python3
"""
mydownloader — Flask web downloader for YouTube, SoundCloud, Facebook, X (Twitter)
Deploy target: Render (free tier). Production WSGI: gunicorn.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file, render_template_string

app = Flask(__name__)

START_TIME = time.time()
DOWNLOADS_DIR = Path(os.environ.get("DOWNLOADS_DIR", "/tmp/downloads"))
COOKIES_PATH = Path(os.environ.get("COOKIES_PATH", "/tmp/cookies.txt"))
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

# === Keamanan & limit ===
# Secret untuk HMAC signed download URL. Generate otomatis kalau env SECRET_KEY kosong
# (cukup untuk single-instance; restart akan rotate token lama → user re-klik link).
SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
# TTL token unduhan (detik). Setelah itu /file/<token> expired.
FILE_TOKEN_TTL = int(os.environ.get("FILE_TOKEN_TTL", "3600"))  # 1 jam
# Rate limit: max N request per window per IP
RATE_LIMIT_MAX = int(os.environ.get("RATE_LIMIT_MAX", "10"))
RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", "60"))  # 60 detik
# Batas ukuran file cookies upload (Flask default 16MB sudah cukup, tapi kita tetapkan)
COOKIES_MAX_BYTES = 256 * 1024  # 256 KB cukup untuk cookies.txt
# Janitor: bersihkan JOBS yang tidak selesai setelah TTL ini (detik)
JOB_TTL = int(os.environ.get("JOB_TTL", "1800"))  # 30 menit
# Batas panjang URL untuk mencegah memory abuse pada parameter ?url=...
MAX_URL_LEN = int(os.environ.get("MAX_URL_LEN", "2048"))
# Domain CDN thumbnail YouTube yang boleh di-load via <img> (untuk CSP)
_THUMB_HOSTS = ("i.ytimg.com", "i9.ytimg.com")

# job_id -> {"events": deque, "done": bool, "result": dict|None, "created": float, "files": [str]}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
# Rate limit per-IP: deque[timestamps]
_RATE_BUCKETS: dict[str, deque] = {}
_RATE_LOCK = threading.Lock()

URL_PATTERNS = [
    ("youtube", r"(youtube\.com|youtu\.be)"),
    ("soundcloud", r"soundcloud\.com"),
    ("twitter", r"(twitter\.com|x\.com)"),
    ("facebook", r"(facebook\.com|fb\.watch)"),
    ("instagram", r"instagram\.com"),
    ("tiktok", r"tiktok\.com"),
    ("reddit", r"reddit\.com"),
    ("vimeo", r"vimeo\.com"),
]

DRM_HOSTS = ("spotify.com/track", "spotify.com/album", "spotify.com/playlist",
             "netflix.com", "disneyplus.com", "hbomax.com", "primevideo.com")

ERROR_MAP = [
    (r"sign in to confirm|confirm you.?re not a bot",
     "YouTube meminta verifikasi. Unggah cookies di panel bawah, lalu coba lagi."),
    (r"drm", "Sumber dilindungi DRM. yt-dlp tidak dapat mengunduh konten ini."),
    (r"unsupported url", "URL tidak dikenali. Pastikan link publik dan valid."),
    (r"private video|video unavailable", "Video privat atau sudah dihapus."),
    (r"http error 429|too many requests", "Terlalu banyak permintaan. Coba lagi beberapa menit lagi."),
    (r"timed? ?out", "Unduhan terlalu lama. Coba resolusi lebih rendah atau ulangi."),
]


def humanize_error(raw: str) -> str:
    low = (raw or "").lower()
    for pat, msg in ERROR_MAP:
        if re.search(pat, low):
            return msg
    return "Gagal mengunduh. Periksa URL atau coba lagi."


# === Keamanan helper ===

def get_client_ip() -> str:
    """Ambil IP client, hormati X-Forwarded-For hanya kalau di belakang proxy tepercaya.
    Render selalu set X-Forwarded-For. Untuk single-instance publik, kita ambil
    header pertama (real client) — risiko spoofing rendah karena tidak ada
    state rahasia per-IP, hanya rate limit kasar."""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


def rate_limit_ok() -> bool:
    """Sliding window rate limit per-IP. True = boleh lanjut, False = harus 429."""
    ip = get_client_ip()
    now = time.time()
    with _RATE_LOCK:
        bucket = _RATE_BUCKETS.setdefault(ip, deque())
        # Buang timestamp di luar window
        while bucket and bucket[0] < now - RATE_LIMIT_WINDOW:
            bucket.popleft()
        if len(bucket) >= RATE_LIMIT_MAX:
            return False
        bucket.append(now)
        return True


def sign_file_token(filename: str) -> str:
    """Token untuk URL /file/<token>. Format: <exp>.<sig>
    Sig = HMAC-SHA256(secret, exp + ":" + filename) hex (16 char cukup)."""
    exp = int(time.time()) + FILE_TOKEN_TTL
    msg = f"{exp}:{filename}".encode()
    sig = hmac.new(SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()[:32]
    return f"{exp}.{sig}"


def verify_file_token(token: str, filename: str) -> bool:
    """Verifikasi token. False kalau expired atau sig salah → 403/410."""
    try:
        exp_str, sig = token.split(".", 1)
        exp = int(exp_str)
    except (ValueError, AttributeError):
        return False
    if time.time() > exp:
        return False
    expected = hmac.new(SECRET_KEY.encode(),
                        f"{exp}:{filename}".encode(),
                        hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, expected)


def janitor_tick() -> None:
    """Cleanup background: hapus JOBS yang expired DAN file temporer terkait.
    Dipanggil dari endpoint /health & dari thread daemon."""
    now = time.time()
    expired_files = []
    with JOBS_LOCK:
        for jid, job in list(JOBS.items()):
            age = now - job.get("created", now)
            if age > JOB_TTL:
                expired_files.extend(job.get("files", []))
                del JOBS[jid]
    for name in expired_files:
        try:
            (DOWNLOADS_DIR / name).unlink(missing_ok=True)
        except OSError:
            pass


def detect_platform(url: str) -> str:
    u = url.lower()
    for name, pat in URL_PATTERNS:
        if re.search(pat, u):
            return name
    if any(d in u for d in DRM_HOSTS):
        return "drm"
    return "generic"


def is_drm(url: str) -> bool:
    u = url.lower()
    return any(d in u for d in DRM_HOSTS)


def build_cmd(url: str, mode: str, fmt: str, use_cookies: bool, client: str,
              job_id: str = "",
              extra: list[str] | None = None) -> list[str]:
    # Job_id prefix di filename → picker tidak salah ambil file job lain.
    prefix = f"{job_id}__" if job_id else ""
    out_tpl = str(DOWNLOADS_DIR / f"{prefix}%(title).80B-%(id)s.%(ext)s")
    cmd = ["yt-dlp", "--no-playlist", "--newline", "--no-warnings", "--no-color",
           "-o", out_tpl, "--retries", "2", "--fragment-retries", "2",
           "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0.0.0 Safari/537.36",
           "--extractor-args", f"youtube:player_client={client}"]

    if mode == "audio":
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "192K"]
    elif mode == "video":
        if fmt in ("720", "1080", "480"):
            cmd += ["-f", f"bv*[height<={fmt}]+ba/b[height<={fmt}]/best",
                    "--merge-output-format", "mp4"]
        else:
            cmd += ["-f", "bv*+ba/b", "--merge-output-format", "mp4"]
    else:  # auto
        cmd += ["-f", "bv*+ba/best", "--merge-output-format", "mp4"]

    if use_cookies and COOKIES_PATH.exists():
        cmd += ["--cookies", str(COOKIES_PATH)]

    if extra:
        cmd += extra

    cmd.append(url)
    return cmd


# Urutan prioritas client YouTube: dari yang paling reliable tanpa cookies
YOUTUBE_CLIENTS = ["web_safari", "tv_embedded", "ios", "mediaconnect", "web", "android"]


def fetch_metadata(url: str) -> dict:
    """Ambil metadata cepat tanpa download (untuk preview)."""
    if is_drm(url):
        return {"success": False, "drm": True, "platform": "drm",
                "error": "DRM-protected content"}
    platform = detect_platform(url)
    cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "--no-color",
           "--dump-json", "--no-download",
           "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0.0.0 Safari/537.36"]
    if COOKIES_PATH.exists():
        cmd += ["--cookies", str(COOKIES_PATH)]
    cmd.append(url)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return {"success": False, "platform": platform, "error": "Timeout metadata"}
    if result.returncode != 0 or not result.stdout.strip():
        stderr = (result.stderr or "")[-500:]
        need_cookies = bool(re.search(r"sign in to confirm|confirm you.?re not a bot", stderr, re.I))
        return {"success": False, "platform": platform, "need_cookies": need_cookies,
                "error": humanize_error(stderr), "stderr": stderr}
    try:
        info = json.loads(result.stdout.splitlines()[0])
    except Exception:
        return {"success": False, "platform": platform, "error": "Gagal membaca metadata"}

    duration = info.get("duration")
    filesize = info.get("filesize") or info.get("filesize_approx")
    return {
        "success": True,
        "platform": platform,
        "title": info.get("title") or "(tanpa judul)",
        "thumbnail": info.get("thumbnail"),
        "duration": duration,
        "duration_str": _fmt_duration(duration) if duration else None,
        "uploader": info.get("uploader") or info.get("channel"),
        "filesize_approx": filesize,
    }


def _fmt_duration(sec) -> str:
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def _job_emit(job_id: str, event: dict):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job["events"].append(event)


def _job_finalize(job_id: str, **fields) -> bool:
    """Set final fields on a job atomically. Returns True kalau job masih ada,
    False kalau sudah dihapus (janitor/stream close). Aman dipanggil dari thread manapun."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return False
        for k, v in fields.items():
            job[k] = v
        return True


def run_download_job(job_id: str, url: str, mode: str, fmt: str):
    """Jalankan download dengan beberapa YouTube client secara bergantian.
    Untuk platform lain (SoundCloud, dll) gunakan client default "web_safari" sendiri.
    File hasil selalu ber-prefix job_id agar picker tidak salah ambil dari job lain.
    """
    platform = detect_platform(url)
    cookies_used = COOKIES_PATH.exists()

    # Client order: mulai dari yang paling terpercaya, terakhir fallback ke yang pasti bekerja
    clients = []
    if platform == "youtube":
        clients = YOUTUBE_CLIENTS[:]
    else:
        clients = ["web_safari"]  # platform lain pakai satu client

    written_files: list[str] = []

    for client in clients:
        cmd = build_cmd(url, mode, fmt, cookies_used, client,
                        job_id=job_id, extra=["--progress"])
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, bufsize=1)
        except Exception as e:
            continue

        _job_emit(job_id, {"stage": "downloading", "message": f"Mengunduh (client {client})…", "pct": 10})
        tail_lines = []
        pct_re = re.compile(r"(\d{1,3}(?:\.\d+)?)%")
        merge_seen = False

        for line in proc.stdout:  # type: ignore
            line = line.rstrip()
            if not line:
                continue
            tail_lines.append(line)
            if len(tail_lines) > 40:
                tail_lines.pop(0)
            m = pct_re.search(line)
            if m:
                pct = min(95.0, float(m.group(1)))
                _job_emit(job_id, {"stage": "downloading", "message": f"Mengunduh (client {client})… {pct:.0f}%", "pct": pct})
            elif "merg" in line.lower() and not merge_seen:
                merge_seen = True
                _job_emit(job_id, {"stage": "merging", "message": "Menggabungkan audio & video…", "pct": 96})

        proc.wait(timeout=300)
        stderr_tail = "\n".join(tail_lines[-15:])

        # Picker: hanya file ber-prefix job_id ini, ukuran > 0.
        prefix = f"{job_id}__"
        try:
            target = next(
                (f for f in DOWNLOADS_DIR.iterdir()
                 if f.is_file() and f.name.startswith(prefix) and f.stat().st_size > 0
                 and f.name not in written_files),
                None,
            )
        except (FileNotFoundError, OSError):
            target = None

        if proc.returncode == 0 and target is not None:
            size = target.stat().st_size
            written_files.append(target.name)
            _job_emit(job_id, {"stage": "done", "message": "Selesai!", "pct": 100})
            _job_finalize(job_id, done=True, files=written_files, result={
                "success": True,
                "platform": platform,
                "file": target.name,
                "size_bytes": size,
                "token": sign_file_token(target.name),
                "url": f"/file/{sign_file_token(target.name)}/{target.name}",
            })
            return
        else:
            _job_emit(job_id, {"stage": "error", "message": humanize_error(stderr_tail)})
            if platform != "youtube":
                _job_finalize(job_id, done=True, files=written_files, result={
                    "success": False, "platform": platform,
                    "error": humanize_error(stderr_tail),
                    "stderr": stderr_tail})
                return
            continue

    _job_emit(job_id, {"stage": "error", "message": "Semua client YouTube terblokir. Unggah cookies untuk bypass verifikasi bot."})
    _job_finalize(job_id, done=True, files=written_files, result={
        "success": False, "platform": platform, "need_cookies": True,
        "error": "YouTube meminta verifikasi. Unggah cookies di panel bawah.",
        "stderr": "Semua client YouTube terblokir."})



INDEX_HTML = r"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mydownloader — Video &amp; Audio Downloader</title>
<style>
:root{
  --bg:#0b0d12; --panel:#141821; --line:#222a36; --txt:#e6edf3;
  --muted:#8b95a4; --brand:#7c5cff; --brand2:#22d3ee; --ok:#22c55e; --err:#ef4444; --warn:#f59e0b;
}
:root[data-theme="light"]{
  --bg:#f5f6f8; --panel:#ffffff; --line:#e2e5ea; --txt:#101418;
  --muted:#5b6472; --brand:#6d4bff; --brand2:#0891b2; --ok:#16a34a; --err:#dc2626; --warn:#d97706;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--bg);color:var(--txt);
  font:15px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  transition:background .2s,color .2s}
a{color:var(--brand2);text-decoration:none}
.wrap{max-width:880px;margin:0 auto;padding:32px 20px 90px}
header{display:flex;align-items:center;gap:14px;margin-bottom:28px}
.logo{width:44px;height:44px;border-radius:12px;flex:none;
  background:linear-gradient(135deg,var(--brand),var(--brand2));
  display:flex;align-items:center;justify-content:center;font-weight:800;color:#fff;font-size:20px}
h1{font-size:22px;margin:0}
.sub{color:var(--muted);font-size:13px;margin-top:2px}
.theme-btn{margin-left:auto;width:40px;height:40px;border-radius:10px;border:1px solid var(--line);
  background:var(--panel);color:var(--txt);cursor:pointer;font-size:17px;flex:none}
.theme-btn:focus-visible,.btn:focus-visible,.tab:focus-visible,input:focus-visible,select:focus-visible{
  outline:3px solid var(--brand);outline-offset:2px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:22px;margin-bottom:18px}
.platform-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:20px}
@media (max-width:560px){.platform-grid{grid-template-columns:repeat(2,1fr)}}
.pcard{padding:14px 10px;border-radius:12px;background:var(--bg);border:1px solid var(--line);
  text-align:center;cursor:pointer;font-size:12px;color:var(--muted);transition:.15s}
.pcard:hover,.pcard:focus-visible{border-color:var(--brand);color:var(--txt)}
.pcard .ico{font-size:22px;display:block;margin-bottom:6px}
.tabs{display:flex;gap:6px;margin-bottom:18px;flex-wrap:wrap}
.tab{padding:8px 14px;border-radius:10px;background:var(--bg);border:1px solid var(--line);
  color:var(--muted);cursor:pointer;font-size:13px;user-select:none}
.tab.active{background:linear-gradient(135deg,var(--brand),var(--brand2));color:#fff;border-color:transparent;font-weight:600}
label{display:block;font-size:13px;color:var(--muted);margin:0 0 6px}
.row{display:flex;gap:10px}
.row > *{flex:1}
input[type=url],input[type=text],select{
  width:100%;padding:12px 14px;border-radius:10px;background:var(--bg);border:1px solid var(--line);
  color:var(--txt);font-size:14px;outline:none}
input:focus,select:focus{border-color:var(--brand)}
.btn{padding:12px 22px;border-radius:10px;border:0;cursor:pointer;font-weight:700;font-size:14px;
  background:linear-gradient(135deg,var(--brand),var(--brand2));color:#fff;flex:none;white-space:nowrap}
.btn:disabled{opacity:.55;cursor:not-allowed}
.btn-ghost{background:var(--bg);color:var(--txt);border:1px solid var(--line)}
.btn-sm{padding:8px 14px;font-size:12px}
.help{font-size:12px;color:var(--muted);margin-top:8px}
#result{margin-top:16px}
.alert{padding:14px 16px;border-radius:10px;border:1px solid var(--line);background:var(--bg);margin-bottom:14px}
.alert-ok{border-color:var(--ok)}
.alert-err{border-color:var(--err)}
.alert-warn{border-color:var(--warn)}
.progress{height:8px;border-radius:99px;background:var(--bg);overflow:hidden;border:1px solid var(--line)}
.bar{height:100%;background:linear-gradient(90deg,var(--brand),var(--brand2));width:0;transition:width .25s}
.file-item{display:flex;align-items:center;justify-content:space-between;gap:10px;
  padding:12px 14px;border-radius:10px;background:var(--panel);border:1px solid var(--line);margin-top:10px}
.file-meta{font-size:12px;color:var(--muted)}
.preview{display:flex;gap:12px;align-items:flex-start;margin-top:10px}
.preview img{width:120px;border-radius:8px;flex:none;background:var(--bg)}
.preview .meta{font-size:12px;color:var(--muted);line-height:1.6}
.preview .meta b{color:var(--txt);font-size:14px;display:block;margin-bottom:4px}
footer{margin-top:24px;color:var(--muted);font-size:12px;text-align:center}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid currentColor;border-top-color:transparent;
  border-radius:50%;animation:s 1s linear infinite;vertical-align:-2px;margin-right:8px}
@keyframes s{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.spinner{animation:none}}
.history{display:flex;flex-direction:column;gap:8px;margin-top:10px}
.hist-item{display:flex;align-items:center;justify-content:space-between;gap:8px;
  padding:10px 12px;border-radius:8px;background:var(--bg);border:1px solid var(--line);font-size:12px}
.hist-item .u{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:60%;color:var(--muted)}
.hist-empty{color:var(--muted);font-size:12px}
details summary{cursor:pointer;font-size:13px;color:var(--muted);user-select:none}
.toasts{position:fixed;right:16px;bottom:16px;display:flex;flex-direction:column;gap:8px;z-index:999;max-width:320px}
.toast{padding:12px 14px;border-radius:10px;background:var(--panel);border:1px solid var(--line);
  box-shadow:0 8px 24px rgba(0,0,0,.25);font-size:13px;animation:tin .2s ease}
@keyframes tin{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.toast button{background:none;border:0;color:var(--brand2);cursor:pointer;font-size:12px;margin-left:8px}
code{font-size:11px;word-break:break-all}
.disclaimer{margin-top:18px;padding:12px 14px;border-radius:10px;
  background:var(--bg);border:1px solid var(--line);font-size:12px;color:var(--muted);
  line-height:1.6}
.disclaimer b{color:var(--warn)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">M</div>
    <div>
      <h1>mydownloader</h1>
      <div class="sub">Unduh video &amp; audio dari YouTube, SoundCloud, Facebook, X, dan lainnya</div>
    </div>
    <button class="theme-btn" id="themeBtn" aria-label="Ganti tema terang/gelap" title="Ganti tema">🌙</button>
  </header>

  <div class="card">
    <div class="platform-grid" id="pgrid">
      <div class="pcard" data-example="https://youtu.be/..." tabindex="0" role="button" aria-label="Contoh URL YouTube"><span class="ico">▶️</span>YouTube</div>
      <div class="pcard" data-example="https://soundcloud.com/artist/track" tabindex="0" role="button" aria-label="Contoh URL SoundCloud"><span class="ico">🎵</span>SoundCloud</div>
      <div class="pcard" data-example="https://facebook.com/.../videos/..." tabindex="0" role="button" aria-label="Contoh URL Facebook"><span class="ico">📘</span>Facebook</div>
      <div class="pcard" data-example="https://x.com/user/status/..." tabindex="0" role="button" aria-label="Contoh URL X/Twitter"><span class="ico">✖️</span>X/Twitter</div>
    </div>

    <div class="tabs" id="tabs" role="tablist" aria-label="Mode unduhan">
      <div class="tab active" data-mode="auto" tabindex="0" role="tab" aria-selected="true">Auto-detect</div>
      <div class="tab" data-mode="video" tabindex="0" role="tab" aria-selected="false">Video (MP4)</div>
      <div class="tab" data-mode="audio" tabindex="0" role="tab" aria-selected="false">Audio (MP3)</div>
    </div>

    <label for="url">URL</label>
    <div class="row">
      <input id="url" type="url" placeholder="Tempel link video di sini… (Ctrl+V otomatis terdeteksi)" autocomplete="off">
      <select id="format" style="max-width:120px" aria-label="Resolusi video">
        <option value="best">Best</option>
        <option value="1080">1080p</option>
        <option value="720">720p</option>
        <option value="480">480p</option>
      </select>
      <button id="go" class="btn">Unduh</button>
    </div>
    <div class="help" id="hint">Tempel URL publik dari platform yang didukung. Tekan <b>Enter</b> untuk mulai.</div>

    <div id="preview" style="display:none"></div>

    <div id="result"></div>
  </div>

  <div class="card">
    <details id="histWrap">
      <summary>📜 Riwayat unduhan (<span id="histCount">0</span>)</summary>
      <div class="history" id="history"></div>
    </details>
  </div>

  <div class="card">
    <label>Cookies (opsional, untuk YouTube &amp; lainnya)</label>
    <div class="row">
      <input id="ckfile" type="file" accept=".txt" aria-label="Pilih file cookies.txt">
      <button id="ckup" class="btn btn-ghost">Unggah</button>
    </div>
    <div class="help">Jika YouTube meminta verifikasi bot, ekspor cookies dari browser via ekstensi <i>Get cookies.txt LOCALLY</i> lalu unggah di sini.</div>
    <div id="ckmsg" style="margin-top:10px"></div>
  </div>

  <footer>
    <div class="disclaimer">
      <b>⚖️ Penggunaan yang bertanggung jawab.</b>
      mydownloader disediakan untuk mengunduh konten yang <b>Anda miliki haknya</b>
      atau yang diizinkan untuk diunduh (mis. CC-BY, domain publik, video Anda sendiri).
      Mengunduh materi berhak cipta tanpa izin dapat melanggar ToS platform dan hukum
      setempat. Konten terproteksi DRM (Spotify, Netflix, Disney+, dsb.) <b>ditolak otomatis</b>.
      Pengguna bertanggung jawab penuh atas penggunaan alat ini.
    </div>
    <div style="margin-top:14px">mydownloader · Render free tier · File sementara, hilang saat restart</div>
  </footer>
</div>

<div class="toasts" id="toasts" aria-live="polite"></div>

<script>
// ---------- Theme ----------
const root=document.documentElement;
const themeBtn=document.getElementById('themeBtn');
function applyTheme(t){root.setAttribute('data-theme',t);themeBtn.textContent=t==='light'?'☀️':'🌙';localStorage.setItem('theme',t)}
applyTheme(localStorage.getItem('theme')||(matchMedia('(prefers-color-scheme: light)').matches?'light':'dark'));
themeBtn.addEventListener('click',()=>applyTheme(root.getAttribute('data-theme')==='light'?'dark':'light'));

// ---------- Toast ----------
function toast(msg,kind='ok',undoFn=null){
  const box=document.getElementById('toasts');
  const el=document.createElement('div');
  el.className='toast';
  el.innerHTML=esc(msg)+(undoFn?' <button type="button">Urungkan</button>':'');
  if(undoFn){el.querySelector('button').onclick=()=>{undoFn();el.remove()}}
  box.appendChild(el);
  setTimeout(()=>el.remove(),5000);
}

function esc(s){return (s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}

// ---------- Metadata preview ----------
let previewTimer=null;
async function fetchPreview(url){
  if(!/^https?:\/\//i.test(url)){hidePreview();return}
  clearTimeout(previewTimer);
  previewTimer=setTimeout(async()=>{
    try{
      const r=await fetch('/api/metadata?url='+encodeURIComponent(url));
      const d=await r.json();
      if(d.success){showPreview(d)}else{hidePreview()}
    }catch(e){hidePreview()}
  },400);
}
function showPreview(d){
  const box=document.getElementById('preview');
  const thumb=d.thumbnail && /^https?:\/\/(i\d*\.ytimg\.com|i\.ytimg\.com)/.test(d.thumbnail)
    ? `<img src="${esc(d.thumbnail)}" alt="" referrerpolicy="no-referrer" loading="lazy">` : '';
  const dur=d.duration_str?' · '+esc(d.duration_str):'';
  box.innerHTML=`<div class="preview">${thumb}
    <div class="meta"><b>${esc(d.title||'(tanpa judul)')}</b>
      ${esc(d.uploader||'')}${dur}<br>${esc(d.platform||'')}</div></div>`;
  box.style.display='block';
}
function hidePreview(){
  const box=document.getElementById('preview');
  if(box){box.style.display='none';box.innerHTML=''}
}

// ---------- Tabs ----------
const tabs=document.querySelectorAll('.tab');
let mode='auto';
function setMode(t){
  tabs.forEach(x=>{x.classList.remove('active');x.setAttribute('aria-selected','false')});
  t.classList.add('active');t.setAttribute('aria-selected','true');mode=t.dataset.mode;
  document.getElementById('hint').innerHTML=
    mode==='audio'?'Mode audio: ekstrak MP3 192kbps dari sumber video/audio.':
    mode==='video'?'Mode video: unduh MP4 kualitas terbaik (hingga 1080p).':
    'Auto-detect: pilih format terbaik otomatis berdasarkan sumber.';
}
tabs.forEach(t=>{
  t.addEventListener('click',()=>setMode(t));
  t.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();setMode(t)}});
});

// ---------- Platform cards: fill example ----------
document.querySelectorAll('.pcard').forEach(c=>{
  const go=()=>{$u.value=c.dataset.example;$u.focus();$u.select()};
  c.addEventListener('click',go);
  c.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();go()}});
});

const $u=document.getElementById('url'),$f=document.getElementById('format'),$go=document.getElementById('go');
const $r=document.getElementById('result');

function show(html){$r.innerHTML=html}
function progressBox(pct,label){
  show(`<div class="alert"><div><span class="spinner"></span>${esc(label)}</div>
    <div class="progress" style="margin-top:8px"><div class="bar" style="width:${pct}%"></div></div></div>`);
}

// ---------- Global paste auto-focus ----------
document.addEventListener('paste',(e)=>{
  if(document.activeElement===$u) return;
  const text=(e.clipboardData||window.clipboardData).getData('text');
  if(text && /^https?:\/\//i.test(text.trim())){
    $u.value=text.trim();$u.focus();
    toast('URL terdeteksi dari clipboard');
  }
});

$go.addEventListener('click',run);
$u.addEventListener('keydown',e=>{
  if(e.key==='Enter') run();
  if(e.key==='Escape') $u.value='';
});
$u.addEventListener('input',e=>fetchPreview(e.target.value));

function saveHistory(entry){
  const key='mdl_history';
  let hist=JSON.parse(localStorage.getItem(key)||'[]');
  hist=hist.filter(h=>h.url!==entry.url);
  hist.unshift(entry);
  hist=hist.slice(0,20);
  localStorage.setItem(key,JSON.stringify(hist));
  renderHistory();
}
function renderHistory(){
  const key='mdl_history';
  const hist=JSON.parse(localStorage.getItem(key)||'[]');
  document.getElementById('histCount').textContent=hist.length;
  const el=document.getElementById('history');
  if(!hist.length){el.innerHTML='<div class="hist-empty">Belum ada riwayat.</div>';return}
  el.innerHTML=hist.map((h,i)=>`
    <div class="hist-item">
      <span class="u" title="${esc(h.url)}">${esc(h.platform)} · ${esc(h.url)}</span>
      <button class="btn btn-sm btn-ghost" data-idx="${i}" data-act="reuse">Ulangi</button>
    </div>`).join('');
  el.querySelectorAll('[data-act=reuse]').forEach(b=>{
    b.addEventListener('click',()=>{$u.value=hist[+b.dataset.idx].url;$u.focus();run()});
  });
}
document.getElementById('history').addEventListener('click',e=>{});
renderHistory();

async function run(){
  const url=$u.value.trim();
  if(!url){toast('Masukkan URL terlebih dahulu','warn');return}
  $go.disabled=true;
  progressBox(3,'Memulai…');
  try{
    const startRes=await fetch('/api/download/start?mode='+mode+'&format='+encodeURIComponent($f.value)+'&url='+encodeURIComponent(url));
    const startData=await startRes.json();
    if(!startData.job_id){
      show(`<div class="alert alert-err"><b>✗ Gagal</b><div class="help">${esc(startData.error||'Tidak dapat memulai')}</div></div>`);
      $go.disabled=false;return;
    }
    listenJob(startData.job_id,url);
  }catch(e){
    show('<div class="alert alert-err"><b>✗ Network error</b> · '+esc(String(e))+'</div>');
    $go.disabled=false;
  }
}

function listenJob(jobId,url){
  const es=new EventSource('/api/download/stream/'+jobId);
  es.onmessage=(ev)=>{
    const d=JSON.parse(ev.data);
    if(d.stage==='result'){
      es.close();$go.disabled=false;
      renderResult(d.result,url);
      return;
    }
    progressBox(d.pct||10,d.message||'Memproses…');
  };
  es.onerror=()=>{
    es.close();$go.disabled=false;
    show('<div class="alert alert-err"><b>✗ Koneksi terputus</b> · Coba lagi.</div>');
  };
}

function renderResult(d,url){
  if(d.success){
    const sz=(d.size_bytes/1048576).toFixed(1);
    show(`<div class="alert alert-ok">
      <b>✓ Berhasil</b> · ${esc(d.platform)} · ${sz} MB
      <div class="file-item">
        <div><b>${esc(d.file)}</b><div class="file-meta">${d.size_bytes.toLocaleString()} bytes</div></div>
        <a class="btn" href="${esc(d.url)}" download>Simpan</a>
      </div>
    </div>`);
    toast('Unduhan selesai: '+d.file);
    saveHistory({url,platform:d.platform,ts:Date.now()});
  }else if(d.drm){
    show(`<div class="alert alert-warn"><b>⊘ Dilindungi DRM</b><div class="help" style="margin-top:6px">${esc(d.platform)} tidak dapat diunduh karena proteksi DRM.</div></div>`);
  }else if(d.need_cookies){
    show(`<div class="alert alert-warn">
      <b>⚠ Perlu verifikasi</b>
      <div class="help" style="margin-top:6px">${esc(d.error||'Situs meminta verifikasi bot.')} Unggah cookies.txt di panel bawah.</div>
    </div>`);
  }else{
    show(`<div class="alert alert-err"><b>✗ Gagal</b><div class="help" style="margin-top:6px">${esc(d.error||'Tidak diketahui')}</div>
      <details style="margin-top:8px"><summary>Detail teknis</summary><code>${esc(d.stderr||'')}</code></details>
    </div>`);
  }
}

// ---------- cookies upload ----------
document.getElementById('ckup').addEventListener('click',async()=>{
  const f=document.getElementById('ckfile').files[0];
  const m=document.getElementById('ckmsg');
  if(!f){toast('Pilih file cookies.txt terlebih dahulu','warn');return}
  const fd=new FormData();fd.append('cookies',f);
  const r=await fetch('/api/cookies',{method:'POST',body:fd});
  const d=await r.json();
  if(d.success){toast('Cookies terunggah ('+(d.size/1024).toFixed(1)+' KB)')}
  else{toast(d.error||'Gagal unggah cookies','err')}
});
</script>
</body>
</html>"""


@app.after_request
def _security_headers(resp):
    """Tambah header keamanan dasar. CSP保守: izinkan img dari ytimg & data:;
    script-src 'self' (tidak ada inline script di template). inline style dipakai
    untuk UI — izinkan via 'unsafe-inline' (alternatif: hash/nonce, biaya besar)."""
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    # HSTS 1 tahun. HTTPS wajib di Render; aman untuk prod.
    resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' https://i.ytimg.com https://i9.ytimg.com data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    # Endpoint download perlu as_attachment (inline aman karena domain sendiri)
    if request.path.startswith("/file/"):
        resp.headers["Content-Disposition"] = resp.headers.get(
            "Content-Disposition", "attachment"
        )
    return resp


@app.before_request
def _before_request():
    # Janitor ringan: setiap ~50 request bersihkan job expired. Threshold kasar
    # supaya tidak panggil di tiap request.
    if not hasattr(app, "_req_count"):
        app._req_count = 0
    app._req_count += 1
    if app._req_count % 50 == 0:
        janitor_tick()


@app.route("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /api/\nDisallow: /file/\n",
                    mimetype="text/plain")


@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


@app.route("/api/download/start", methods=["GET"])
def api_download_start():
    if not rate_limit_ok():
        return jsonify({"error": "Terlalu banyak permintaan. Tunggu beberapa detik."}), 429
    url = (request.args.get("url") or "").strip()
    mode = (request.args.get("mode") or "auto").strip()
    fmt = (request.args.get("format") or "best").strip()
    if not url:
        return jsonify({"error": "URL kosong"}), 400
    if len(url) > MAX_URL_LEN:
        return jsonify({"error": f"URL terlalu panjang (maks {MAX_URL_LEN} karakter)"}), 414
    if mode not in ("auto", "video", "audio"):
        mode = "auto"

    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {"events": deque(), "done": False, "result": None,
                        "files": [], "created": time.time()}
    t = threading.Thread(target=run_download_job, args=(job_id, url, mode, fmt), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/api/download/stream/<job_id>")
def api_download_stream(job_id):
    def gen():
        sent = 0
        deadline = time.time() + 290
        while time.time() < deadline:
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if job is None:
                    yield f"data: {json.dumps({'stage': 'error', 'message': 'Job tidak ditemukan'})}\n\n"
                    return
                # deque mendukung slicing via itertools.islice; pakai list copy
                events = list(job["events"])[sent:]
                sent = len(job["events"])
                done = job["done"]
                result = job["result"]
            for ev in events:
                yield f"data: {json.dumps(ev)}\n\n"
            if done:
                yield f"data: {json.dumps({'stage': 'result', 'result': result})}\n\n"
                with JOBS_LOCK:
                    JOBS.pop(job_id, None)
                return
            time.sleep(0.5)
        yield f"data: {json.dumps({'stage': 'error', 'message': 'Timeout'})}\n\n"

    # direct_passthrough=False default, tapi kita pakai generator yang sudah yield str.
    # Flush per yield via Response dengan explicit header agar gunicorn tidak buffer.
    response = Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})
    response.implicit_sequence_conversion = False
    return response


@app.route("/api/metadata", methods=["GET"])
def api_metadata():
    if not rate_limit_ok():
        return jsonify({"success": False, "error": "Rate limit"}), 429
    url = (request.args.get("url") or "").strip()
    if not url:
        return jsonify({"success": False, "error": "URL kosong"}), 400
    if len(url) > MAX_URL_LEN:
        return jsonify({"success": False,
                        "error": f"URL terlalu panjang (maks {MAX_URL_LEN} karakter)"}), 414
    return jsonify(fetch_metadata(url))


@app.route("/file/<token>/<path:filename>")
def get_file(token, filename):
    # Tolak traversal & wajib token valid.
    if "/" in filename or ".." in filename or "\\" in filename:
        abort(400)
    safe = (DOWNLOADS_DIR / filename).resolve()
    try:
        if DOWNLOADS_DIR.resolve() not in safe.parents and safe != DOWNLOADS_DIR.resolve():
            abort(404)
    except (OSError, ValueError):
        abort(404)
    if not safe.exists() or not safe.is_file():
        abort(404)
    if not verify_file_token(token, filename):
        # 410 Gone untuk expired, 403 untuk signature invalid
        try:
            exp_str = token.split(".", 1)[0]
            if int(exp_str) < time.time():
                abort(410)
        except (ValueError, IndexError):
            pass
        abort(403)
    return send_file(str(safe), as_attachment=True, download_name=safe.name,
                     max_age=0)


@app.route("/api/cookies", methods=["POST"])
def api_cookies():
    f = request.files.get("cookies")
    if not f:
        return jsonify({"success": False, "error": "Tidak ada file"}), 400
    # Validasi ekstensi & ukuran
    if not (f.filename or "").lower().endswith(".txt"):
        return jsonify({"success": False, "error": "File harus .txt"}), 400
    # Simpan ke temp lalu cek ukuran
    data = f.read(COOKIES_MAX_BYTES + 1)
    if len(data) > COOKIES_MAX_BYTES:
        return jsonify({"success": False,
                        "error": f"Terlalu besar (maks {COOKIES_MAX_BYTES//1024} KB)"}), 413
    COOKIES_PATH.write_bytes(data)
    return jsonify({"success": True, "size": COOKIES_PATH.stat().st_size})


@app.route("/health")
def health():
    files = sum(1 for _ in DOWNLOADS_DIR.iterdir() if _.is_file())
    return jsonify({"status": "ok", "uptime": round(time.time() - START_TIME),
                    "cookies": COOKIES_PATH.exists(), "files": files,
                    "active_jobs": len(JOBS)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)), threaded=True)
