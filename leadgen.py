#!/usr/bin/env python
"""leadgen -- free, reusable cold-outreach lead scraper (CLI).

Pick a business type and a location at run time; get a CSV of leads with
emails scraped live from each business's own website.

  python leadgen.py --query "dentist" --location "Austin, Texas" --limit 100

Prefer a UI? Run `python app.py` and open the local page in your browser.

Two lead sources (pluggable via --source):
  gmaps  Google Maps via Playwright (default; dense, ToS-gray)
  osm    OpenStreetMap / Overpass  (clean, free, sparser)
"""
from __future__ import annotations

import sys
import argparse
import asyncio

from models import write_csv
from pipeline import generate_leads

_last_stage = None


def _console_progress(stage: str, done: int, total: int, message: str) -> None:
    global _last_stage
    if stage != _last_stage and _last_stage is not None:
        print()  # new line when the stage changes
    _last_stage = stage
    if stage == "done":
        return
    if total:
        print(f"\r  {message}... {done}/{total}    ", end="", flush=True)
    else:
        print(f"\r  {message}    ", end="", flush=True)


def run(args: argparse.Namespace) -> int:
    print(f"[*] source={args.source}  query={args.query!r}  "
          f"location={args.location!r}  limit={args.limit}")

    businesses = generate_leads(
        args.query, args.location, args.limit, args.source,
        args.concurrency, args.no_website_scrape, args.headful,
        on_progress=_console_progress,
    )
    print()

    if not businesses:
        print("No businesses found. Try a more specific --query, a different "
              "--location, or --source osm.")
        return 1

    with_site = sum(1 for b in businesses if b.website)
    with_email = sum(1 for b in businesses if b.emails)
    write_csv(businesses, args.out)
    print(f"[+] Wrote {len(businesses)} leads -> {args.out}")
    print(f"    {with_site} have a website, {with_email} have at least one email "
          f"({(with_email / len(businesses) * 100):.0f}% email fill rate)")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Free cold-outreach lead scraper (Google Maps / OpenStreetMap).",
    )
    ap.add_argument("--query", required=True, help='Business type, e.g. "dentist"')
    ap.add_argument("--location", required=True, help='Place, e.g. "Austin, Texas"')
    ap.add_argument("--limit", type=int, default=50, help="Max businesses (default 50)")
    ap.add_argument("--source", choices=["gmaps", "osm"], default="gmaps",
                    help="Lead source (default gmaps)")
    ap.add_argument("--out", default="leads.csv", help="Output CSV path")
    ap.add_argument("--concurrency", type=int, default=10,
                    help="Parallel website fetches (default 10)")
    ap.add_argument("--no-website-scrape", action="store_true",
                    help="Skip stage 2 (source data only, no email mining)")
    ap.add_argument("--headful", action="store_true",
                    help="gmaps only: show the browser window (debugging)")
    args = ap.parse_args()

    # UTF-8 console so status messages (…, arrows) render on Windows.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # Parent runs only httpx work -> Selector loop is clean on Windows. (The
    # gmaps child sets its own Proactor policy for Playwright.)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        code = run(args)
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
