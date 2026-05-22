#!/usr/bin/env bash
# Pull Squarespace sites from HTTP Archive: query -> BigQuery table -> GCS CSV
# shards -> download + merge locally. Streaming millions of rows directly
# through `bq query` is unreliable, hence the table/export round-trip.
#
# Usage:
#   PROJECT=your-gcp-project DATE=2026-05-01 ./squarespace_run.sh

set -euo pipefail

: "${PROJECT:?set PROJECT=your-gcp-project}"
DATE="${DATE:-2026-05-01}"
OUT="${OUT:-squarespace_sites.csv}"
LOCATION="${LOCATION:-US}"
DATASET="${DATASET:-tncfb_squarespace}"
TABLE="${TABLE:-squarespace_${DATE//-/_}}"
BUCKET="${BUCKET:-${PROJECT}-tncfb-squarespace}"

SQL="$(sed "s/@DATE/${DATE}/g" squarespace.sql)"

echo "Estimating cost (dry run)..."
printf '%s\n' "$SQL" | bq query --project_id="$PROJECT" --use_legacy_sql=false --dry_run

read -r -p "Proceed with billed query? [y/N] " yn
[[ "$yn" == "y" || "$yn" == "Y" ]] || exit 0

echo "Ensuring dataset ${PROJECT}:${DATASET} (location=${LOCATION})..."
bq --project_id="$PROJECT" --location="$LOCATION" mk -f --dataset "${PROJECT}:${DATASET}" >/dev/null

echo "Ensuring GCS bucket gs://${BUCKET}..."
gsutil ls -b "gs://${BUCKET}" >/dev/null 2>&1 || \
  gsutil mb -p "$PROJECT" -l "$LOCATION" "gs://${BUCKET}"

echo "Running query -> ${PROJECT}:${DATASET}.${TABLE} ..."
printf '%s\n' "$SQL" | bq query \
  --project_id="$PROJECT" \
  --location="$LOCATION" \
  --use_legacy_sql=false \
  --destination_table="${PROJECT}:${DATASET}.${TABLE}" \
  --replace \
  --batch=false

echo "Exporting table to gs://${BUCKET}/${TABLE}/*.csv.gz ..."
bq extract \
  --project_id="$PROJECT" \
  --location="$LOCATION" \
  --destination_format=CSV \
  --compression=GZIP \
  --print_header=true \
  "${PROJECT}:${DATASET}.${TABLE}" \
  "gs://${BUCKET}/${TABLE}/part-*.csv.gz"

echo "Downloading shards..."
TMPDIR_LOCAL="$(mktemp -d)"
gsutil -m cp "gs://${BUCKET}/${TABLE}/part-*.csv.gz" "$TMPDIR_LOCAL/"

echo "Combining -> ${OUT}"
FIRST=1
: > "$OUT"
for f in "$TMPDIR_LOCAL"/part-*.csv.gz; do
  if [[ $FIRST -eq 1 ]]; then
    gunzip -c "$f" >> "$OUT"; FIRST=0
  else
    gunzip -c "$f" | tail -n +2 >> "$OUT"
  fi
done
rm -rf "$TMPDIR_LOCAL"

echo "Wrote $(wc -l < "$OUT") rows (including header) to $OUT"
echo "Next: split into chunks, then run enrich_batch.py (see README.md)."
