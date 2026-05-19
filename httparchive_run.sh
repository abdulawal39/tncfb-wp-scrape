#!/usr/bin/env bash
# Run the HTTP Archive WordPress query via the `bq` CLI and save to CSV.
#
# Prereqs:
#   1. gcloud + bq installed:  https://cloud.google.com/sdk/docs/install
#   2. `gcloud auth login`     (interactive, run in your own terminal)
#   3. A GCP project with billing enabled. New accounts get $300 free credit
#      and 1 TB/month free BigQuery query quota — this query fits inside it.
#
# Usage:
#   PROJECT=your-gcp-project DATE=2026-04-01 ./httparchive_run.sh

set -euo pipefail

: "${PROJECT:?set PROJECT=your-gcp-project}"
DATE="${DATE:-2026-04-01}"
OUT="${OUT:-wp_sites_httparchive.csv}"

SQL="$(sed "s/@DATE/${DATE}/g" httparchive_wp.sql)"

echo "Estimating cost (dry run)..."
bq query --project_id="$PROJECT" --use_legacy_sql=false --dry_run "$SQL"

read -r -p "Proceed with billed query? [y/N] " yn
[[ "$yn" == "y" || "$yn" == "Y" ]] || exit 0

echo "Running query..."
bq query \
  --project_id="$PROJECT" \
  --use_legacy_sql=false \
  --format=csv \
  --max_rows=100000 \
  "$SQL" > "$OUT"

echo "Wrote $(wc -l < "$OUT") rows to $OUT"
