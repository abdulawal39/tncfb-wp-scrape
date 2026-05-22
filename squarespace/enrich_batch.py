"""
Batched enricher over a folder of split CSVs.

Walks the input folder in sorted order, processing N domains per run,
spilling across file boundaries as needed. Tracks progress in a state
file so the next run picks up exactly where the previous one stopped.

Each run writes its results to a new file in --out-dir, sequentially
numbered (run_0000.csv, run_0001.csv, ...). State (lifetime tried + resume
pointer) lives alongside --out-dir.

Usage:
  python enrich_batch.py \
      --dir squarespace_sites_split \
      --out-dir enriched_splits \
      --batch 20000 \
      --workers 50

State files (siblings of --out-dir):
  enriched_splits.tried.txt    -> every domain attempted (lifetime)
  enriched_splits.progress.json-> {"file": "part_003.csv", "row": 1742}
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

from country import detect_country

RE_PLATFORM = re.compile(r"static1\.squarespace\.com|squarespace-cdn|sqsp\.net|SQUARESPACE_CONTEXT", re.I)
RE_PDF = re.compile(r"\.pdf(\?|\"|')", re.I)
RE_CATALOG = re.compile(r"catalog|catalogue|brochure|magazine|lookbook|menu", re.I)
RE_COMP = re.compile(r"issuu\.com|fliphtml5|3dflipbook|dflip|flipbook", re.I)
RE_VERTICAL = re.compile(
    r"real estate|realtor|property|dealership|car dealer|"
    r"travel agency|tour operator|restaurant|cafe|hotel|"
    r"publisher|magazine|university|school|museum|gallery",
    re.I,
)
RE_MAILTO = re.compile(r'mailto:([^"\'?\s>]+)', re.I)
RE_EMAIL = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}\b"
)
RE_CONTACT_LINK = re.compile(
    r'href=["\']([^"\']*(?:contact|about)[^"\']*)["\']',
    re.I,
)
EMAIL_JUNK_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js",
)
EMAIL_JUNK_LOCALS = {
    "sentry", "wixpress", "example", "email", "you", "your", "name",
    "user", "username", "test", "noreply", "no-reply", "donotreply",
}
CONTACT_FALLBACK_PATHS = (
    "/contact", "/contact-us", "/contact/", "/contact-us/",
    "/about", "/about-us", "/about/", "/about-us/",
)

FIELDS = ["domain", "final_url", "has_pdf", "has_catalog_word",
          "has_flipbook_competitor", "vertical_match", "fit_score",
          "emails", "email_source", "country"]


def extract_emails(html: str, domain: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for m in RE_MAILTO.finditer(html):
        c = m.group(1).split("?", 1)[0].strip().lower()
        if c and c not in seen and _email_ok(c):
            seen.add(c)
            found.append(c)
    for m in RE_EMAIL.finditer(html):
        c = m.group(0).strip().lower()
        if c not in seen and _email_ok(c):
            seen.add(c)
            found.append(c)
    found.sort(key=lambda e: (0 if e.endswith("@" + domain) else 1, e))
    return found[:5]


def _email_ok(addr: str) -> bool:
    if "@" not in addr:
        return False
    local, _, host = addr.partition("@")
    if not local or not host or "." not in host:
        return False
    if any(addr.endswith(s) for s in EMAIL_JUNK_SUFFIXES):
        return False
    if local in EMAIL_JUNK_LOCALS:
        return False
    if any(c in addr for c in "<>()[]{}"):
        return False
    return True


def find_contact_urls(html: str, base_url: str) -> list[str]:
    """Pull /contact and /about hrefs from homepage HTML, dedup, cap."""
    out: list[str] = []
    seen: set[str] = set()
    for m in RE_CONTACT_LINK.finditer(html):
        href = m.group(1).strip()
        if not href or href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        url = _absolutize(href, base_url)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
            if len(out) >= 3:
                break
    return out


def _absolutize(href: str, base_url: str) -> str | None:
    try:
        from urllib.parse import urljoin, urlparse
        u = urljoin(base_url, href)
        p = urlparse(u)
        if p.scheme not in ("http", "https"):
            return None
        if not p.netloc:
            return None
        return u
    except Exception:
        return None


stop_flag = False


async def fetch(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        r = await client.get(url, timeout=10, follow_redirects=True)
        if r.status_code >= 400:
            return None
        return r.text[:200_000]
    except Exception:
        return None


async def hunt_contact_emails(
    client: httpx.AsyncClient, homepage_html: str, base_url: str, domain: str
) -> tuple[list[str], str, str]:
    """Try contact/about links discovered on the homepage, then well-known
    fallback paths. Return (emails, source_url_or_label, contact_html). The
    contact_html is from the email-yielding page, else the last page fetched
    — useful for country detection (address blocks live on contact pages)."""
    candidates: list[str] = []
    seen: set[str] = set()
    for u in find_contact_urls(homepage_html, base_url):
        if u not in seen:
            seen.add(u)
            candidates.append(u)
    for path in CONTACT_FALLBACK_PATHS:
        u = base_url.rstrip("/") + path
        if u not in seen:
            seen.add(u)
            candidates.append(u)

    last_html = ""
    for url in candidates[:4]:
        html = await fetch(client, url)
        if not html:
            continue
        last_html = html
        emails = extract_emails(html, domain)
        if emails:
            return emails, url, html
    return [], "", last_html


async def check(client: httpx.AsyncClient, domain: str) -> dict | None:
    for scheme in ("https://", "http://"):
        try:
            r = await client.get(scheme + domain, timeout=10, follow_redirects=True)
            if r.status_code >= 400:
                continue
            html = r.text[:200_000]
            if not RE_PLATFORM.search(html):
                return None
            has_pdf = bool(RE_PDF.search(html))
            has_cat = bool(RE_CATALOG.search(html))
            has_comp = bool(RE_COMP.search(html))
            vertical = RE_VERTICAL.search(html)
            score = (3 if has_pdf else 0) + (2 if has_cat else 0) \
                  + (4 if has_comp else 0) + (2 if vertical else 0)

            final_url = str(r.url)
            emails = extract_emails(html, domain)
            email_source = "homepage" if emails else ""
            contact_html = ""
            if not emails:
                emails, source_url, contact_html = await hunt_contact_emails(
                    client, html, final_url, domain
                )
                if emails:
                    email_source = source_url

            return {
                "domain": domain,
                "final_url": final_url,
                "has_pdf": int(has_pdf),
                "has_catalog_word": int(has_cat),
                "has_flipbook_competitor": int(has_comp),
                "vertical_match": vertical.group(0).lower() if vertical else "",
                "fit_score": score,
                "emails": "|".join(emails),
                "email_source": email_source,
                "country": detect_country(domain, html, contact_html),
            }
        except Exception:
            continue
    return None


async def worker(queue, client, writer, email_writer, noemail_writer,
                 tried_f, lock, counters):
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
                if result["emails"]:
                    email_writer.writerow(result)
                    counters["email_f"].flush()
                    counters["with_email"] += 1
                else:
                    noemail_writer.writerow(result)
                    counters["noemail_f"].flush()
            if counters["tried"] % 100 == 0:
                print(f"  {counters['tried']} tried, {counters['hits']} hits, "
                      f"{counters['with_email']} w/email", file=sys.stderr)
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


def next_run_path(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(out_dir.glob("run_*.csv"))
    if not existing:
        n = 0
    else:
        last = existing[-1].stem.split("_", 1)[1]
        try:
            n = int(last) + 1
        except ValueError:
            n = len(existing)
    return out_dir / f"run_{n:04d}.csv"


def collect_todo(folder: Path, progress: dict, tried: set[str], batch: int):
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
    out_dir = Path(args.out_dir)
    tried_path = out_dir.with_suffix(".tried.txt")
    progress_path = out_dir.with_suffix(".progress.json")

    tried = load_tried(tried_path)
    progress = load_progress(progress_path)
    print(f"Resuming: lifetime tried={len(tried)}, "
          f"pointer={progress.get('file')}:{progress.get('row')}",
          file=sys.stderr)

    todo, end_file, end_row = collect_todo(folder, progress, tried, args.batch)
    if not todo:
        print("Nothing new to process. (Pointer at end or all domains tried.)",
              file=sys.stderr)
        sys.exit(2)

    out_path = next_run_path(out_dir)
    email_dir = out_dir / "with-emails"
    noemail_dir = out_dir / "without-emails"
    email_dir.mkdir(parents=True, exist_ok=True)
    noemail_dir.mkdir(parents=True, exist_ok=True)
    email_path = email_dir / out_path.name
    noemail_path = noemail_dir / out_path.name
    print(f"This run: {len(todo)} new domains → {out_path}", file=sys.stderr)
    print(f"  with-emails    → {email_path}", file=sys.stderr)
    print(f"  without-emails → {noemail_path}", file=sys.stderr)
    print(f"  (pointer will advance to {end_file}:{end_row})", file=sys.stderr)

    out_f = open(out_path, "w", newline="")
    writer = csv.DictWriter(out_f, fieldnames=FIELDS)
    writer.writeheader()
    out_f.flush()
    email_f = open(email_path, "w", newline="")
    email_writer = csv.DictWriter(email_f, fieldnames=FIELDS)
    email_writer.writeheader()
    email_f.flush()
    noemail_f = open(noemail_path, "w", newline="")
    noemail_writer = csv.DictWriter(noemail_f, fieldnames=FIELDS)
    noemail_writer.writeheader()
    noemail_f.flush()
    tried_f = open(tried_path, "a")

    lock = asyncio.Lock()
    counters = {"tried": 0, "hits": 0, "with_email": 0,
                "out_f": out_f, "email_f": email_f, "noemail_f": noemail_f}

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
                       worker(queue, client, writer, email_writer,
                              noemail_writer, tried_f, lock, counters))
                   for _ in range(args.workers)]
        await asyncio.gather(*workers)

    out_f.close()
    email_f.close()
    noemail_f.close()
    tried_f.close()

    save_progress(progress_path, end_file, end_row)
    print(f"Done. {counters['tried']} tried, {counters['hits']} hits, "
          f"{counters['with_email']} with email → {out_path}", file=sys.stderr)
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
    ap.add_argument("--dir", default="squarespace_sites_split",
                    help="Folder of split CSVs (sorted by filename)")
    ap.add_argument("--out-dir", default="enriched_splits",
                    help="Folder for per-run result CSVs (run_NNNN.csv)")
    ap.add_argument("--workers", type=int, default=50)
    ap.add_argument("--batch", type=int, default=20000,
                    help="Domains to process per invocation")
    args = ap.parse_args()
    install_sigint()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
