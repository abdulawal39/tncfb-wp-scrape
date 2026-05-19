# tncfb-wp-scrape

Tooling to build a list of WordPress sites that are plausible buyers of
**[TNC FlipBook 3D](https://tncflipbook.com)** — a WordPress plugin for
turning PDFs (catalogs, brochures, magazines, lookbooks, menus) into
interactive 3D page-flip experiences.

The pipeline:

1. **Pull every WordPress site on the web** from HTTP Archive's monthly
   BigQuery crawl (~3–5M domains) — costs about $0.10.
2. **Enrich each domain** by fetching its homepage and scoring it for
   flipbook-fit signals (PDF links, catalog vocabulary, competing flipbook
   tools already installed, vertical match).
3. **Sort by `fit_score`** and start outreach from the top.

Total out-of-pocket cost: under $1. Time: a day or two depending on
machine and patience.

---

## Files

| File | What it does |
|---|---|
| `httparchive_wp_cheap.sql` | Cheap BigQuery query — scans only the `technologies` column. ~$0.07. Recommended. |
| `httparchive_wp.sql` | Expensive variant that also regexes the HTML payload. ~$20–50. Skip unless you have a reason. |
| `httparchive_run.sh` | Wrapper that runs the SQL via `bq`, dry-runs first to show cost, then writes CSV. |
| `commoncrawl_wp.py` | Alternative: stream Common Crawl WARCs and detect WordPress by regex. Free but slow (50–200 GB download, ~24h). |
| `enrich.py` | Resumable homepage scanner. Confirms WP + scores each domain 0–11 for flipbook fit. |

---

## Prerequisites

- macOS or Linux
- Python 3.10+
- `gcloud` + `bq` CLI: `brew install --cask google-cloud-sdk`
- A GCP project with billing enabled (new accounts get $300 free credit)
- `pip install httpx warcio requests`

---

## Step 1 — Pull the WordPress universe (HTTP Archive)

### 1a. Auth + project setup

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
```

Find the latest crawl date:

```bash
bq query --use_legacy_sql=false \
  "SELECT DISTINCT date FROM httparchive.crawl.pages ORDER BY date DESC LIMIT 5"
```

### 1b. Run the cheap query

```bash
PROJECT=your-project DATE=2026-04-01 ./httparchive_run.sh
```

The script does a dry run first and shows the exact bytes that will be
scanned. Confirm `y` to proceed. Output: `wp_sites_httparchive.csv`.

The default `httparchive_run.sh` runs `httparchive_wp.sql`. To use the
cheap variant instead:

```bash
cp httparchive_wp_cheap.sql httparchive_wp.sql
```

### 1c. (Optional) Remove the row limit for the full universe

Edit the SQL and delete the `LIMIT 100000` line. Cost is the same — only
your CSV gets bigger (~200–400 MB for the full list).

---

## Step 2 — Enrich + score (resumable, daily batches)

`enrich.py` fetches each homepage, confirms WordPress is live, and scores
it for flipbook fit. It's **resumable**: stop anytime, re-run, and it
picks up where it left off.

### First run

```bash
python enrich.py \
  --in wp_sites_httparchive.csv \
  --out wp_sites_enriched.csv \
  --workers 50 \
  --limit 200
```

This processes the next 200 unseen domains. State is kept in:

- `wp_sites_enriched.csv` — every WordPress site with at least one fit signal
- `wp_sites_enriched.tried.txt` — every domain attempted (so failures are not retried)

### Daily run

Same command. It reads `.tried.txt`, skips those, processes the next 200.

### Bigger batches

`--limit 0` means no limit. On a decent connection 50 workers finish about
30–50 domains/second, so:

| Domains | Runtime (approx) |
|---|---|
| 200 | <1 min |
| 5,000 | 2–5 min |
| 100,000 | 1 hour |
| 3,000,000 | 24–30 hours |

For the full universe, run on a $5/month VPS overnight rather than your
laptop — saves your home IP from rate-limit attention and finishes faster.

### Ctrl-C is safe

One Ctrl-C = graceful stop (finishes in-flight requests). Two = hard exit.
Either way, state on disk is consistent.

### Scoring

| Signal | Points | Why |
|---|---|---|
| Uses Issuu / FlipHTML5 / 3DFlipBook / dFlip | +4 | Already paying for a competitor — warmest lead |
| `.pdf` link on homepage | +3 | Has the source material |
| Vertical match (real estate, dealer, hotel, school, museum, magazine, …) | +2 | Industry known to publish catalogs |
| "catalog" / "brochure" / "menu" vocabulary | +2 | Self-identifies the use case |

Max ~11. Anything ≥6 is a strong lead. Sort by `fit_score DESC`.

---

## Step 3 — Pick your outreach list

```bash
# Top 1000 warmest leads
sort -t, -k7 -nr wp_sites_enriched.csv | head -1000 > outreach.csv
```

Sites scoring 8+ are typically:

- Already using Issuu/FlipHTML5 (paying a competitor)
- Real estate / dealerships / publishers / hospitality
- Have `.pdf` links on their homepage

These convert best on cold outreach for a flipbook plugin.

---

## Alternative path — Common Crawl (free, no GCP)

If you can't or don't want to set up a GCP billing account:

```bash
python commoncrawl_wp.py --crawl CC-MAIN-2026-18 --workers 8 --target 100000
```

- $0 direct cost
- ~50–200 GB bandwidth
- ~12–24 hours
- Less reliable WP detection than HTTP Archive (regex vs Wappalyzer)

Output is a `wp_sites_commoncrawl.csv` compatible with `enrich.py`.

---

## Costs summary

| Step | Cost |
|---|---|
| HTTP Archive query (cheap) | ~$0.07 |
| HTTP Archive query (with payload regex) | ~$20–50 — avoid |
| Common Crawl | $0 + bandwidth |
| Enrichment (`enrich.py`) | $0 + ~200KB/domain bandwidth |
| **Total realistic** | **under $0.10** |

New GCP accounts have $300 credit + 1TB/month free BigQuery, so the first
run is effectively free.

---

## License

MIT. Use freely for outreach for TNC FlipBook 3D or any other purpose.
