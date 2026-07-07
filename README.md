# leadgen — free cold-outreach lead scraper

Pick a **business type** and a **location** at run time; get a CSV of leads with
**emails scraped live from each business's own website**. No paid APIs, no
monthly bill, no API keys.

This is the core of "find businesses → scrape their site for contact data"
pipelines (like the Reddit *business-finder* tool), built free.

## Install (one time)

```bash
pip install -r requirements.txt
python -m playwright install chromium   # only needed for --source gmaps
```

Requires Python 3.10+.

## Usage — web UI (easiest)

```bash
python app.py
```

Opens `http://127.0.0.1:5000` in your browser. Type a business type + location,
pick a source, click **Find leads**, watch the progress bar, then browse the
results table and **Download CSV**. Everything runs locally on your machine.

## Usage — command line

```bash
python leadgen.py --query "dentist" --location "Austin, Texas" --limit 100
```

Output → `leads.csv`, one row per business:

```
name, category, address, phone, website, emails, instagram, facebook,
linkedin, tiktok, youtube, rating, reviews, source
```

### Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--query` | *(required)* | Business type, e.g. `"real estate agency"` |
| `--location` | *(required)* | Place, e.g. `"Madrid, Spain"` |
| `--limit` | `50` | Max businesses to pull |
| `--source` | `gmaps` | `gmaps` (dense) or `osm` (clean) |
| `--out` | `leads.csv` | Output CSV path |
| `--concurrency` | `10` | Parallel website fetches |
| `--no-website-scrape` | off | Source data only, skip email mining |
| `--headful` | off | `gmaps`: show the browser (debugging) |

## The two sources

- **`gmaps`** — Google Maps via Playwright. Dense, current, most listings have a
  website, so the email fill-rate downstream is high. **Automated access to
  Google Maps is against Google's Terms of Service** — use at low volume for your
  own outreach. If Google changes its page markup, the selectors in
  `sources/gmaps.py` may need a tweak; run with `--headful` to see what's
  happening, or switch to `osm`.
- **`osm`** — OpenStreetMap via Nominatim + Overpass. Free, no key, ToS-clean,
  global, never bot-blocked. Coverage is sparser (fewer businesses carry a
  `website` tag), so expect fewer emails. Best for a compliant first pass.

## How it works

1. **Find businesses** (`sources/gmaps.py` or `sources/osm.py`) → name, website,
   phone, rating, category, address.
2. **Scrape each website** (`scrape_site.py`) → fetches homepage + likely contact
   pages, extracts emails (incl. `mailto:`, Cloudflare-obfuscated, and
   `name [at] domain` text), phone, and social profiles.
3. **Dedupe + write CSV** (`leadgen.py`, `models.py`).

## Sending responsibly (please read)

- Cold email to businesses is legal in many places **only with** an accurate
  sender identity, a real unsubscribe/opt-out, and honoring removals
  (US **CAN-SPAM**; EU/UK **GDPR** legitimate-interest + easy opt-out).
- Send **individually from your own inbox** — not bulk blasts. It's better for
  deliverability and keeps you on the right side of the rules.
- Respect `robots.txt` and each site's terms; keep volume low and human.

## Optional next step (not built yet)

AI review analysis + auto-drafted personalized emails per lead — doable free with
Google **Gemini's free tier** or a local **Ollama** model. Reviews require the
`gmaps` source. Ask if you want this layered on.
