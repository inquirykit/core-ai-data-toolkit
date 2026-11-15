#!/usr/bin/env sh
# Zip the project's own files: the ones git tracks plus new files it would
# track, so what .gitignore keeps out (.env, data/, .venv, node_modules, caches,
# logs, the local notes) stays out. Files go in as they are on disk, uncommitted
# edits included, under a top-level folder named after the repo.
#
#   scripts/make_zip.sh             # dist/ai_toolkit-<commit>.zip
#   scripts/make_zip.sh ~/out.zip
set -eu

root="$(cd "$(dirname "$0")/.." && pwd)"
name="$(basename "$root")"
version="$(git -C "$root" describe --always --dirty)"
out="${1:-$root/dist/$name-$version.zip}"

command -v zip >/dev/null 2>&1 || { echo "zip is needed (apt install zip)" >&2; exit 1; }

mkdir -p "$(dirname "$out")"
out="$(cd "$(dirname "$out")" && pwd)/$(basename "$out")"
rm -f "$out"
# From the parent, so every path starts with the folder's name. git still lists
# files deleted but not yet committed; those are skipped.
cd "$root/.."
git -C "$name" -c core.quotePath=false ls-files --cached --others --exclude-standard \
  | while IFS= read -r f; do [ -e "$name/$f" ] && printf '%s\n' "$name/$f"; done \
  | zip -q -y "$out" -@
echo "Wrote $out ($(du -h "$out" | cut -f1))"
