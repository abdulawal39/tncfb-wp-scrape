"""
Take a CSV of domains, fetch each homepage, confirm WordPress, and score
flipbook-fit. Resumable: safe to stop and re-run; picks up where it left off.

Usage:
  pip install httpx
  # First run (creates output + state files):
  python enrich.py --in wp_sites.csv --out wp_sites_enriched.csv --limit 200
  # Subsequent runs (resumes automatically):
  python enrich.py --in wp_sites.csv --out wp_sites_enriched.csv --limit 200

State files (auto-created next to --out):
  wp_sites_enriched.csv         -> hits only (WordPress + at least one signal)
  wp_sites_enriched.tried.txt   -> every domain attempted (one per line)

Re-runs read .tried.txt and skip those domains, so failed/non-WP sites
are not retried on the next day's batch.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import re
import signal
import sys
from pathlib import Path

import httpx

RE_WP = re.compile(r"/wp-content/|/wp-includes/|wp-json|generator[^>]+WordPress", re.I)
RE_PDF = re.compile(r"\.pdf(\?|\"|')", re.I)
RE_CATALOG = re.compile(r"catalog|catalogue|brochure|magazine|lookbook|menu", re.I)
RE_COMP = re.compile(r"issuu\.com|fliphtml5|3dflipbook|dflip|flipbook", re.I)
RE_VERTICAL = re.compile(
    r"real estate|realtor|property|dealership|car dealer|"
    r"travel agency|tour operator|restaurant|cafe|hotel|"
    r"publisher|magazine|university|school|museum|gallery",
    re.I,
)

FIELDS = ["domain", "final_url", "has_pdf", "has_catalog_word",
          "has_flipbook_competitor", "vertical_match", "fit_score"]

stop_flag = False


async def check(client: httpx.AsyncClient, domain: str) -> dict | None:
    for scheme in ("https://", "http://"):
        try:
            r = await client.get(scheme + domain, timeout=10, follow_redirects=True)
            if r.status_code >= 400:
                continue
            html = r.text[:200_000]
            if not RE_WP.search(html):
                return None
            has_pdf = bool(RE_PDF.search(html))
            has_cat = bool(RE_CATALOG.search(html))
            has_comp = bool(RE_COMP.search(html))
            vertical = RE_VERTICAL.search(html)
            score = (3 if has_pdf else 0) + (2 if has_cat else 0) \
                  + (4 if has_comp else 0) + (2 if vertical else 0)
            return {
                "domain": domain,
                "final_url": str(r.url),
                "has_pdf": int(has_pdf),
                "has_catalog_word": int(has_cat),
                "has_flipbook_competitor": int(has_comp),
                "vertical_match": vertical.group(0).lower() if vertical else "",
                "fit_score": score,
            }
        except Exception:
            continue
    return None


async def worker(queue, client, writer, tried_f, lock, counters):
    while True:
        domain = await queue.get()
        if domain is None or stop_flag:
            queue.task_done()
            return
        result = await check(client, domain)
        async with lock:
            tried_f.write(domain + "\n")
            tried_f.flush()
            counters["tried"] += 1
            if result:
                writer.writerow(result)
                counters["out_f"].flush()
                counters["hits"] += 1
            if counters["tried"] % 50 == 0:
                print(f"  {counters['tried']} tried, {counters['hits']} hits",
                      file=sys.stderr)
        queue.task_done()


def load_tried(tried_path: Path) -> set[str]:
    if not tried_path.exists():
        return set()
    with open(tried_path) as f:
        return {line.strip() for line in f if line.strip()}


async def main_async(args):
    out_path = Path(args.out)
    tried_path = out_path.with_suffix(".tried.txt")

    tried = load_tried(tried_path)
    print(f"Resuming: {len(tried)} domains previously tried", file=sys.stderr)

    with open(args.input) as f:
        reader = csv.DictReader(f)
        all_domains = [row["domain"].strip() for row in reader
                       if row.get("domain", "").strip()]
    todo = [d for d in all_domains if d not in tried]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(all_domains)} total / {len(todo)} new to process this run",
          file=sys.stderr)
    if not todo:
        print("Nothing to do.", file=sys.stderr)
        return

    out_is_new = not out_path.exists() or out_path.stat().st_size == 0
    out_f = open(out_path, "a", newline="")
    writer = csv.DictWriter(out_f, fieldnames=FIELDS)
    if out_is_new:
        writer.writeheader()
        out_f.flush()
    tried_f = open(tried_path, "a")

    lock = asyncio.Lock()
    counters = {"tried": 0, "hits": 0, "out_f": out_f}

    queue: asyncio.Queue = asyncio.Queue()
    for d in todo:
        queue.put_nowait(d)
    for _ in range(args.workers):
        queue.put_nowait(None)

    limits = httpx.Limits(max_connections=args.workers,
                          max_keepalive_connections=args.workers)
    headers = {"User-Agent": "Mozilla/5.0 (flipbook-fit-scanner)"}
    async with httpx.AsyncClient(limits=limits, headers=headers, http2=True) as client:
        workers = [asyncio.create_task(
                       worker(queue, client, writer, tried_f, lock, counters))
                   for _ in range(args.workers)]
        await asyncio.gather(*workers)

    out_f.close()
    tried_f.close()
    print(f"Done. {counters['tried']} tried this run, "
          f"{counters['hits']} new hits.", file=sys.stderr)
    print(f"Total tried lifetime: {len(tried) + counters['tried']}",
          file=sys.stderr)


def install_sigint():
    def handler(signum, frame):
        global stop_flag
        if stop_flag:
            sys.exit(1)
        stop_flag = True
        print("\nCtrl-C: finishing in-flight requests, then stopping...",
              file=sys.stderr)
    signal.signal(signal.SIGINT, handler)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="input", required=True)
    ap.add_argument("--out", default="wp_sites_enriched.csv")
    ap.add_argument("--workers", type=int, default=50)
    ap.add_argument("--limit", type=int, default=0,
                    help="Max new domains to process this run (0 = no limit)")
    args = ap.parse_args()
    install_sigint()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
