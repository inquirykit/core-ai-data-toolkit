#!/usr/bin/env sh
# Create or update a job environment: JOB_ENVS_DIR/<name> (default envs/<name>),
# a virtualenv with requirements-<name>.txt. Jobs registered with env="<name>"
# run in its interpreter (see jobs/README.md).
#
#   scripts/setup_job_env.sh autogluon
#
# PYTHON picks its Python version (default 3.13; uv downloads it if missing).
set -eu

name="${1:?usage: $0 <env name>, e.g. autogluon}"
root="$(cd "$(dirname "$0")/.." && pwd)"
requirements="$root/requirements-$name.txt"
envs_dir="${JOB_ENVS_DIR:-$root/envs}"
target="$envs_dir/$name"
python="${PYTHON:-3.13}"

[ -f "$requirements" ] || { echo "No $requirements" >&2; exit 1; }
command -v uv >/dev/null 2>&1 || { echo "uv is needed: https://docs.astral.sh/uv/getting-started/installation/" >&2; exit 1; }

mkdir -p "$envs_dir"
[ -x "$target/bin/python" ] || uv venv --python "$python" "$target"
uv pip install --python "$target/bin/python" -r "$requirements"
# Anything the environment needs beyond packages (data downloads, say).
hook="$root/scripts/job_env_hooks/$name.py"
[ -f "$hook" ] && "$target/bin/python" "$hook"
echo "Ready: $target/bin/python"
