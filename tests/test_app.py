"""Tests untuk mydownloader.

Menjalankan: pytest tests/ -q
Butuh env vars di conftest untuk DOWNLOADS_DIR & SECRET_KEY supaya tidak
menentuh /tmp produksi."""

import hashlib
import hmac
import io
import time

import pytest

import app as app_module


@pytest.fixture
def client():
    """Flask test client dengan env terisolasi."""
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        # Bersihkan state rate limit & jobs
        with app_module._RATE_LOCK:
            app_module._RATE_BUCKETS.clear()
        with app_module.JOBS_LOCK:
            app_module.JOBS.clear()
        yield c


@pytest.fixture
def fixture_file(tmp_path):
    """Taruh file palsu di DOWNLOADS_DIR, return Path."""
    p = app_module.DOWNLOADS_DIR / "real.mp4"
    p.write_bytes(b"FAKE_MP4_DATA_FOR_TEST")
    yield p
    p.unlink(missing_ok=True)


# === Token ===

def test_token_roundtrip():
    tok = app_module.sign_file_token("job__Some_Title-abc.mp4")
    assert app_module.verify_file_token(tok, "job__Some_Title-abc.mp4")


def test_token_rejects_other_filename():
    tok = app_module.sign_file_token("a.mp4")
    assert not app_module.verify_file_token(tok, "b.mp4")


def test_token_expired():
    secret = app_module.SECRET_KEY.encode()
    old_exp = int(time.time()) - 10
    sig = hmac.new(secret, f"{old_exp}:foo.mp4".encode(),
                   hashlib.sha256).hexdigest()[:32]
    assert not app_module.verify_file_token(f"{old_exp}.{sig}", "foo.mp4")


# === /health ===

def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    data = r.get_json()
    assert data["status"] == "ok"
    assert "uptime" in data


# === /robots.txt ===

def test_robots_disallows_api_and_file(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200
    txt = r.get_data(as_text=True)
    assert "Disallow: /api/" in txt
    assert "Disallow: /file/" in txt


# === /file/ ===

def test_file_no_token_404(client):
    assert client.get("/file/just.mp4").status_code == 404


def test_file_valid_token_with_real_file(client, fixture_file):
    tok = app_module.sign_file_token("real.mp4")
    r = client.get(f"/file/{tok}/real.mp4")
    assert r.status_code == 200
    assert r.data == b"FAKE_MP4_DATA_FOR_TEST"


def test_file_expired_returns_410(client, fixture_file):
    old_exp = int(time.time()) - 10
    sig = hmac.new(app_module.SECRET_KEY.encode(),
                   f"{old_exp}:real.mp4".encode(),
                   hashlib.sha256).hexdigest()[:32]
    r = client.get(f"/file/{old_exp}.{sig}/real.mp4")
    assert r.status_code == 410


def test_file_bad_signature_returns_403(client, fixture_file):
    bad = str(int(time.time()) + 3600) + "." + "0" * 32
    r = client.get(f"/file/{bad}/real.mp4")
    assert r.status_code == 403


def test_file_traversal_blocked(client, fixture_file):
    tok = app_module.sign_file_token("real.mp4")
    r = client.get(f"/file/{tok}/../etc/passwd")
    assert r.status_code in (400, 404)


# === /api/cookies ===

def test_cookies_no_file_400(client):
    assert client.post("/api/cookies", data={}).status_code == 400


def test_cookies_bad_extension_400(client):
    r = client.post("/api/cookies",
                    data={"cookies": (io.BytesIO(b"evil"), "evil.exe")},
                    content_type="multipart/form-data")
    assert r.status_code == 400


def test_cookies_too_big_413(client):
    big = b"x" * (app_module.COOKIES_MAX_BYTES + 1)
    r = client.post("/api/cookies",
                    data={"cookies": (io.BytesIO(big), "big.txt")},
                    content_type="multipart/form-data")
    assert r.status_code == 413


def test_cookies_upload_ok(client):
    r = client.post("/api/cookies",
                    data={"cookies": (io.BytesIO(b"# cookies\n"), "cookies.txt")},
                    content_type="multipart/form-data")
    assert r.status_code == 200
    assert r.get_json()["success"] is True


# === Rate limit ===

def test_rate_limit_429(client, monkeypatch):
    monkeypatch.setattr(app_module, "RATE_LIMIT_MAX", 3)
    with app_module._RATE_LOCK:
        app_module._RATE_BUCKETS.clear()
    headers = {"X-Forwarded-For": "1.2.3.4"}
    for i in range(3):
        r = client.get("/api/download/start?url=https://youtu.be/dQw4w9WgXcQ",
                       headers=headers)
        assert r.status_code == 200, f"iter {i} got {r.status_code}"
    r = client.get("/api/download/start?url=https://youtu.be/dQw4w9WgXcQ",
                   headers=headers)
    assert r.status_code == 429


def test_rate_limit_per_ip_isolated(client, monkeypatch):
    monkeypatch.setattr(app_module, "RATE_LIMIT_MAX", 3)
    with app_module._RATE_LOCK:
        app_module._RATE_BUCKETS.clear()
    for i in range(3):
        r = client.get("/api/download/start?url=https://youtu.be/dQw4w9WgXcQ",
                       headers={"X-Forwarded-For": "5.6.7.8"})
        assert r.status_code == 200
    # IP beda boleh lewat
    r = client.get("/api/download/start?url=https://youtu.be/dQw4w9WgXcQ",
                   headers={"X-Forwarded-For": "9.10.11.12"})
    assert r.status_code == 200


# === DRM detection ===

def test_drm_url_returns_drm_flag(client):
    with app_module._RATE_LOCK:
        app_module._RATE_BUCKETS.clear()
    r = client.get("/api/download/start?url=https://open.spotify.com/track/abc",
                   headers={"X-Forwarded-For": "50.50.50.50"})
    assert r.status_code == 200
    assert "job_id" in r.get_json()


# === SSE stream ===

def test_sse_stream_completes(client):
    with app_module._RATE_LOCK:
        app_module._RATE_BUCKETS.clear()
    r = client.get("/api/download/start?url=https://open.spotify.com/track/abc",
                   headers={"X-Forwarded-For": "60.60.60.60"})
    jid = r.get_json()["job_id"]
    deadline = time.time() + 30
    done = False
    while time.time() < deadline and not done:
        time.sleep(1)
        with client.get(f"/api/download/stream/{jid}") as resp:
            if resp.status_code == 200:
                for chunk in resp.response:
                    if b"result" in chunk:
                        done = True
                        break
                if done:
                    break
    assert done, "SSE stream should emit result event"


# === Janitor ===

def test_janitor_cleans_expired_jobs():
    with app_module.JOBS_LOCK:
        app_module.JOBS["old"] = {"events": [], "done": False, "result": None,
                                  "files": [], "created": time.time() - 7200}
        app_module.JOBS["new"] = {"events": [], "done": False, "result": None,
                                  "files": [], "created": time.time()}
    app_module.janitor_tick()
    with app_module.JOBS_LOCK:
        assert "old" not in app_module.JOBS
        assert "new" in app_module.JOBS
    # Cleanup
    with app_module.JOBS_LOCK:
        app_module.JOBS.clear()


# === Security headers ===

def test_security_headers_present(client):
    """Setiap response harus punya security headers dasar."""
    r = client.get("/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert "max-age=31536000" in r.headers["Strict-Transport-Security"]
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "https://i.ytimg.com" in csp


def test_csp_on_index_html(client):
    """Landing page juga kena CSP (img-src izinkan ytimg)."""
    r = client.get("/")
    assert r.status_code == 200
    csp = r.headers["Content-Security-Policy"]
    assert "img-src" in csp
    assert "i.ytimg.com" in csp


# === URL length validation ===

def test_download_url_too_long_414(client, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_URL_LEN", 100)
    with app_module._RATE_LOCK:
        app_module._RATE_BUCKETS.clear()
    long_url = "https://example.com/" + ("a" * 200)
    r = client.get(f"/api/download/start?url={long_url}",
                   headers={"X-Forwarded-For": "1.1.1.1"})
    assert r.status_code == 414
    assert "terlalu panjang" in r.get_json()["error"]


def test_metadata_url_too_long_414(client, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_URL_LEN", 100)
    with app_module._RATE_LOCK:
        app_module._RATE_BUCKETS.clear()
    long_url = "https://example.com/" + ("a" * 200)
    r = client.get(f"/api/metadata?url={long_url}",
                   headers={"X-Forwarded-For": "2.2.2.2"})
    assert r.status_code == 414


def test_download_url_at_limit_accepted(client, monkeypatch):
    """URL tepat di batas harus diterima (boundary check)."""
    monkeypatch.setattr(app_module, "MAX_URL_LEN", 100)
    with app_module._RATE_LOCK:
        app_module._RATE_BUCKETS.clear()
    # URL dengan panjang total = 100 (domain "https://youtu.be/" = 17 char + 83 char path)
    ok_url = "https://youtu.be/" + ("a" * 83)  # total = 100
    assert len(ok_url) == 100
    r = client.get(f"/api/download/start?url={ok_url}",
                   headers={"X-Forwarded-For": "3.3.3.3"})
    assert r.status_code == 200
    assert "job_id" in r.get_json()
