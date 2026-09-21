#!/usr/bin/env bash
# Call the docket HTTP API. Start it with `docket-api` or the Docker image.
# Usage: ./examples/api_client.sh invoice.pdf
set -euo pipefail
API=${DOCKET_URL:-http://localhost:8000}
AUTH=(-H "Authorization: Bearer ${DOCKET_API_KEY:-}")
FILE=$1

# 1. Synchronous: upload and wait for the result (fine for small documents).
curl -sf "${AUTH[@]}" -F "file=@${FILE}" "$API/process"
echo

# 2. Asynchronous: submit a job, then poll. Idempotency-Key makes retries safe.
job=$(curl -sf "${AUTH[@]}" -H "Idempotency-Key: $(shasum -a 256 "$FILE" | cut -c1-32)" \
      -F "file=@${FILE}" "$API/jobs")
id=$(echo "$job" | python3 -c "import json,sys; print(json.load(sys.stdin)['job_id'])")
while :; do
  status=$(curl -sf "${AUTH[@]}" "$API/jobs/$id")
  state=$(echo "$status" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])")
  [ "$state" = completed ] || [ "$state" = failed ] && break
  sleep 2
done
echo "$status"
