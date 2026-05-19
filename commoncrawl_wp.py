"""
Stream Common Crawl WARC files, detect WordPress + flipbook-relevant signals,
emit a CSV of candidate domains.

How it works:
  - CC publishes a monthly crawl manifest at:
      https://data.commoncrawl.org/crawl-data/CC-MAIN-<id>/warc.paths.gz
  - Each WARC file (~1 GB gzipped) contains thousands of pages.
  - We stream-decode them with `warcio` and run cheap regexes on the HTML.
  - We DO NOT need to download whole WARCs to disk; we stream + scan + discard.

Usage:
  pip install warcio requests
  python commoncrawl_wp.py --crawl CC-MAIN-2026-18 --workers 8 --target 100000

Notes:
  - One WARC ≈ 35-50k pages. To hit 100k WP+catalog sites you'll likely need
    50–200 WARCs depending on hit rate (~1-2% of all sites are WP-with-PDF).
  - Bandwidth: ~1 GB per WARC. Plan for 50-200 GB egress total.
  - CC is free, no auth required, hosted on S3 + HTTPS mirror.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from warcio.archiveiterator import ArchiveIterator

CC_BASE = "https://data.commoncrawl.org"

# Cheap, conservative regexes. Operate on bytes to skip decoding cost.
RE_WP = re.compile(rb"/wp-content/|/wp-includes/|wp-json|generator[^>]+WordPress", re.I)
RE_PDF = re.compile(rb"\.pdf(\?|\"|')", re.I)
RE_CATALOG = re.compile(rb"catalog|catalogue|brochure|magazine|lookbook|menu", re.I)
RE_FLIP_COMPETITOR = re.compile(rb"issuu\.com|fliphtml5|3dflipbook|dflip|flipbook", re.I)

write_lock = threading.Lock()
seen_domains: set[str] = set()
seen_lock = threading.Lock()


def fetch_warc_paths(crawl_id: str) -> list[str]:
    url = f"{CC_BASE}/crawl-data/{crawl_id}/warc.paths.gz"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return gzip.decompress(r.content).decode().splitlines()


def scan_warc(warc_path: str, writer: csv.writer, target: int) -> int:
    """Stream one WARC, write hits, return count."""
    url = f"{CC_BASE}/{warc_path}"
    hits = 0
    try:
        with requests.get(url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            for record in ArchiveIterator(resp.raw):
                if record.rec_type != "response":
                    continue
                target_uri = record.rec_headers.get_header("WARC-Target-URI") or ""
                if not target_uri.startswith("http"):
                    continue

                # Read at most 256 KB — the head of an HTML page has all signals.
                body = record.content_stream().read(256 * 1024)
                if not body:
                    continue

                if not RE_WP.search(body):
                    continue
                has_pdf = bool(RE_PDF.search(body))
                has_cat = bool(RE_CATALOG.search(body))
                has_comp = bool(RE_FLIP_COMPETITOR.search(body))
                if not (has_pdf or has_cat or has_comp):
                    continue

                domain = urlparse(target_uri).netloc.lower().lstrip("www.")
                with seen_lock:
                    if domain in seen_domains:
                        continue
                    seen_domains.add(domain)
                    if len(seen_domains) > target:
                        return hits

                with write_lock:
                    writer.writerow([
                        domain, target_uri,
                        int(has_pdf), int(has_cat), int(has_comp),
                    ])
                hits += 1
    except Exception as e:
        print(f"[warn] {warc_path}: {e}", file=sys.stderr)
    return hits


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--crawl", required=True, help="e.g. CC-MAIN-2026-18")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--target", type=int, default=100_000)
    ap.add_argument("--max-warcs", type=int, default=300)
    ap.add_argument("--out", default="wp_sites_commoncrawl.csv")
    args = ap.parse_args()

    print(f"Fetching WARC manifest for {args.crawl}...", file=sys.stderr)
    paths = fetch_warc_paths(args.crawl)[: args.max_warcs]
    print(f"  {len(paths)} WARCs queued", file=sys.stderr)

    with open(args.out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["domain", "url", "has_pdf", "has_catalog_word", "has_flipbook_competitor"])

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(scan_warc, p, writer, args.target) for p in paths]
            for fut in as_completed(futures):
                fut.result()
                with seen_lock:
                    n = len(seen_domains)
                print(f"  progress: {n} unique domains", file=sys.stderr)
                if n >= args.target:
                    for f2 in futures:
                        f2.cancel()
                    break

    print(f"Done. {len(seen_domains)} domains -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
