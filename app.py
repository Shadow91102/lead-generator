#!/usr/bin/env python
"""Local web UI for the lead scraper.

    python app.py
    -> open http://127.0.0.1:5000 in your browser

Runs each scrape as a background job and streams progress the browser polls
for. Nothing leaves your machine; results download as a CSV.
"""
from __future__ import annotations

import os
import re
import sys
import hmac
import uuid
import secrets
import time
import asyncio
import threading
from collections import deque
from dataclasses import asdict

from flask import Flask, request, jsonify, send_file, session, redirect

import pipeline
from models import write_csv

# Parent process runs only httpx work -> Selector loop is clean on Windows.
# (The gmaps child process sets its own Proactor policy for Playwright.)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "outputs")
os.makedirs(OUT_DIR, exist_ok=True)

app = Flask(__name__)
# Sign session cookies. Set SECRET_KEY in the env for logins that survive a
# restart; otherwise a random key is used and everyone re-logs in after a reboot.
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

# ---- App login ----------------------------------------------------------
# Set APP_PASSWORD (and optionally APP_USERNAME, default "admin") to require a
# login. Leave APP_PASSWORD unset to run open — handy for a local dev session.
AUTH_USER = os.environ.get("APP_USERNAME", "admin")
AUTH_PASS = os.environ.get("APP_PASSWORD", "")
LOGIN_REQUIRED = bool(AUTH_PASS)
_PUBLIC_PATHS = {"/login", "/favicon.ico"}

LOGIN_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in &middot; Lead Scraper</title>
<style>
  :root { color-scheme: dark; } * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; display:flex; align-items:center;
         justify-content:center; background:#0f1115; color:#e6e8ec;
         font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif; }
  .card { width:100%; max-width:360px; background:#171a21; border:1px solid #262b36;
          border-radius:14px; padding:28px 26px; box-shadow:0 10px 40px rgba(0,0,0,.4); }
  h1 { margin:0 0 4px; font-size:20px; } .sub { margin:0 0 20px; color:#9aa3b2; font-size:13px; }
  label { display:block; margin:0 0 6px; font-size:13px; color:#c3cad6; }
  input { width:100%; padding:10px 12px; margin-bottom:16px; border-radius:9px;
          border:1px solid #2c323e; background:#0f1218; color:#e6e8ec; font-size:15px; }
  input:focus { outline:none; border-color:#3b82f6; }
  button { width:100%; padding:11px; border:0; border-radius:9px; background:#3b82f6;
           color:#fff; font-size:15px; font-weight:600; cursor:pointer; }
  button:hover { background:#2f74e8; }
  .err { margin:0 0 14px; padding:9px 12px; border-radius:8px; font-size:13px;
         background:#3b1d22; border:1px solid #6b2b34; color:#ffb4bd; }
</style></head>
<body>
  <form class="card" method="post" action="/login">
    <h1>Lead Scraper</h1>
    <p class="sub">Sign in to continue</p>
    <!--ERR-->
    <label for="u">Username</label>
    <input id="u" name="username" autocomplete="username" autofocus>
    <label for="p">Password</label>
    <input id="p" name="password" type="password" autocomplete="current-password">
    <button type="submit">Sign in</button>
  </form>
</body></html>"""


def _login_page(error: str = "") -> str:
    note = f'<p class="err">{error}</p>' if error else ""
    return LOGIN_HTML.replace("<!--ERR-->", note)


@app.before_request
def _require_login():
    """Gate every route behind a session when APP_PASSWORD is set."""
    if not LOGIN_REQUIRED or session.get("logged_in"):
        return None
    p = request.path
    if p in _PUBLIC_PATHS or p.startswith("/static/"):
        return None
    if p.startswith("/api/"):
        return jsonify({"error": "authentication required"}), 401
    return redirect("/login")


@app.get("/login")
def login_form():
    if not LOGIN_REQUIRED or session.get("logged_in"):
        return redirect("/")
    return _login_page()


@app.post("/login")
def login_submit():
    wait = _rate_retry_after("login", LOGIN_LIMIT, LOGIN_WINDOW)
    if wait:
        return _login_page(f"Too many attempts — wait {wait}s and try again."), 429
    user = (request.form.get("username") or "").strip()
    pw = request.form.get("password") or ""
    # Constant-time on both fields so neither the username nor password leaks
    # via response timing.
    user_ok = hmac.compare_digest(user.encode(), AUTH_USER.encode())
    pass_ok = hmac.compare_digest(pw.encode(), AUTH_PASS.encode())
    if user_ok and pass_ok:
        session["logged_in"] = True
        session["user"] = user
        return redirect("/")
    return _login_page("Invalid username or password."), 401


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/login" if LOGIN_REQUIRED else "/")


JOBS: dict[str, dict] = {}

# ---- Rate limiting (in-memory; correct because we run a single worker) -----
RUN_LIMIT    = int(os.environ.get("RUN_LIMIT", "5"))     # scrapes / window / IP
RUN_WINDOW   = int(os.environ.get("RUN_WINDOW", "60"))
LOGIN_LIMIT  = int(os.environ.get("LOGIN_LIMIT", "8"))   # login tries / window / IP
LOGIN_WINDOW = int(os.environ.get("LOGIN_WINDOW", "300"))
MAX_ACTIVE_JOBS = int(os.environ.get("MAX_ACTIVE_JOBS", "3"))  # concurrent scrapes, all users

_RL_LOCK = threading.Lock()
_RL_HITS: dict[tuple, deque] = {}


def _client_ip() -> str:
    # Behind nginx every request's remote_addr is 127.0.0.1, so trust the proxy's
    # forwarded client IP (our nginx sets X-Forwarded-For / X-Real-IP).
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.headers.get("X-Real-IP") or request.remote_addr or "unknown"


def _rate_retry_after(bucket: str, limit: int, window: int) -> int:
    """Sliding-window limiter. Returns 0 if allowed, else seconds until a slot frees."""
    now = time.monotonic()
    key = (bucket, _client_ip())
    with _RL_LOCK:
        dq = _RL_HITS.setdefault(key, deque())
        cutoff = now - window
        while dq and dq[0] <= cutoff:   # drop timestamps older than the window
            dq.popleft()
        if len(dq) >= limit:
            return int(dq[0] + window - now) + 1
        dq.append(now)
        return 0


def _build_city_index():
    """Offline city index for the location search box (geonamescache).

    Returns a list of (name_lower, label, population) sorted by population so
    that major cities surface first. US cities include their state name.
    """
    try:
        import geonamescache
    except Exception:
        return []
    gc = geonamescache.GeonamesCache()
    countries = {iso: c["name"] for iso, c in gc.get_countries().items()}
    us_states = {code: s["name"] for code, s in gc.get_us_states().items()}
    index = []
    for c in gc.get_cities().values():
        cc = c["countrycode"]
        country = countries.get(cc, cc)
        region = us_states.get(c.get("admin1code")) if cc == "US" else None
        parts = [c["name"]] + ([region] if region else []) + [country]
        label = ", ".join(parts)
        index.append((c["name"].lower(), label, c.get("population", 0)))
    index.sort(key=lambda x: -x[2])
    return index


CITY_INDEX = _build_city_index()


@app.get("/api/places")
def api_places():
    """Type-ahead city search. Prefix matches first, ranked by population."""
    q = (request.args.get("q") or "").strip().lower()
    if len(q) < 2 or not CITY_INDEX:
        return jsonify([])
    starts, contains = [], []
    for name_lower, label, _pop in CITY_INDEX:  # already population-sorted
        if name_lower.startswith(q):
            starts.append(label)
        elif len(contains) < 8 and q in name_lower:
            contains.append(label)
        if len(starts) >= 8:
            break
    seen, out = set(), []
    for label in starts + contains:
        if label not in seen:
            seen.add(label)
            out.append(label)
        if len(out) >= 8:
            break
    return jsonify([{"label": r, "value": r} for r in out])


def _safe_name(*parts: str) -> str:
    raw = "_".join(p for p in parts if p)
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("_") or "leads"


@app.get("/")
def index():
    with open(os.path.join(HERE, "webui.html"), encoding="utf-8") as f:
        html = f.read()
    # Show the logout link only when a login is actually in force.
    link = ('<a href="/logout" style="position:fixed;top:14px;right:18px;'
            'color:#9aa3b2;font-size:13px;text-decoration:none">Log out &rsaquo;</a>'
            if LOGIN_REQUIRED else "")
    return html.replace("<!--LOGOUT-->", link)


@app.get("/stored")
def stored_page():
    return send_file(os.path.join(HERE, "stored.html"))


@app.get("/api/stored")
def api_stored():
    src = request.args.get("source")
    src = src if src in ("gmaps", "osm") else None
    import store
    return jsonify({"counts": store.counts(), "leads": store.all_leads(src)})


@app.get("/api/stored.csv")
def api_stored_csv():
    src = request.args.get("source")
    src = src if src in ("gmaps", "osm") else None
    import store
    rows = store.all_business(src)
    fname = f"stored_leads_{src or 'all'}.csv"
    path = os.path.join(OUT_DIR, fname)
    write_csv(rows, path)
    return send_file(path, as_attachment=True, download_name=fname)


@app.post("/api/run")
def api_run():
    wait = _rate_retry_after("run", RUN_LIMIT, RUN_WINDOW)
    if wait:
        resp = jsonify({"error": f"Too many scrapes — wait {wait}s and try again."})
        resp.headers["Retry-After"] = str(wait)
        return resp, 429
    active = sum(1 for j in JOBS.values() if j.get("status") == "running")
    if active >= MAX_ACTIVE_JOBS:
        return jsonify({"error": "Server busy — too many scrapes running right now. "
                                 "Try again in a minute."}), 429

    data = request.get_json(force=True, silent=True) or {}
    query = (data.get("query") or "").strip()
    location = (data.get("location") or "").strip()
    if not query or not location:
        return jsonify({"error": "Business type and location are both required."}), 400

    try:
        limit = max(1, min(int(data.get("limit") or 50), 300))
    except (TypeError, ValueError):
        limit = 50
    source = data.get("source") if data.get("source") in ("gmaps", "osm") else "gmaps"
    try:
        concurrency = max(1, min(int(data.get("concurrency") or 10), 30))
    except (TypeError, ValueError):
        concurrency = 10
    no_scrape = bool(data.get("no_website_scrape"))
    skip_known = bool(data.get("skip_known"))

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "status": "running", "stage": "find", "done": 0, "total": 0,
        "message": "Starting…", "count": 0, "with_email": 0,
        "leads": None, "csv": None, "error": None,
        "query": query, "location": location, "source": source,
    }
    t = threading.Thread(
        target=_run_job,
        args=(job_id, query, location, limit, source, concurrency, no_scrape, skip_known),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": job_id})


def _run_job(job_id, query, location, limit, source, concurrency, no_scrape, skip_known):
    job = JOBS[job_id]

    def on_progress(stage, done, total, message):
        job.update(stage=stage, done=done, total=total, message=message)

    try:
        businesses = pipeline.generate_leads(
            query, location, limit, source, concurrency, no_scrape,
            headful=False, skip_known=skip_known, on_progress=on_progress,
        )
        csv_name = _safe_name("leads", query, location) + ".csv"
        csv_path = os.path.join(OUT_DIR, f"{job_id}_{csv_name}")
        write_csv(businesses, csv_path)

        leads = [asdict(b) for b in businesses]  # emails stays a list for the table
        job.update(
            status="done", stage="done", message="Done",
            leads=leads, count=len(businesses),
            with_email=sum(1 for b in businesses if b.emails),
            with_site=sum(1 for b in businesses if b.website),
            csv=os.path.basename(csv_path), csv_name=csv_name,
        )
    except Exception as e:  # noqa: BLE001 - surface any failure to the UI
        job.update(status="error", message="Error", error=str(e))


@app.get("/api/status/<job_id>")
def api_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(job)


@app.get("/api/download/<job_id>")
def api_download(job_id):
    job = JOBS.get(job_id)
    if not job or not job.get("csv"):
        return "Not ready", 404
    path = os.path.join(OUT_DIR, job["csv"])
    if not os.path.exists(path):
        return "File missing", 404
    return send_file(path, as_attachment=True, download_name=job.get("csv_name", "leads.csv"))


def main():
    # HOST=0.0.0.0 to expose directly; behind nginx keep 127.0.0.1. WAITRESS=1
    # serves via a production WSGI server instead of Flask's dev server.
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    prod = os.environ.get("WAITRESS") == "1"
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    url = f"http://{shown}:{port}"
    print("\n  " + "=" * 46)
    print("   Lead Scraper UI is running")
    print(f"   Open  {url}  in your browser")
    login = f"ON  (user: {AUTH_USER})" if LOGIN_REQUIRED else "OFF (set APP_PASSWORD to enable)"
    print(f"   Login {login}")
    print(f"   Limit {RUN_LIMIT} scrapes/{RUN_WINDOW}s per IP, {MAX_ACTIVE_JOBS} concurrent max")
    print("   Press Ctrl+C to stop")
    print("  " + "=" * 46 + "\n")
    # Pop a browser only for a genuine local dev run — never under a server.
    if not prod and host in ("127.0.0.1", "localhost"):
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    if prod:
        # waitress: one process + a thread pool, so the in-memory JOBS dict and
        # the background scrape threads keep working (unlike multi-worker gunicorn).
        from waitress import serve
        serve(app, host=host, port=port, threads=8)
    else:
        app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
