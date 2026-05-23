"""Clean invalid emails out of campaign files.

For each campaigns_split/<region>/part_*.csv:
  - valid emails stay in place
  - invalid emails (backslashes, %-encoding, mailto:, surrounding HTML/quotes)
    are removed from the part file and, where repairable, written to
    campaigns_split/fixed/<region>.csv for re-import
  - unrepairable emails are dropped (counted)

Repaired emails are deduped against the region's already-valid emails, so
you only re-import addresses that weren't already imported successfully.

Usage:
  python3 clean_campaign_emails.py --dir campaigns_split
"""

from __future__ import annotations

import argparse
import csv
import re
import urllib.parse
from pathlib import Path

OUT_FIELDS = ["email", "domain", "country", "fit_score", "vertical_match",
              "has_flipbook_competitor", "final_url"]

VALID = re.compile(
    r"^[a-z0-9](?:[a-z0-9._+-]*[a-z0-9])?@"
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*"
    r"\.[a-z]{2,24}$"
)
EXTRACT = re.compile(
    r"[A-Za-z0-9._+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}"
)
STRIP_CHARS = "\"'<>()[]{} \t\r\n"

# Non-contact / placeholder domains that pass format checks but bounce or
# get rejected as invalid by ESPs.
BLOCK_DOMAINS = {"sentry.io", "wixpress.com", "domain.com", "email.com",
                 "yourdomain.com", "yourcompany.com"}
BLOCK_DOMAIN_SUFFIXES = (".calendar.google.com",)


def is_valid(email: str) -> bool:
    if not VALID.fullmatch(email) or "\\" in email:
        return False
    if ".." in email:                       # consecutive dots are invalid
        return False
    local, _, domain = email.partition("@")
    if domain in BLOCK_DOMAINS:
        return False
    if any(domain.endswith(s) for s in BLOCK_DOMAIN_SUFFIXES):
        return False
    if domain.split(".", 1)[0] == "example":  # example.com / example.co.jp ...
        return False
    return True


def repair(raw: str) -> str | None:
    s = raw.strip().strip(STRIP_CHARS)
    if s.lower().startswith("mailto:"):
        s = s[7:]
    try:
        s = urllib.parse.unquote(s)
    except Exception:
        pass
    s = s.strip().strip(STRIP_CHARS)
    m = EXTRACT.search(s)
    if not m:
        return None
    cand = m.group(0).rstrip(".").lower()
    return cand if is_valid(cand) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="campaigns_split")
    args = ap.parse_args()

    base = Path(args.dir)
    fixed_dir = base / "fixed"
    fixed_dir.mkdir(parents=True, exist_ok=True)

    regions = [d for d in sorted(base.iterdir())
               if d.is_dir() and d.name != "fixed"]

    grand = {"valid": 0, "repaired": 0, "dropped": 0}
    for region in regions:
        parts = sorted(region.glob("part_*.csv"))
        if not parts:
            continue
        # Pass 1: collect the set of already-valid emails in this region.
        valid_set: set[str] = set()
        for p in parts:
            with open(p) as f:
                for r in csv.DictReader(f):
                    e = (r.get("email") or "").strip().lower()
                    if is_valid(e):
                        valid_set.add(e)

        repaired_rows: list[dict] = []
        repaired_seen: set[str] = set()
        v = rep = drop = 0
        # Pass 2: rewrite each part file with valid rows only.
        for p in parts:
            with open(p) as f:
                rows = list(csv.DictReader(f))
            keep = []
            for r in rows:
                e = (r.get("email") or "").strip()
                if is_valid(e.lower()):
                    r["email"] = e.lower()
                    keep.append(r)
                    v += 1
                    continue
                fixed = repair(e)
                if fixed and fixed not in valid_set and fixed not in repaired_seen:
                    repaired_seen.add(fixed)
                    nr = dict(r)
                    nr["email"] = fixed
                    repaired_rows.append(nr)
                    rep += 1
                elif fixed:
                    # repairs to an address we already have — just drop it
                    drop += 1
                else:
                    drop += 1
            tmp = p.with_suffix(".tmp")
            with open(tmp, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
                w.writeheader()
                for r in keep:
                    w.writerow({k: r.get(k, "") for k in OUT_FIELDS})
            tmp.replace(p)

        if repaired_rows:
            fp = fixed_dir / f"{region.name}.csv"
            with open(fp, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
                w.writeheader()
                for r in repaired_rows:
                    w.writerow({k: r.get(k, "") for k in OUT_FIELDS})

        grand["valid"] += v
        grand["repaired"] += rep
        grand["dropped"] += drop
        print(f"{region.name:16} valid={v}  repaired={rep}  dropped={drop}"
              + (f"  -> fixed/{region.name}.csv" if repaired_rows else ""))

    print(f"\nTotals: {grand['valid']} valid kept, "
          f"{grand['repaired']} repaired (separate files), "
          f"{grand['dropped']} unfixable dropped.")


if __name__ == "__main__":
    main()
