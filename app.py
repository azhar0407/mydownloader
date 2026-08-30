#!/usr/bin/env python3
"""
mydownloader — Flask web downloader for YouTube, SoundCloud, Facebook, X (Twitter)
Deploy target: Render (free tier). Production WSGI: gunicorn.
"""

import os
import re
import subprocess
import time
import uuid
import threading
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template_string, redirect, url_for

app = Flask(__name__)

START_TIME = time.time()
DOWNLOADS_DIR = Path(os.environ.get("DOWNLOADS_DIR", "/tmp/downloads"))
COOKIES_PATH = Path(os.environ.get("COOKIES_PATH", "/tmp/cookies.txt"))
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

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

DRM_PLATFORMS = {"spotify", "netflix", "disney", "hbomax", "prime"}

INDEX_HTML = r"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mydownloader — Video & Audio Downloader</title>
<style>
:root{
  --bg:#0b0d12; --panel:#141821; --line:#222a36; --txt:#e6edf3;
  --muted:#8b95a4; --brand:#7c5cff; --brand2:#22d3ee; --ok:#22c55e; --err:#ef4444; --warn:#f59e0b;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--bg);color:var(--txt);
  font:15px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
a{color:var(--brand2);text-decoration:none}
.wrap{max-width:880px;margin:0 auto;padding:32px 20px}
header{display:flex;align-items:center;gap:14px;margin-bottom:28px}
.logo{
  width:44px;height:44px;border-radius:12px;
  background:linear-gradient(135deg,var(--brand),var(--brand2));
  display:flex;align-items:center;justify-content:center;font-weight:800;color:#000;font-size:20px
}
h1{font-size:22px;margin:0}
.sub{color:var(--muted);font-size:13px;margin-top:2px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:22px}
.tabs{display:flex;gap:6px;margin-bottom:18px;flex-wrap:wrap}
.tab{padding:8px 14px;border-radius:10px;background:#0e1218;border:1px solid var(--line);
  color:var(--muted);cursor:pointer;font-size:13px;user-select:none}
.tab.active{background:linear-gradient(135deg,var(--brand),var(--brand2));color:#000;border-color:transparent;font-weight:600}
label{display:block;font-size:13px;color:var(--muted);margin:0 0 6px}
.row{display:flex;gap:10px}
.row > *{flex:1}
input[type=url],input[type=text],select{
  width:100%;padding:12px 14px;border-radius:10px;background:#0e1218;border:1px solid var(--line);
  color:var(--txt);font-size:14px;outline:none
}
input:focus,select:focus{border-color:var(--brand)}
.btn{
  padding:12px 22px;border-radius:10px;border:0;cursor:pointer;font-weight:700;font-size:14px;
  background:linear-gradient(135deg,var(--brand),var(--brand2));color:#000
}
.btn:disabled{opacity:.55;cursor:not-allowed}
.btn-ghost{background:#0e1218;color:var(--txt);border:1px solid var(--line)}
.platforms{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
.chip{padding:6px 12px;border-radius:999px;background:#0e1218;border:1px solid var(--line);
  font-size:12px;color:var(--muted)}
.chip b{color:var(--txt)}
#result{margin-top:20px}
.alert{padding:14px 16px;border-radius:10px;border:1px solid var(--line);background:#0e1218;margin-bottom:14px}
.alert-ok{border-color:var(--ok);background:rgba(34,197,94,.08)}
.alert-err{border-color:var(--err);background:rgba(239,68,68,.08)}
.alert-warn{border-color:var(--warn);background:rgba(245,158,11,.08)}
.progress{height:8px;border-radius:99px;background:#0e1218;overflow:hidden;border:1px solid var(--line)}
.bar{height:100%;background:linear-gradient(90deg,var(--brand),var(--brand2));width:0;transition:width .2s}
.file-list{margin-top:12px;display:flex;flex-direction:column;gap:8px}
.file-item{display:flex;align-items:center;justify-content:space-between;gap:10px;
  padding:12px 14px;border-radius:10px;background:#0e1218;border:1px solid var(--line)}
.file-meta{font-size:12px;color:var(--muted)}
footer{margin-top:40px;color:var(--muted);font-size:12px;text-align:center}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid #000;border-top-color:transparent;
  border-radius:50%;animation:s 1s linear infinite;vertical-align:-2px;margin-right:8px}
@keyframes s{to{transform:rotate(360deg)}}
.help{font-size:12px;color:var(--muted);margin-top:8px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">M</div>
    <div>
      <h1>mydownloader</h1>
      <div class="sub">Unduh video & audio dari YouTube, SoundCloud, Facebook, X, dan lainnya</div>
    </div>
  </header>

  <div class="card">
    <div class="tabs" id="tabs">
      <div class="tab active" data-mode="auto">Auto-detect</div>
      <div class="tab" data-mode="video">Video (MP4)</div>
      <div class="tab" data-mode="audio">Audio (MP3)</div>
    </div>

    <label for="url">URL</label>
    <div class="row">
      <input id="url" type="url" placeholder="Tempel link video di sini…" autocomplete="off">
      <select id="format" style="max-width:140px">
        <option value="best">Best</option>
        <option value="1080">1080p</option>
        <option value="720">720p</option>
        <option value="480">480p</option>
      </select>
      <button id="go" class="btn">Unduh</button>
    </div>
    <div class="help" id="hint">Tempel URL publik dari platform yang didukung. Klik <b>Auto-detect</b> untuk format otomatis.</div>

    <div class="platforms">
      <span class="chip"><b>YouTube</b></span>
      <span class="chip"><b>SoundCloud</b></span>
      <span class="chip"><b>Facebook</b></span>
      <span class="chip"><b>X/Twitter</b></span>
      <span class="chip"><b>Instagram</b></span>
      <span class="chip"><b>TikTok</b></span>
      <span class="chip"><b>Reddit</b></span>
      <span class="chip"><b>Vimeo</b></span>
    </div>

    <div id="result"></div>
  </div>

  <div class="card" style="margin-top:18px">
    <label>Cookies (opsional, untuk YouTube &amp; lainnya)</label>
    <div class="row">
      <input id="ckfile" type="file" accept=".txt">
      <button id="ckup" class="btn btn-ghost">Unggah</button>
    </div>
    <div class="help">Jika YouTube meminta "Sign in to confirm", ekspor cookies dari browser via ekstensi <i>Get cookies.txt LOCALLY</i> lalu unggah di sini.</div>
    <div id="ckmsg" style="margin-top:10px"></div>
  </div>

  <footer>mydownloader · Render free tier · Service ephemeral, file sementara</footer>
</div>

<script>
const tabs=document.querySelectorAll('.tab');
let mode='auto';
tabs.forEach(t=>t.addEventListener('click',()=>{
  tabs.forEach(x=>x.classList.remove('active'));t.classList.add('active');mode=t.dataset.mode;
  document.getElementById('hint').innerHTML=
    mode==='audio'?'Mode audio: ekstrak MP3 192kbps dari sumber video/audio.':
    mode==='video'?'Mode video: unduh MP4 kualitas terbaik (hingga 1080p).':
    'Auto-detect: pilih format terbaik otomatis berdasarkan sumber.';
}));

const $u=document.getElementById('url'),$f=document.getElementById('format'),$go=document.getElementById('go');
const $r=document.getElementById('result');

function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}

function show(html){$r.innerHTML=html}
function progress(pct,label){
  show(`<div class="alert"><div>${esc(label)}</div><div class="progress" style="margin-top:8px"><div class="bar" style="width:${pct}%"></div></div></div>`);
}

$go.addEventListener('click',run);
$u.addEventListener('keydown',e=>{if(e.key==='Enter')run()});

async function run(){
  const url=$u.value.trim();
  if(!url){show('<div class="alert alert-warn">Masukkan URL terlebih dahulu.</div>');return}
  $go.disabled=true;
  progress(10,'<span class="spinner"></span>Mengirim permintaan…');
  try{
    const r=await fetch('/api/download?mode='+mode+'&format='+encodeURIComponent($f.value)+'&url='+encodeURIComponent(url));
    const d=await r.json();
    if(d.success){
      const sz=(d.size_bytes/1048576).toFixed(1);
      show(`<div class="alert alert-ok">
        <b>✓ Berhasil</b> · ${esc(d.platform)} · ${sz} MB
        <div class="file-list">
          <div class="file-item">
            <div><b>${esc(d.file)}</b><div class="file-meta">${d.size_bytes.toLocaleString()} bytes</div></div>
            <a class="btn" href="/file/${encodeURIComponent(d.file)}" download>Simpan</a>
          </div>
        </div>
      </div>`);
    }else if(d.drm){
      show(`<div class="alert alert-warn"><b>⊘ DRM</b> · ${esc(d.platform)} dilindungi DRM. yt-dlp tidak dapat mengunduh.</div>`);
    }else if(d.need_cookies){
      show(`<div class="alert alert-warn">
        <b>⚠ Cookies diperlukan</b> · ${esc(d.platform)} meminta verifikasi bot.
        <div class="help" style="margin-top:6px">Unggah cookies.txt dari browser Anda (lihat panel Cookies di bawah).</div>
      </div>`);
    }else{
      show(`<div class="alert alert-err"><b>✗ Gagal</b> · ${esc(d.platform)}<div class="help" style="margin-top:6px"><code>${esc(d.stderr||d.error||'Tidak diketahui')}</code></div></div>`);
    }
  }catch(e){
    show('<div class="alert alert-err"><b>✗ Network error</b> · '+esc(String(e))+'</div>');
  }
  $go.disabled=false;
}

// cookies upload
document.getElementById('ckup').addEventListener('click',async()=>{
  const f=document.getElementById('ckfile').files[0];
  const m=document.getElementById('ckmsg');
  if(!f){m.innerHTML='<div class="alert alert-warn">Pilih file cookies.txt terlebih dahulu.</div>';return}
  const fd=new FormData();fd.append('cookies',f);
  const r=await fetch('/api/cookies',{method:'POST',body:fd});
  const d=await r.json();
  m.innerHTML=d.success
    ?`<div class="alert alert-ok">✓ Cookies terunggah (${(d.size/1024).toFixed(1)} KB)</div>`
    :`<div class="alert alert-err">✗ ${esc(d.error||'Gagal')}</div>`;
});
</script>
</body>
</html>"""


def detect_platform(url: str) -> str:
    u = url.lower()
    for name, pat in URL_PATTERNS:
        if re.search(pat, u):
            return name
    if any(d in u for d in DRM_PLATFORMS):
        return "drm"
    return "generic"


def is_drm(url: str) -> bool:
    u = url.lower()
    return any(d in u for d in ("spotify.com/track", "spotify.com/album",
                                 "spotify.com/playlist", "netflix.com",
                                 "disneyplus.com", "hbomax.com", "primevideo.com"))


def build_cmd(url: str, mode: str, fmt: str, cookies: bool) -> list[str]:
    out_tpl = str(DOWNLOADS_DIR / "%(title).80B-%(id)s.%(ext)s")
    cmd = ["yt-dlp", "--no-playlist", "--no-progress", "--no-warnings",
           "--no-color", "-o", out_tpl,
           "--retries", "3", "--fragment-retries", "3",
           "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0.0.0 Safari/537.36",
           "--extractor-args", "youtube:player_client=web,default"]

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

    if cookies and COOKIES_PATH.exists():
        cmd += ["--cookies", str(COOKIES_PATH)]

    cmd.append(url)
    return cmd


def run_download(url: str, mode: str, fmt: str) -> dict:
    if is_drm(url):
        return {"success": False, "drm": True, "platform": "drm",
                "error": "DRM-protected content"}

    platform = detect_platform(url)
    cookies_used = COOKIES_PATH.exists()
    cmd = build_cmd(url, mode, fmt, cookies_used)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return {"success": False, "platform": platform,
                "error": "Timeout 180 detik"}
    except Exception as e:
        return {"success": False, "platform": platform, "error": str(e)}

    files = sorted(DOWNLOADS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    target = None
    for f in files:
        if f.is_file() and f.stat().st_size > 0:
            target = f
            break

    if target is None:
        stderr = (result.stderr or "")[-500:]
        if "not a bot" in stderr.lower() or "confirm you" in stderr.lower():
            return {"success": False, "need_cookies": True,
                    "platform": platform, "stderr": stderr}
        return {"success": False, "platform": platform, "stderr": stderr}

    size = target.stat().st_size
    return {"success": True, "platform": platform, "file": target.name,
            "size_bytes": size,
            "url": f"/file/{target.name}"}


@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


@app.route("/api/download", methods=["GET"])
def api_download():
    url = (request.args.get("url") or "").strip()
    mode = (request.args.get("mode") or "auto").strip()
    fmt = (request.args.get("format") or "best").strip()
    if not url:
        return jsonify({"success": False, "error": "URL kosong"}), 400
    if mode not in ("auto", "video", "audio"):
        mode = "auto"
    return jsonify(run_download(url, mode, fmt))


@app.route("/file/<path:filename>")
def get_file(filename):
    # Prevent directory traversal
    safe = (DOWNLOADS_DIR / filename).resolve()
    if not str(safe).startswith(str(DOWNLOADS_DIR.resolve())) or not safe.exists():
        return jsonify({"error": "File tidak ditemukan"}), 404
    return send_file(str(safe), as_attachment=True, download_name=safe.name)


@app.route("/api/cookies", methods=["POST"])
def api_cookies():
    f = request.files.get("cookies")
    if not f:
        return jsonify({"success": False, "error": "Tidak ada file"}), 400
    f.save(COOKIES_PATH)
    return jsonify({"success": True, "size": COOKIES_PATH.stat().st_size})


@app.route("/health")
def health():
    files = sum(1 for _ in DOWNLOADS_DIR.iterdir() if _.is_file())
    return jsonify({"status": "ok", "uptime": round(time.time() - START_TIME),
                    "cookies": COOKIES_PATH.exists(), "files": files})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
