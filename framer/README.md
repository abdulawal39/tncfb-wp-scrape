# tncfb-framer-scrape

Lead-gen pipeline for **Framer** sites (mirror of the WordPress pipeline).
Pulls the Framer universe from HTTP Archive, then enriches each domain:
confirms it's live Framer, extracts emails (homepage + contact/about
pages), detects country, and scores flipbook fit.

## 1. Pull the universe

```bash
PROJECT=your-gcp-project DATE=2026-05-01 ./framer_run.sh
```

Output: `framer_sites.csv`.

## 2. Split into chunks

```bash
mkdir -p framer_sites_split
HEADER=$(head -1 framer_sites.csv)
tail -n +2 framer_sites.csv | split -l 100000 -d -a 3 - framer_sites_split/part_
for f in framer_sites_split/part_*; do
  { echo "$HEADER"; cat "$f"; } > "${f}.csv" && rm "$f"
done
```

## 3. Enrich (resumable, batched)

```bash
pip3 install 'httpx[http2]'
python3 enrich_batch.py --dir framer_sites_split --out-dir enriched_splits \
                        --batch 20000 --workers 150
```

Per-run output: `enriched_splits/run_NNNN.csv` (+ `with-emails/` and
`without-emails/` mirrors). State in `enriched_splits.tried.txt` and
`enriched_splits.progress.json`. Loop it:

```bash
for i in $(seq 1 10); do
  python3 enrich_batch.py --dir framer_sites_split --out-dir enriched_splits \
                          --batch 20000 --workers 150 || break
done
```
