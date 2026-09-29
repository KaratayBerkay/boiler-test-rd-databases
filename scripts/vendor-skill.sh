#!/usr/bin/env bash
# Vendor one skill directory from a GitHub repo into skills/vendor/<owner>__<repo>__<skill> with a VENDORED.txt.
# Usage: scripts/vendor-skill.sh <owner/repo> <path/in/repo> <license> [dest-name]
set -euo pipefail
repo="$1"; path="$2"; license="$3"; name="${4:-$(basename "$path")}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
dest="$ROOT/skills/vendor/${repo/\//__}__${name}"
branch="$(gh api "repos/$repo" --jq .default_branch)"
mkdir -p "$dest"
gh api "repos/$repo/git/trees/$branch?recursive=1" --jq '.tree[] | select(.type=="blob") | .path' | grep -E "^$path/" | while read -r f; do
  rel="${f#"$path"/}"
  mkdir -p "$dest/$(dirname "$rel")"
  gh api "repos/$repo/contents/$f?ref=$branch" --jq '.content' | base64 -d > "$dest/$rel"
  echo "  $rel"
done
printf 'source: https://github.com/%s/tree/%s/%s\nlicense: %s\nvendored: %s\n' "$repo" "$branch" "$path" "$license" "$(date +%F)" > "$dest/VENDORED.txt"
echo "-> $dest"
