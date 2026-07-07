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
import uuid
import asyncio
import threading
from dataclasses import asdict

from flask import Flask, request, jsonify, send_file

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
JOBS: dict[str, dict] = {}


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
    return send_file(os.path.join(HERE, "webui.html"))


@app.post("/api/run")
def api_run():
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

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "status": "running", "stage": "find", "done": 0, "total": 0,
        "message": "Starting…", "count": 0, "with_email": 0,
        "leads": None, "csv": None, "error": None,
        "query": query, "location": location, "source": source,
    }
    t = threading.Thread(
        target=_run_job,
        args=(job_id, query, location, limit, source, concurrency, no_scrape),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": job_id})


def _run_job(job_id, query, location, limit, source, concurrency, no_scrape):
    job = JOBS[job_id]

    def on_progress(stage, done, total, message):
        job.update(stage=stage, done=done, total=total, message=message)

    try:
        businesses = pipeline.generate_leads(
            query, location, limit, source, concurrency, no_scrape,
            headful=False, on_progress=on_progress,
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
    # HOST=0.0.0.0 to expose on a server (VPS); default stays localhost-only.
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    url = f"http://{shown}:{port}"
    print("\n  " + "=" * 46)
    print("   Lead Scraper UI is running")
    print(f"   Open  {url}  in your browser")
    print("   Press Ctrl+C to stop")
    print("  " + "=" * 46 + "\n")
    # Only pop a browser on a genuine local run — never on a headless server.
    if host in ("127.0.0.1", "localhost"):
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
