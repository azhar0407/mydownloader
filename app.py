#!/usr/bin/env python3
"""Downloader Web untuk Render/Render Web Services"""

from flask import Flask, request, jsonify
import subprocess
import json
import os
import re
from pathlib import Path

app = Flask(__name__)

DOWNLOADS_DIR = Path("/tmp/downloads")
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

# Pattern untuk URL platform
URL_PATTERNS = {
    'youtube': r'(youtube\.com|youtu\.be)',
    'spotify': r'spotify\.com',
    'soundcloud': r'soundcloud\.com',
    'twitter': r'twitter\.com|x\.com',
    'facebook': r'facebook\.com|fb\.watch',
}

def detect_platform(url: str) -> str:
    for platform, pattern in URL_PATTERNS.items():
        if re.search(pattern, url, re.IGNORECASE):
            return platform
    return 'generic'

@app.route('/', methods=['GET'])
def index():
    return '''
<!doctype html>
<html>
<head><title>Downloader Web</title></head>
<body>
<h1>Web Downloader</h1>
<form action="/download" method="get">
    <input type="text" name="url" placeholder="URL video/lagu..." size="50" required>
    <button type="submit">Download</button>
</form>
<div id="result"></div>
<script>
const form = document.querySelector('form');
form.onsubmit = async function(e) {
    e.preventDefault();
    const url = this.url.value;
    const resp = await fetch('/download?url=' + encodeURIComponent(url));
    const data = await resp.json();
    document.getElementById('result').innerHTML = '<pre>' + JSON.stringify(data, null, 2) + '</pre>';
};
</script>
</body>
</html>
'''

@app.route('/download', methods=['GET'])
def download():
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'URL diperlukan', 'success': False})
    
    platform = detect_platform(url)
    out_dir = str(DOWNLOADS_DIR)
    filename = f"{Path(url).stem}_{platform}"
    
    # yt-dlp command
    cmd = [
        'yt-dlp',
        '--quiet',
        '-o', f'{out_dir}/{filename}.%(ext)s',
        '--no-overwrites',
        url
    ]
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300  # 5 menit max
        )
        
        # Cari file yang dibuat
        files = list(DOWNLOADS_DIR.glob(f"{filename}*"))
        if files:
            latest = max(files, key=lambda x: x.stat().st_size)
            size = latest.stat().st_size
            return jsonify({
                'success': True,
                'platform': platform,
                'file': latest.name,
                'url': f'/file/{latest.name}',
                'size_bytes': size,
                'size_kb': round(size/1024, 2),
                'message': f'Download berhasil: {latest.name}'
            })
        else:
            return jsonify({
                'success': False,
                'platform': platform,
                'error': 'File tidak ditemukan',
                'stderr': result.stderr[-500:] if result.stderr else ''
            })
            
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Timeout download', 'success': False})
    except Exception as e:
        return jsonify({'error': str(e), 'success': False})

@app.route('/file/<filename>', methods=['GET'])
def get_file(filename):
    filepath = DOWNLOADS_DIR / filename
    if not filepath.exists():
        return jsonify({'error': 'File tidak ditemukan'}), 404
    return jsonify({
        'url': f'/download-url-for-file/{filename}'
    })

@app.route('/download-url-for-file/<filename>', methods=['GET'])
def get_download_url(filename):
    return jsonify({
        'url': f'/file/{filename}'
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 3000)))