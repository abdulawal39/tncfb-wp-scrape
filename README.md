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
| `enrich.py` | Resumable homepage scanner over a single CSV. Confirms WP + scores each domain 0–11 for flipbook fit. |
| `enrich_batch.py` | Same scanner, but walks a folder of split CSVs and processes a fixed batch per run. Tracks a resume pointer across files. |
| `country.py` | Shared best-effort country detection (ccTLD + page-content signals). Used by `enrich_batch.py` and `backfill_country.py`. |
| `backfill_country.py` | Adds a `country` column (ccTLD-only) to enriched CSVs written before country detection existed. Idempotent. |
| `build_campaigns.py` | Buckets enriched leads by region into 100k-email campaign files. Incremental, dedupes emails. |

---

## Prerequisites

- macOS or Linux
- Python 3.10+
- `gcloud` + `bq` CLI: `brew install --cask google-cloud-sdk`
- A GCP project with billing enabled (new accounts get $300 free credit)
- `pip3 install httpx warcio requests`

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

Edit the SQL and delete the `LIMIT` line if present. Cost is the same — only
your CSV gets bigger (~200–400 MB for the full list).

For multi-million-row results the script writes a destination table, exports
gzipped CSV shards to a GCS bucket, then downloads + merges them locally —
streaming millions of rows directly through `bq query` is unreliable.

### 1d. (Optional) Split the big CSV into chunks

Easier to process in pieces, and required for `enrich_batch.py`:

```bash
mkdir -p wp_sites_split
HEADER=$(head -1 wp_sites_httparchive.csv)
tail -n +2 wp_sites_httparchive.csv | split -l 100000 -d -a 3 - wp_sites_split/part_
for f in wp_sites_split/part_*; do
  { echo "$HEADER"; cat "$f"; } > "${f}.csv" && rm "$f"
done
```

Produces `wp_sites_split/part_000.csv` … `part_NNN.csv`, 100k rows each,
header on every file.

---

## Step 2 — Enrich + score (resumable, daily batches)

`enrich.py` fetches each homepage, confirms WordPress is live, and scores
it for flipbook fit. It's **resumable**: stop anytime, re-run, and it
picks up where it left off.

### First run

```bash
python3 enrich.py \
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

### Alternative: batched run over the split folder

If you split the CSV in step 1d, use `enrich_batch.py` instead. It walks
the folder in sorted filename order, processes a fixed batch per run,
remembers a pointer across runs, and writes a new result file per run.

#### One-time setup

```bash
pip3 install 'httpx[http2]'
```

#### Each run (same command, every time)

```bash
python3 enrich_batch.py \
  --dir wp_sites_split \
  --out-dir enriched_splits \
  --batch 20000 \
  --workers 50
```

That's it. Run it once, run it again tomorrow, run it 200 times — every
invocation:

1. Reads `enriched_splits.progress.json` to find the resume pointer.
2. Reads `enriched_splits.tried.txt` to skip already-attempted domains.
3. Walks `wp_sites_split/` in sorted filename order from the pointer and
   takes the next `--batch` unseen domains (spilling across files).
4. Writes results into a fresh `enriched_splits/run_NNNN.csv`
   (auto-numbered — new file every run).
5. Mirrors that into `enriched_splits/with-emails/run_NNNN.csv` and
   `enriched_splits/without-emails/run_NNNN.csv` for easy filtering.
6. Advances the pointer **only after the batch finishes**, so Ctrl-C is
   safe (you keep what you've collected, no domain is skipped).

#### Output layout

```
enriched_splits/
├── run_0000.csv                       # all hits from run 0
├── run_0001.csv
├── with-emails/
│   ├── run_0000.csv                   # subset of run_0000 that has ≥1 email
│   └── run_0001.csv
└── without-emails/
    ├── run_0000.csv                   # subset of run_0000 with no email
    └── run_0001.csv
enriched_splits.tried.txt              # every domain attempted (lifetime)
enriched_splits.progress.json          # {"file": "part_003.csv", "row": 1742}
```

Don't delete or hand-edit the two state files unless you want to restart
or move the pointer. They're the only thing that prevents re-running
domains you've already paid time for.

#### Throughput

20000 domains at 50 workers ≈ 10–15 minutes per run (homepage fetch +
optional contact-page fallback for sites without a homepage email).
Full universe (~4.1M) ≈ 200+ runs.

#### Running it in a loop

**Run until done** — chew through everything sequentially in one sitting:

```bash
while python3 enrich_batch.py --dir wp_sites_split --out-dir enriched_splits \
                              --batch 20000 --workers 50; do :; done
```

**Run N times** — useful for an overnight batch where you want a hard cap
(e.g. 30 iterations × 20k = 600k domains):

```bash
for i in $(seq 1 30); do
  echo "=== iteration $i / 30 ==="
  python3 enrich_batch.py --dir wp_sites_split --out-dir enriched_splits \
                          --batch 20000 --workers 50 || break
done
```

The `|| break` makes the loop stop early if the script exits non-zero —
either because everything's been processed (exit code `2`) or because
something went wrong. Each iteration is a separate process with a fresh
`run_NNNN.csv`; the resume pointer survives across iterations exactly
the same way it survives across days.

**Logging each iteration to a file** — handy if you walk away:

```bash
for i in $(seq 1 30); do
  python3 enrich_batch.py --dir wp_sites_split --out-dir enriched_splits \
                          --batch 20000 --workers 50 \
    >> enrich.log 2>&1 || break
done
```

#### Email extraction

Each enriched row also gets an `emails` column (pipe-separated, up to 5
addresses) and an `email_source` column. The scan tries the homepage
first; if no email is found there, it follows up to 3 `contact`/`about`
links scraped from the homepage plus a few well-known fallback paths
(`/contact`, `/contact-us`, `/about`, `/about-us`). Best-fit emails
(those matching the site's own domain) sort first.

#### Country detection

Each enriched row gets a `country` column (ISO-3166 alpha-2, e.g. `GB`,
`DE`, blank if unknown). It's resolved in order of confidence:

1. **ccTLD** on the domain — `.co.uk`→GB, `.com.au`→AU, `.de`→DE … (authoritative)
2. **schema.org `addressCountry`** — scraped from the homepage *or* the
   contact page (reusing the email-hunt fetch, so no extra requests)
3. **`og:locale` → `<html lang>` → `tel:+<code>` → `£`** as weaker fallbacks

ccTLD covers roughly half the hits; the content signals fill in generic
`.com/.org/.net` sites where possible. Detection logic lives in
`country.py` and is shared with the backfill script.

To add the column to result files written **before** this feature existed
(ccTLD-only, since their HTML wasn't stored):

```bash
python3 backfill_country.py --dir enriched_splits
```

It's idempotent — files that already have a `country` column are skipped.

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

## Step 4 — Build campaign lists by region

`build_campaigns.py` turns the enriched `with-emails` output into
ready-to-send, region-bucketed campaign files.

```bash
python3 build_campaigns.py \
  --src enriched_splits/with-emails \
  --out campaigns_split \
  --per-file 100000
```

What it does:

- Reads every `enriched_splits/with-emails/run_*.csv`
- Buckets each lead by its `country` into a region
- **Explodes to one row per email address** (a site with `info@` and
  `sales@` becomes two rows)
- Writes region files capped at 100k emails each (`part_000.csv`, …)
- Columns: `email, domain, country, fit_score, vertical_match,
  has_flipbook_competitor, final_url`

Regions:

| Folder | Countries |
|---|---|
| `us_canada` | US, CA |
| `europe` | UK, IE, FR, DE, IT, ES, PT, NL, BE, LU, AT, CH, SE, NO, DK, FI, IS, PL, CZ, SK, HU, RO, BG, GR, HR, SI, EE, LV, LT, RS, UA, MT, CY + smaller European states |
| `apac_developed` | SG, JP, MY, KR, HK, TW, AU, NZ |
| `rest` | any other known country (BR, IN, ZA, …) |
| `unknown` | blank country (couldn't be geolocated) |

### Incremental — safe to re-run

Two state files in `--out` make re-runs additive:

- `.copied_domains.txt` — source domains already consumed
- `.seen_emails.txt` — every email already written (also dedupes
  addresses, so the same email never lands in two campaigns)

After enriching more leads, just re-run the same command. It skips
everything already copied and appends only new emails — topping up the
last partial file in each region before rolling to a new one.

Email addresses are validated and repaired before they're written
(URL-decoding `%20`, stripping `mailto:`/quotes/HTML, extracting the clean
address), so campaign files only ever contain importable emails.

### Cleaning already-built campaign files

If you built campaign files before validation existed and an import
rejected malformed addresses, run:

```bash
python3 clean_campaign_emails.py --dir campaigns_split
```

It removes invalid emails from every `part_*.csv` in place and writes the
repaired versions to `campaigns_split/fixed/<region>.csv` (deduped against
already-valid addresses) so you can re-import just the recovered ones.

---

## Alternative path — Common Crawl (free, no GCP)

If you can't or don't want to set up a GCP billing account:

```bash
python3 commoncrawl_wp.py --crawl CC-MAIN-2026-18 --workers 8 --target 100000
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
