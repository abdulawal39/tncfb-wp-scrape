# tncfb-wordpress-scrape (both-client pull)

Fresh WordPress lead-gen pipeline using the updated process: pulls BOTH
the desktop and mobile HTTP Archive crawls (deduped per domain), enriches
each domain (confirm live WordPress, emails from homepage + contact/about
pages, country detection, flipbook-fit scoring).

This folder is fully independent of the original root-level WP run — its
own data, state, and output. Nothing here disturbs that earlier progress.

## 1. Pull the universe

```bash
PROJECT=your-gcp-project DATE=2026-05-01 ./wordpress_run.sh
```

Output: `wordpress_sites.csv`.

## 2. Split into chunks

```bash
mkdir -p wordpress_sites_split
HEADER=$(head -1 wordpress_sites.csv)
tail -n +2 wordpress_sites.csv | split -l 100000 -d -a 3 - wordpress_sites_split/part_
for f in wordpress_sites_split/part_*; do
  { echo "$HEADER"; cat "$f"; } > "${f}.csv" && rm "$f"
done
```

## 3. Enrich (resumable, batched)

```bash
pip3 install 'httpx[http2]'
for i in $(seq 1 10); do
  python3 enrich_batch.py --dir wordpress_sites_split --out-dir enriched_splits \
                          --batch 20000 --workers 150 || break
done
```
