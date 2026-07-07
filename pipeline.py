"""Shared lead-generation pipeline used by both the CLI (leadgen.py) and the
web UI (app.py).

`generate_leads(...)` runs the two stages (find businesses -> scrape their
websites) and reports progress through an `on_progress(stage, done, total,
message)` callback so any front-end can render a progress bar.
"""
from __future__ import annotations

import os
import sys
import json
import asyncio
import tempfile
import subprocess
from urllib.parse import urlparse

from models import Business
from scrape_site import scrape_website, make_client, normalize_url, _registrable

HERE = os.path.dirname(os.path.abspath(__file__))


def _noop(stage: str, done: int, total: int, message: str) -> None:
    pass


def dedupe(businesses: list[Business]) -> list[Business]:
    """Drop duplicate businesses, keyed by website domain, else name+address."""
    seen: set[str] = set()
    out: list[Business] = []
    for b in businesses:
        if b.website:
            host = urlparse(normalize_url(b.website)).netloc
            key = "w:" + _registrable(host)
        else:
            key = "n:" + b.name.strip().lower() + "|" + b.address.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(b)
    return out


def fetch_gmaps(query: str, location: str, limit: int, headful: bool = False,
                on_progress=_noop) -> list[Business]:
    """Run the Playwright scraper in a child process (python -m sources.gmaps).

    The child writes the JSON result to a temp file (--out-json) and streams
    structured "__PROG__ <stage> <done> <total>" progress lines on stderr. We
    parse those into on_progress() calls; any other stderr passes through.
    Writing results to a file (not stdout) avoids pipe-buffer deadlocks.
    """
    fd, json_path = tempfile.mkstemp(suffix=".json", prefix="leads_")
    os.close(fd)
    cmd = [sys.executable, "-m", "sources.gmaps",
           "--query", query, "--location", location,
           "--limit", str(limit), "--out-json", json_path]
    if headful:
        cmd.append("--headful")

    on_progress("find", 0, 0, "Searching Google Maps…")
    proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True, bufsize=1)
    for line in proc.stderr:
        line = line.rstrip("\n")
        if line.startswith("__PROG__"):
            parts = line.split()
            if len(parts) == 4:
                _, stage, done, total = parts
                if stage == "search":
                    msg = "Searching Google Maps…"
                elif stage == "collect":
                    msg = f"Found {total} listings"
                else:
                    msg = "Opening places for website & phone"
                try:
                    on_progress("find", int(done), int(total), msg)
                except ValueError:
                    pass
        elif line.strip():
            print(line, file=sys.stderr)
    proc.wait()

    data = []
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        pass
    finally:
        try:
            os.remove(json_path)
        except OSError:
            pass
    return [Business(**d) for d in data]


async def enrich_from_websites(businesses: list[Business], concurrency: int,
                               on_progress=_noop) -> None:
    """Scrape each business's website for emails/phone/socials, in place."""
    targets = [b for b in businesses if b.website]
    total = len(targets)
    on_progress("scrape", 0, total, "Scraping websites for emails")
    if not targets:
        return
    sem = asyncio.Semaphore(concurrency)
    done = 0

    async with make_client() as client:
        async def worker(b: Business) -> None:
            nonlocal done
            async with sem:
                try:
                    data = await scrape_website(b.website, client)
                except Exception:
                    data = {"emails": [], "socials": {}, "phone": ""}
            if data["emails"]:
                b.emails = list(dict.fromkeys(b.emails + data["emails"]))
            if not b.phone and data["phone"]:
                b.phone = data["phone"]
            for net in ("instagram", "facebook", "linkedin", "tiktok", "youtube"):
                if not getattr(b, net) and data["socials"].get(net):
                    setattr(b, net, data["socials"][net])
            done += 1
            on_progress("scrape", done, total, "Scraping websites for emails")

        await asyncio.gather(*(worker(b) for b in targets))


def generate_leads(query: str, location: str, limit: int, source: str = "gmaps",
                   concurrency: int = 10, no_website_scrape: bool = False,
                   headful: bool = False, on_progress=_noop) -> list[Business]:
    """Full pipeline: find businesses -> scrape their websites -> return leads."""
    if source == "gmaps":
        businesses = fetch_gmaps(query, location, limit, headful, on_progress)
    else:
        on_progress("find", 0, 0, "Querying OpenStreetMap…")
        from sources import osm
        businesses = asyncio.run(osm.fetch_businesses(query, location, limit))

    businesses = dedupe(businesses)

    if not no_website_scrape:
        asyncio.run(enrich_from_websites(businesses, concurrency, on_progress))

    on_progress("done", len(businesses), len(businesses), "Done")
    return businesses
