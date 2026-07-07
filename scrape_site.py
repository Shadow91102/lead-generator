"""Visit a business website and extract emails, phone and social profiles.

Fully free / no API keys. Fetches the homepage plus a few likely contact
pages, then mines them for contact data. Handles a couple of common
obfuscation tricks (Cloudflare email protection, "name [at] domain" text).
"""
from __future__ import annotations

import re
import asyncio
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

try:
    import tldextract

    def _registrable(host: str) -> str:
        ext = tldextract.extract(host)
        return ".".join(p for p in (ext.domain, ext.suffix) if p).lower()
except Exception:  # pragma: no cover - fallback if tldextract unavailable
    def _registrable(host: str) -> str:
        parts = host.lower().split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Reject obvious non-leads and asset/tracking false positives.
JUNK_EMAIL_EXT = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js", ".ico",
)
JUNK_EMAIL_DOMAINS = (
    "example.com", "example.org", "domain.com", "yourdomain.com", "email.com",
    "sentry.io", "wixpress.com", "wix.com", "squarespace.com", "godaddy.com",
    "sentry-next.wixpress.com", "schema.org", "w3.org", "core.com",
)
JUNK_LOCAL_PARTS = ("no-reply", "noreply", "donotreply")

# Anchor hints that a link leads to a contact / about page worth fetching.
CONTACT_HINTS = (
    "contact", "kontakt", "contacto", "about", "about-us", "impressum",
    "team", "reach", "connect", "get-in-touch",
)

SOCIAL_PATTERNS = {
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/[A-Za-z0-9_.]+/?", re.I),
    "facebook": re.compile(r"https?://(?:www\.)?facebook\.com/[A-Za-z0-9_.\-/]+/?", re.I),
    "linkedin": re.compile(
        r"https?://(?:www\.)?linkedin\.com/(?:company|in)/[A-Za-z0-9_%\-]+/?", re.I
    ),
    "tiktok": re.compile(r"https?://(?:www\.)?tiktok\.com/@[A-Za-z0-9_.]+/?", re.I),
    "youtube": re.compile(
        r"https?://(?:www\.)?youtube\.com/(?:channel/|c/|user/|@)[A-Za-z0-9_.\-]+/?", re.I
    ),
}

# Handles that belong to the site builder / platform, not the business (e.g. a
# Wix or Squarespace template links the platform's own socials in the footer).
_JUNK_SOCIAL_HANDLES = {
    "wix", "squarespace", "godaddy", "wordpress", "wordpressdotcom", "shopify",
    "weebly", "duda", "webflow", "wixsite", "godaddysites", "elementor",
}
# Non-profile paths: share dialogs, tracking pixels, generic pages.
_JUNK_SOCIAL_PATHS = {
    "sharer", "sharer.php", "share", "share.php", "tr", "intent", "plugins",
    "dialog", "home.php", "login", "signup", "explore", "hashtag",
    "sharearticle", "sharing", "shareopengraph", "profile.php",
}


def _is_junk_social(url: str) -> bool:
    """Reject builder/platform socials and share/tracking links."""
    segs = [s.lstrip("@") for s in urlparse(url.lower()).path.strip("/").split("/") if s]
    if not segs:
        return True  # bare domain, no profile
    if any(s in _JUNK_SOCIAL_PATHS for s in segs):
        return True
    return segs[-1] in _JUNK_SOCIAL_HANDLES


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _decode_cfemail(hex_str: str) -> str:
    """Decode a Cloudflare-obfuscated email (data-cfemail attribute)."""
    try:
        key = int(hex_str[:2], 16)
        return "".join(
            chr(int(hex_str[i : i + 2], 16) ^ key)
            for i in range(2, len(hex_str), 2)
        )
    except Exception:
        return ""


def _deobfuscate(text: str) -> str:
    """Turn 'name [at] domain [dot] com' style text into real emails."""
    text = re.sub(r"\s*[\[(]\s*at\s*[\])]\s*", "@", text, flags=re.I)
    text = re.sub(r"\s+at\s+", "@", text, flags=re.I)
    text = re.sub(r"\s*[\[(]\s*dot\s*[\])]\s*", ".", text, flags=re.I)
    text = re.sub(r"\s+dot\s+", ".", text, flags=re.I)
    return text


def _clean_emails(emails: set[str], website: str) -> list[str]:
    site_host = urlparse(normalize_url(website)).netloc
    site_domain = _registrable(site_host) if site_host else ""
    kept: list[str] = []
    for e in emails:
        e = e.strip().strip(".").lower()
        if not e or e.count("@") != 1:
            continue
        local, _, domain = e.partition("@")
        if e.endswith(JUNK_EMAIL_EXT):
            continue
        if any(domain == d or domain.endswith("." + d) for d in JUNK_EMAIL_DOMAINS):
            continue
        if any(local.startswith(p) for p in JUNK_LOCAL_PARTS):
            continue
        if len(local) < 1 or "." not in domain:
            continue
        kept.append(e)
    kept = sorted(set(kept))
    # Prefer addresses on the business's own domain first.
    if site_domain:
        kept.sort(key=lambda e: 0 if e.endswith("@" + site_domain) or e.endswith("." + site_domain) else 1)
    return kept


def _extract_from_html(html: str, base_url: str):
    """Return (emails:set, socials:dict, phone:str) mined from one page."""
    emails: set[str] = set()
    socials: dict[str, str] = {}
    phone = ""

    soup = BeautifulSoup(html, "lxml")

    # Cloudflare-protected emails.
    for el in soup.select("[data-cfemail]"):
        dec = _decode_cfemail(el.get("data-cfemail", ""))
        if dec:
            emails.add(dec)

    # mailto: / tel: links.
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        if low.startswith("mailto:"):
            addr = href[7:].split("?")[0].strip()
            if addr:
                emails.add(addr)
        elif low.startswith("tel:") and not phone:
            phone = href[4:].strip()

    # Social profiles from any href (skip builder/share/tracking links).
    for a in soup.find_all("a", href=True):
        href = a["href"]
        for net, pat in SOCIAL_PATTERNS.items():
            if net not in socials:
                m = pat.search(href)
                if m and not _is_junk_social(m.group(0)):
                    socials[net] = m.group(0)

    # Emails in visible text (after light deobfuscation).
    text = _deobfuscate(soup.get_text(" ", strip=True))
    for m in EMAIL_RE.findall(text):
        emails.add(m)
    # Also scan raw HTML for social links that live in scripts/attrs.
    for net, pat in SOCIAL_PATTERNS.items():
        if net not in socials:
            for cand in pat.findall(html):
                url = cand if isinstance(cand, str) else cand[0]
                if not _is_junk_social(url):
                    socials[net] = url
                    break

    return emails, socials, phone


def _discover_contact_links(base_url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    base_host = _registrable(urlparse(base_url).netloc)
    found: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = (a.get_text(" ", strip=True) or "").lower()
        blob = (href + " " + text).lower()
        if not any(h in blob for h in CONTACT_HINTS):
            continue
        abs_url = urljoin(base_url, href)
        if not abs_url.startswith(("http://", "https://")):
            continue
        if _registrable(urlparse(abs_url).netloc) != base_host:
            continue
        abs_url = abs_url.split("#")[0]
        if abs_url in seen:
            continue
        seen.add(abs_url)
        found.append(abs_url)
    return found


async def _fetch(url: str, client: httpx.AsyncClient) -> str:
    try:
        r = await client.get(url)
        ctype = r.headers.get("content-type", "")
        if r.status_code == 200 and "html" in ctype.lower():
            return r.text
    except Exception:
        pass
    return ""


async def scrape_website(website: str, client: httpx.AsyncClient, max_pages: int = 4) -> dict:
    """Fetch homepage + a few contact pages and mine contact data.

    Returns {"emails": [...], "socials": {...}, "phone": str}.
    """
    base = normalize_url(website)
    result = {"emails": [], "socials": {}, "phone": ""}
    if not base:
        return result

    home = await _fetch(base, client)
    pages = [(base, home)]
    if home:
        for link in _discover_contact_links(base, home):
            if len(pages) >= max_pages:
                break
            pages.append((link, await _fetch(link, client)))

    all_emails: set[str] = set()
    socials: dict[str, str] = {}
    phone = ""
    for url, html in pages:
        if not html:
            continue
        e, s, p = _extract_from_html(html, url)
        all_emails |= e
        for k, v in s.items():
            socials.setdefault(k, v)
        if p and not phone:
            phone = p

    result["emails"] = _clean_emails(all_emails, base)
    result["socials"] = socials
    result["phone"] = phone
    return result


def make_client() -> httpx.AsyncClient:
    """A tolerant HTTP client tuned for small-business sites."""
    return httpx.AsyncClient(
        headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
        follow_redirects=True,
        timeout=httpx.Timeout(15.0, connect=10.0),
        verify=False,  # many small-biz sites have broken/expired certs
        # keepalive=0: close sockets promptly so no SSL transports are left with
        # pending overlapped ops at loop teardown (a Windows Proactor crash).
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=0),
    )
