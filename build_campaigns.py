"""Build cold-email campaign lists from enriched with-emails CSVs.

- Reads every enriched_splits/with-emails/run_*.csv
- Buckets each lead by country into a region (us_canada / europe /
  apac_developed / rest)
- Explodes to ONE row per email address
- Writes region files capped at 100k email rows each (part_000.csv, ...)
- Incremental: remembers which source domains were already consumed and
  which email addresses were already written, so re-runs only add new ones.

Usage:
  python3 build_campaigns.py \
      --src enriched_splits/with-emails \
      --out campaigns_split \
      --per-file 100000
"""

from __future__ import annotations

import argparse
import csv
import glob
from pathlib import Path

US_CA = {"US", "CA"}
EUROPE = {
    "GB", "IE", "FR", "DE", "IT", "ES", "PT", "NL", "BE", "LU", "AT", "CH",
    "SE", "NO", "DK", "FI", "IS", "PL", "CZ", "SK", "HU", "RO", "BG", "GR",
    "HR", "SI", "EE", "LV", "LT", "RS", "UA", "MT", "CY", "AL", "BA", "MK",
    "ME", "MD", "LI", "AD", "MC", "SM", "BY", "XK",
}
APAC_DEV = {"SG", "JP", "MY", "KR", "HK", "TW", "AU", "NZ"}

OUT_FIELDS = ["email", "domain", "country", "fit_score", "vertical_match",
              "has_flipbook_competitor", "final_url"]


def region_of(country: str) -> str:
    c = (country or "").strip().upper()
    if not c:
        return "unknown"
    if c in US_CA:
        return "us_canada"
    if c in EUROPE:
        return "europe"
    if c in APAC_DEV:
        return "apac_developed"
    return "rest"


def load_lines(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with open(path) as f:
        return {ln.strip() for ln in f if ln.strip()}


class RegionWriter:
    """Appends rows to region/part_NNN.csv, rolling at `cap` rows per file."""

    def __init__(self, region_dir: Path, cap: int):
        self.dir = region_dir
        self.cap = cap
        self.dir.mkdir(parents=True, exist_ok=True)
        parts = sorted(self.dir.glob("part_*.csv"))
        if parts:
            last = parts[-1]
            n = sum(1 for _ in open(last)) - 1  # minus header
            if n < cap:
                self.idx = int(last.stem.split("_")[1])
                self.count = n
                self.f = open(last, "a", newline="")
                self.w = csv.DictWriter(self.f, fieldnames=OUT_FIELDS)
                return
            self.idx = int(last.stem.split("_")[1]) + 1
        else:
            self.idx = 0
        self.count = 0
        self.f = None
        self.w = None

    def _open_new(self):
        if self.f:
            self.f.close()
        p = self.dir / f"part_{self.idx:03d}.csv"
        self.f = open(p, "w", newline="")
        self.w = csv.DictWriter(self.f, fieldnames=OUT_FIELDS)
        self.w.writeheader()
        self.count = 0

    def write(self, row: dict):
        if self.w is None or self.count >= self.cap:
            if self.w is not None and self.count >= self.cap:
                self.idx += 1
            self._open_new()
        self.w.writerow(row)
        self.count += 1

    def close(self):
        if self.f:
            self.f.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="enriched_splits/with-emails")
    ap.add_argument("--out", default="campaigns_split")
    ap.add_argument("--per-file", type=int, default=100000)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    copied_path = out / ".copied_domains.txt"
    seen_path = out / ".seen_emails.txt"

    copied = load_lines(copied_path)
    seen = load_lines(seen_path)
    print(f"State: {len(copied)} domains already consumed, "
          f"{len(seen)} emails already written")

    writers: dict[str, RegionWriter] = {}
    new_domains: list[str] = []
    new_emails: list[str] = []
    region_counts: dict[str, int] = {}

    src_files = sorted(glob.glob(str(Path(args.src) / "run_*.csv")))
    for sf in src_files:
        with open(sf) as f:
            for r in csv.DictReader(f):
                domain = (r.get("domain") or "").strip()
                if not domain or domain in copied:
                    continue
                copied.add(domain)
                new_domains.append(domain)
                emails = [e for e in (r.get("emails") or "").split("|") if e]
                if not emails:
                    continue
                region = region_of(r.get("country", ""))
                for email in emails:
                    email = email.strip().lower()
                    if not email or email in seen:
                        continue
                    seen.add(email)
                    new_emails.append(email)
                    if region not in writers:
                        writers[region] = RegionWriter(out / region, args.per_file)
                    writers[region].write({
                        "email": email,
                        "domain": domain,
                        "country": r.get("country", ""),
                        "fit_score": r.get("fit_score", ""),
                        "vertical_match": r.get("vertical_match", ""),
                        "has_flipbook_competitor": r.get("has_flipbook_competitor", ""),
                        "final_url": r.get("final_url", ""),
                    })
                    region_counts[region] = region_counts.get(region, 0) + 1

    for w in writers.values():
        w.close()

    # Persist state (append new, keep file as full set)
    with open(copied_path, "a") as f:
        for d in new_domains:
            f.write(d + "\n")
    with open(seen_path, "a") as f:
        for e in new_emails:
            f.write(e + "\n")

    print(f"\nThis run: {len(new_domains)} new domains, "
          f"{len(new_emails)} new emails added.")
    if region_counts:
        for region in ("us_canada", "europe", "apac_developed", "rest", "unknown"):
            if region in region_counts:
                print(f"  {region:16} +{region_counts[region]} emails")
    print(f"Totals now: {len(copied)} domains consumed, "
          f"{len(seen)} emails across all campaign files.")


if __name__ == "__main__":
    main()
