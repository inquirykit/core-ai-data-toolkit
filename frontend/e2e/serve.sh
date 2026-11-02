#!/bin/sh
# The service the smoke test opens (frontend/e2e/smoke.e2e.ts): its own port, keys, settings and data, in a folder
# made afresh each run, so neither a developer's .env, nor a setting exported in their shell, nor their data reach it.
set -eu
root="$(cd "$(dirname "$0")/../.." && pwd)"
dir="${TMPDIR:-/tmp}/ai-toolkit-smoke"
rm -rf "$dir"
mkdir -p "$dir"
# Every setting env.example names is unset, so the file written below is all the service reads.
for name in $(sed -nE 's/^#? ?([A-Z][A-Z0-9_]+)=.*/\1/p' "$root/env.example" | sort -u); do
  unset "$name"
done
cat > "$dir/settings.env" <<EOF
HOST=127.0.0.1
PORT=${SMOKE_PORT:-8765}
API_KEY=smoke-client-key
ADMIN_API_KEY=smoke-admin-key
SETUP_WIZARD=false
DEFAULT_DEVICE=cpu
ARCADEDB_PATH=$dir/arcadedb
JOBS_DIR=$dir/jobs
JOB_DATA_DIR=$dir/datasets
JOB_ENVS_DIR=$dir/envs
ENRICHERS_DIR=$dir/enrichers
EVIDENCE_DIR=$dir/evidence
EVIDENCE_TSA_URL=
IMAGE_DIR=$dir/images
MODEL_SERVERS_FILE=$dir/model_servers.toml
MODEL_SERVERS_SAVED_FILE=$dir/model_servers.json
EOF
# Nothing goes to the Hugging Face Hub: the pages only read what is on disk.
export ENV_FILE="$dir/settings.env" HF_HUB_OFFLINE=1
cd "$root"
# The service's log (ArcadeDB's JVM is talkative) goes to a file, read when the service does not come up.
echo "smoke test service: http://127.0.0.1:${SMOKE_PORT:-8765}, its log in $dir/service.log" >&2
exec uv run --frozen python main.py > "$dir/service.log" 2>&1
