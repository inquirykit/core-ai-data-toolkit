#!/usr/bin/env sh
# Create or update a job environment: JOB_ENVS_DIR/<name> (default envs/<name>),
# a virtualenv with requirements-<name>.txt. Jobs registered with env="<name>"
# run in its interpreter (see jobs/README.md). The setup-env job (the Set up
# button in /admin) runs this script.
#
#   scripts/setup_job_env.sh autogluon
#
# PYTHON picks its Python version (default 3.13; uv downloads it if missing),
# UV the uv to use (default: the one on PATH). The environment is ready once
# its .ready file, a copy of the requirements it was installed from, is
# written: last, so a first install that stopped halfway is not taken for
# one. An update that fails keeps the .ready of the install before it: the
# environment goes on serving, and still shows as out of date. One whose
# interpreter no longer resolves (the Python uv kept for it was removed) is
# made again.
set -eu

name="${1:?usage: $0 <env name>, e.g. autogluon}"
root="$(cd "$(dirname "$0")/.." && pwd)"
requirements="$root/requirements-$name.txt"
envs_dir="${JOB_ENVS_DIR:-$root/envs}"
target="$envs_dir/$name"
python="${PYTHON:-3.13}"
uv="${UV:-uv}"

[ -f "$requirements" ] || { echo "No $requirements" >&2; exit 1; }
command -v "$uv" >/dev/null 2>&1 || { echo "uv is needed: https://docs.astral.sh/uv/getting-started/installation/" >&2; exit 1; }

mkdir -p "$envs_dir"
[ -x "$target/bin/python" ] || "$uv" venv --clear --python "$python" "$target"
"$uv" pip install --python "$target/bin/python" -r "$requirements"
cp "$requirements" "$target/.ready"
echo "Ready: $target/bin/python"
