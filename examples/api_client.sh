#!/usr/bin/env bash
# Call the docket HTTP API. Start it with `docket-api` or the Docker image.
# Usage: ./examples/api_client.sh invoice.pdf [more.pdf ...]
set -euo pipefail
API=${DOCKET_URL:-http://localhost:8000}
AUTH=(-H "Authorization: Bearer ${DOCKET_API_KEY:-}")

# 1. Synchronous: one document, wait for its DocumentResult.
curl -sf "${AUTH[@]}" -F "file=@$1" -F "document_type=invoice" "$API/process"
echo

# 2. Asynchronous batch: upload every file as one job, poll, download.
#    Idempotency-Key makes a retried upload return the same job.
files=()
for f in "$@"; do files+=(-F "files=@${f}"); done
job=$(curl -sf "${AUTH[@]}" -H "Idempotency-Key: $(cat "$@" | shasum -a 256 | cut -c1-32)" "${files[@]}" "$API/jobs")
id=$(echo "$job" | python3 -c "import json,sys; print(json.load(sys.stdin)['job_id'])")
while :; do
  state=$(curl -sf "${AUTH[@]}" "$API/jobs/$id" | python3 -c "import json,sys; j=json.load(sys.stdin); print(j['status'], j['counts']['done'], '/', j['counts']['total'])")
  echo "job $id: $state" >&2
  case "$state" in completed*|failed*) break ;; esac
  sleep 2
done
curl -sf "${AUTH[@]}" "$API/jobs/$id/results.csv" -o results.csv
curl -sf "${AUTH[@]}" "$API/jobs/$id/line-items.csv" -o line_items.csv
curl -sf "${AUTH[@]}" "$API/jobs/$id/results.jsonl" -o results.jsonl
echo "wrote results.csv, line_items.csv, results.jsonl" >&2
