"""SQLite store for scraped leads so repeated runs can skip ones already pulled.

Dedup is scoped *per source*: a lead is keyed on (source, dedup_key), where
dedup_key is the website's registrable domain if present, else name+address.
That means the same business found in both Google Maps and OpenStreetMap is
tracked once per source — skipping known gmaps leads never hides osm ones.

The DB file (leads.db) lives next to the app and is gitignored (it holds real
contact data). Set LEADS_DB to override its location.
"""
from __future__ import annotations

import os
import json
import sqlite3
import threading
from urllib.parse import urlparse

from models import Business, CSV_FIELDS
from scrape_site import normalize_url, _registrable

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("LEADS_DB", os.path.join(HERE, "leads.db"))
_LOCK = threading.Lock()

# Stored data columns = every Business field except `source` (it's its own column).
_COLS = [c for c in CSV_FIELDS if c != "source"]


def lead_key(b: Business) -> str:
    """Stable dedup key: website's registrable domain if any, else name+address."""
    if b.website:
        host = urlparse(normalize_url(b.website)).netloc
        return "w:" + _registrable(host)
    return "n:" + b.name.strip().lower() + "|" + b.address.strip().lower()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS leads (
            source     TEXT NOT NULL,
            dedup_key  TEXT NOT NULL,
            name TEXT, category TEXT, address TEXT, phone TEXT, website TEXT,
            emails TEXT, instagram TEXT, facebook TEXT, linkedin TEXT,
            tiktok TEXT, youtube TEXT, rating TEXT, reviews TEXT,
            first_seen TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (source, dedup_key)
        )"""
    )
    return conn


def known_keys(source: str) -> set[str]:
    """Every dedup key already stored for this source (empty set on any error)."""
    try:
        with _LOCK:
            conn = _connect()
            try:
                rows = conn.execute(
                    "SELECT dedup_key FROM leads WHERE source = ?", (source,)
                ).fetchall()
            finally:
                conn.close()
        return {r[0] for r in rows}
    except sqlite3.Error:
        return set()


def save_leads(businesses: list[Business], source: str) -> int:
    """Insert new leads (INSERT OR IGNORE keyed on source+dedup_key).

    Returns the number of rows actually added (duplicates are ignored).
    """
    if not businesses:
        return 0
    rows = []
    for b in businesses:
        vals = [getattr(b, c) for c in _COLS]
        vals[_COLS.index("emails")] = json.dumps(b.emails)  # list -> JSON text
        rows.append((source, lead_key(b), *vals))

    cols = "source, dedup_key, " + ", ".join(_COLS)
    placeholders = ", ".join(["?"] * (2 + len(_COLS)))
    with _LOCK:
        conn = _connect()
        try:
            conn.executemany(
                f"INSERT OR IGNORE INTO leads ({cols}) VALUES ({placeholders})", rows
            )
            conn.commit()
            return conn.total_changes
        finally:
            conn.close()


def counts() -> dict:
    """Lead counts per source, plus a 'total' key."""
    try:
        with _LOCK:
            conn = _connect()
            try:
                rows = conn.execute(
                    "SELECT source, COUNT(*) FROM leads GROUP BY source"
                ).fetchall()
            finally:
                conn.close()
    except sqlite3.Error:
        return {"total": 0}
    by = {src: n for src, n in rows}
    by["total"] = sum(by.values())
    return by


def all_leads(source: str | None = None) -> list[dict]:
    """Every stored lead (optionally one source), newest first, emails as a list."""
    q = "SELECT source, " + ", ".join(_COLS) + ", first_seen FROM leads"
    params: tuple = ()
    if source in ("gmaps", "osm"):
        q += " WHERE source = ?"
        params = (source,)
    q += " ORDER BY first_seen DESC"
    with _LOCK:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(q, params).fetchall()
        finally:
            conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["emails"] = json.loads(d["emails"] or "[]")
        out.append(d)
    return out


def all_business(source: str | None = None) -> list[Business]:
    """Stored leads as Business objects (for CSV export)."""
    out = []
    for d in all_leads(source):
        d.pop("first_seen", None)
        out.append(Business(**d))
    return out


if __name__ == "__main__":  # quick inspect / export from the shell
    import argparse
    ap = argparse.ArgumentParser(description="Inspect or export the lead store.")
    ap.add_argument("--source", choices=["gmaps", "osm"], help="Filter to one source")
    ap.add_argument("--csv", help="Export stored leads to this CSV path")
    a = ap.parse_args()

    c = counts()
    print(f"Stored leads: total={c.get('total', 0)}  "
          f"gmaps={c.get('gmaps', 0)}  osm={c.get('osm', 0)}")
    print(f"DB: {DB_PATH}")
    if a.csv:
        from models import write_csv
        rows = all_business(a.source)
        write_csv(rows, a.csv)
        print(f"Wrote {len(rows)} leads -> {a.csv}")
