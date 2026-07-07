"""Google Maps lead source via Playwright.

The results feed only exposes each place's detail URL -- the website, phone
and address live in the place *detail panel*. So we work in two passes:

  1. Scroll the results feed and collect (name, place_url) for each listing.
  2. Open each place and read website/phone/address/rating from the panel's
     stable `data-item-id` attributes.

NOTE: Automated access to Google Maps is against Google's Terms of Service.
Use at low volume for your own outreach. Pass 2 visits one place at a time, so
it is slower than a flat scrape but it is the only way to get the website (and
therefore an email). If Google changes its markup these selectors may need
updating; `--source osm` is the ToS-clean, no-browser alternative.
"""
from __future__ import annotations

import os
import re
import sys
import json
import asyncio
from urllib.parse import quote_plus

from models import Business

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


async def _dismiss_consent(page) -> None:
    """Click through Google's cookie-consent wall if it appears."""
    for sel in (
        'button[aria-label="Accept all"]',
        'button[aria-label="Reject all"]',
        'form[action*="consent"] button',
        'button:has-text("Accept all")',
        'button:has-text("Reject all")',
        'button:has-text("I agree")',
    ):
        try:
            btn = page.locator(sel).first
            if await btn.count() and await btn.is_visible():
                await btn.click(timeout=3000)
                await page.wait_for_timeout(1500)
                return
        except Exception:
            continue


async def _collect_places(page, limit: int) -> list[tuple[str, str]]:
    """Pass 1: scroll the feed, gather (place_url, name) pairs."""
    feed = page.locator('div[role="feed"]')
    seen: dict[str, str] = {}
    prev = -1
    stagnant = 0
    for _ in range(40):
        anchors = await page.locator("a.hfpxzc").evaluate_all(
            "els => els.map(a => ({href: a.href, name: a.getAttribute('aria-label')}))"
        )
        for a in anchors:
            href, name = a.get("href"), a.get("name")
            if href and name and href not in seen:
                seen[href] = name
        if len(seen) >= limit:
            break
        if len(seen) == prev:
            stagnant += 1
            if stagnant >= 3:
                break  # reached the end of the list
        else:
            stagnant = 0
        prev = len(seen)
        try:
            await feed.evaluate("el => el.scrollBy(0, el.scrollHeight)")
        except Exception:
            pass
        await page.wait_for_timeout(1800)
    return list(seen.items())[:limit]


async def _extract_place(page, href: str, name: str) -> Business:
    """Pass 2: open one place and read its detail panel."""
    b = Business(source="gmaps", name=name)
    try:
        await page.goto(href, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_selector("h1.DUwDvf", timeout=10000)
    except Exception:
        return b  # keep at least the name from the feed

    try:
        h = page.locator("h1.DUwDvf").first
        if await h.count():
            b.name = (await h.inner_text()).strip() or b.name
    except Exception:
        pass

    # Website: the "authority" action row holds the real site URL.
    try:
        w = page.locator('a[data-item-id="authority"]').first
        if await w.count():
            b.website = (await w.get_attribute("href")) or ""
    except Exception:
        pass

    # Phone: data-item-id looks like "phone:tel:+15125551234".
    try:
        ph = page.locator('[data-item-id^="phone:tel:"]').first
        if await ph.count():
            did = (await ph.get_attribute("data-item-id")) or ""
            b.phone = did.split("tel:")[-1]
    except Exception:
        pass

    # Address.
    try:
        ad = page.locator('[data-item-id="address"]').first
        if await ad.count():
            al = (await ad.get_attribute("aria-label")) or ""
            b.address = al.split(":", 1)[-1].strip()
    except Exception:
        pass

    # Rating + review count (e.g. "4.9(137)").
    try:
        fr = page.locator("div.F7nice").first
        if await fr.count():
            t = (await fr.inner_text()).strip()
            m = re.match(r"([\d.]+)", t)
            if m:
                b.rating = m.group(1)
            mr = re.search(r"\(([\d,]+)\)", t)
            if mr:
                b.reviews = mr.group(1).replace(",", "")
    except Exception:
        pass

    # Category chip.
    try:
        c = page.locator("button.DkEaL").first
        if await c.count():
            b.category = (await c.inner_text()).strip()
    except Exception:
        pass

    return b


async def fetch_businesses(
    query: str, location: str, limit: int, headful: bool = False
) -> list[Business]:
    from playwright.async_api import async_playwright

    search = f"{query} in {location}"
    url = f"https://www.google.com/maps/search/{quote_plus(search)}/?hl=en"

    businesses: list[Business] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not headful)
        context = await browser.new_context(
            user_agent=UA, locale="en-US", viewport={"width": 1280, "height": 900}
        )
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await _dismiss_consent(page)

            try:
                await page.wait_for_selector('div[role="feed"]', timeout=20000)
            except Exception:
                print("[gmaps] Results feed did not appear -- Google may have "
                      "served a different layout or a CAPTCHA. Try --headful to "
                      "inspect, or use --source osm.")
                return []

            print("__PROG__ search 0 0", file=sys.stderr, flush=True)
            places = await _collect_places(page, limit)
            # Structured progress -> stderr; the parent parses these lines.
            n = len(places)
            print(f"__PROG__ collect {n} {n}", file=sys.stderr, flush=True)
            print(f"__PROG__ visit 0 {n}", file=sys.stderr, flush=True)
            for i, (href, name) in enumerate(places, 1):
                b = await _extract_place(page, href, name)
                businesses.append(b)
                print(f"__PROG__ visit {i} {n}", file=sys.stderr, flush=True)
                await page.wait_for_timeout(400)  # be gentle
        finally:
            await browser.close()

    return businesses


def _main() -> None:
    """Worker entrypoint: run in a child process, print JSON leads to stdout.

    Kept separate from the parent so Playwright's Windows Proactor loop stays
    isolated here; os._exit() at the end skips its noisy finalizer.
    """
    import argparse
    from dataclasses import asdict

    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True)
    ap.add_argument("--location", required=True)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--out-json", default=None,
                    help="Write results JSON here instead of stdout")
    a = ap.parse_args()

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    # Manual loop, deliberately NOT closed: asyncio.run()'s internal loop.close()
    # trips a harmless "pending overlapped op" traceback from Playwright's pipe.
    # os._exit() below skips the finalizer, so we never close it here. Playwright
    # is already shut down (browser.close() awaited inside fetch_businesses).
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        businesses = loop.run_until_complete(
            fetch_businesses(a.query, a.location, a.limit, headful=a.headful)
        )
    except Exception as e:
        msg = str(e)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            print("[gmaps] Chromium isn't installed. Run:\n"
                  "    python -m playwright install chromium", file=sys.stderr)
        else:
            print(f"[gmaps] error: {msg}", file=sys.stderr)
        businesses = []

    payload = json.dumps([asdict(b) for b in businesses])
    if a.out_json:
        with open(a.out_json, "w", encoding="utf-8") as f:
            f.write(payload)
    else:
        sys.stdout.write(payload)
        sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    _main()
