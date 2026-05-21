"""Add a `country` column to already-written enriched CSVs.

These files predate country detection and have no stored HTML, so country
is derived from the domain ccTLD only (generic TLDs stay blank). Idempotent:
files that already have a `country` column are left unchanged.

Usage:
  python3 backfill_country.py --dir enriched_splits
"""

from __future__ import annotations

import argparse
import csv
import sys
import tempfile
from pathlib import Path

from country import country_from_domain


def backfill_file(path: Path) -> tuple[int, int]:
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        if "country" in fields:
            return 0, 0  # already done
        rows = list(reader)

    out_fields = fields + ["country"]
    filled = 0
    for r in rows:
        c = country_from_domain(r.get("domain", ""))
        r["country"] = c
        if c:
            filled += 1

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with open(fd, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=out_fields)
        w.writeheader()
        w.writerows(rows)
    Path(tmp).replace(path)
    return len(rows), filled


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="enriched_splits")
    args = ap.parse_args()

    files = sorted(Path(args.dir).rglob("*.csv"))
    if not files:
        print(f"No CSVs under {args.dir}", file=sys.stderr)
        return

    total_rows = total_filled = touched = skipped = 0
    for fp in files:
        rows, filled = backfill_file(fp)
        if rows == 0 and filled == 0:
            skipped += 1
            continue
        touched += 1
        total_rows += rows
        total_filled += filled
        print(f"  {fp}: {filled}/{rows} got a country")

    print(f"\nDone. {touched} files updated, {skipped} already had the column.",
          file=sys.stderr)
    if total_rows:
        pct = 100 * total_filled // total_rows
        print(f"Country resolved for {total_filled}/{total_rows} rows "
              f"({pct}%) via ccTLD.", file=sys.stderr)


if __name__ == "__main__":
    main()
