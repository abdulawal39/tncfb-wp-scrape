"""
Batched enricher over a folder of split CSVs.

Walks the input folder in sorted order, processing N domains per run,
spilling across file boundaries as needed. Tracks progress in a state
file so the next run picks up exactly where the previous one stopped.

Usage:
  python enrich_batch.py \
      --dir wp_sites_split \
      --out wp_sites_enriched.csv \
      --batch 2000 \
      --workers 50

State files (next to --out):
  wp_sites_enriched.csv          -> hits (WordPress + at least one signal)
  wp_sites_enriched.tried.txt    -> every domain attempted (lifetime)
  wp_sites_enriched.progress.json-> {"file": "part_003.csv", "row": 1742}
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
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
        item = await queue.get()
        if item is None or stop_flag:
            queue.task_done()
            return
        domain = item
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


def load_tried(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with open(path) as f:
        return {ln.strip() for ln in f if ln.strip()}


def load_progress(path: Path) -> dict:
    if not path.exists():
        return {"file": None, "row": 0}
    return json.loads(path.read_text())


def save_progress(path: Path, file_name: str | None, row: int):
    path.write_text(json.dumps({"file": file_name, "row": row}))


def collect_todo(folder: Path, progress: dict, tried: set[str], batch: int):
    """Yield up to `batch` new domains in folder order, starting from progress.
    Returns (todo, end_file, end_row) where end_* is where to resume next time.
    """
    files = sorted(p for p in folder.glob("*.csv"))
    if not files:
        return [], None, 0

    start_file = progress.get("file")
    start_row = progress.get("row", 0)
    started = start_file is None

    todo: list[str] = []
    end_file: str | None = None
    end_row = 0

    for fp in files:
        if not started:
            if fp.name == start_file:
                started = True
            else:
                continue
        with open(fp) as f:
            reader = csv.DictReader(f)
            for idx, row in enumerate(reader):
                if fp.name == start_file and idx < start_row:
                    continue
                domain = (row.get("domain") or "").strip()
                end_file, end_row = fp.name, idx + 1
                if not domain or domain in tried:
                    continue
                todo.append(domain)
                if len(todo) >= batch:
                    return todo, end_file, end_row
    return todo, end_file, end_row


async def main_async(args):
    folder = Path(args.dir)
    out_path = Path(args.out)
    tried_path = out_path.with_suffix(".tried.txt")
    progress_path = out_path.with_suffix(".progress.json")

    tried = load_tried(tried_path)
    progress = load_progress(progress_path)
    print(f"Resuming: lifetime tried={len(tried)}, "
          f"pointer={progress.get('file')}:{progress.get('row')}",
          file=sys.stderr)

    todo, end_file, end_row = collect_todo(folder, progress, tried, args.batch)
    if not todo:
        print("Nothing new to process. (Pointer at end or all domains tried.)",
              file=sys.stderr)
        return

    print(f"This run: {len(todo)} new domains "
          f"(will advance pointer to {end_file}:{end_row})", file=sys.stderr)

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

    save_progress(progress_path, end_file, end_row)
    print(f"Done. {counters['tried']} tried this run, "
          f"{counters['hits']} new hits.", file=sys.stderr)
    print(f"Pointer advanced to {end_file}:{end_row}. "
          f"Lifetime tried: {len(tried) + counters['tried']}", file=sys.stderr)


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
    ap.add_argument("--dir", default="wp_sites_split",
                    help="Folder of split CSVs (sorted by filename)")
    ap.add_argument("--out", default="wp_sites_enriched.csv")
    ap.add_argument("--workers", type=int, default=50)
    ap.add_argument("--batch", type=int, default=2000,
                    help="Domains to process per invocation")
    args = ap.parse_args()
    install_sigint()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
