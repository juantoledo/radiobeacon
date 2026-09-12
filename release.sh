#!/usr/bin/env bash
# Cuts a new radiobeacon release: bumps the single project-wide version
# (VERSION at the repo root — see adapters/version.py for how every
# component reads it back), keeps the two packaged components'
# pyproject.toml version fields in sync, records the release in
# CHANGELOG.md, commits, and tags. Manual semver bump on purpose — this
# repo has no CI-derived versioning (no conventional-commit parsing), so
# you decide patch/minor/major yourself, same as you'd decide it for any
# other project with a low, human-reviewed commit volume.
#
# Usage: ./release.sh patch|minor|major
#
# Does NOT push — `git push && git push --tags` is left to you, since
# pushing a tag is what triggers .github/workflows/release.yml to publish
# a GitHub Release, and that's a shared/visible action worth doing
# deliberately rather than as a side effect of running this script.
set -euo pipefail
cd "$(dirname "$0")"

bump="${1:-}"
case "$bump" in
  patch|minor|major) ;;
  *)
    echo "usage: $0 patch|minor|major" >&2
    exit 1
    ;;
esac

if [ -n "$(git status --porcelain)" ]; then
  echo "error: working tree is not clean — commit or stash your changes first" >&2
  exit 1
fi

current_branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "$current_branch" != "main" ]; then
  echo "error: releases are cut from main, currently on '$current_branch'" >&2
  exit 1
fi

current_version="$(cat VERSION)"
IFS='.' read -r major minor patch <<< "$current_version"

case "$bump" in
  patch) patch=$((patch + 1)) ;;
  minor) minor=$((minor + 1)); patch=0 ;;
  major) major=$((major + 1)); minor=0; patch=0 ;;
esac

new_version="${major}.${minor}.${patch}"
new_tag="v${new_version}"

if git rev-parse "$new_tag" >/dev/null 2>&1; then
  echo "error: tag $new_tag already exists" >&2
  exit 1
fi

echo "== releasing $new_tag (was v$current_version) =="

echo "$new_version" > VERSION

# Both pyproject.toml files start each `version = "..."` line the same
# way, so an anchored substitution can't cross into an unrelated field
# (e.g. requires-python's own quoted string).
for f in data-adapters/pyproject.toml dispatcher/pyproject.toml; do
  sed -i.bak -E "s/^version = \"[^\"]+\"/version = \"${new_version}\"/" "$f"
  rm -f "$f.bak"
done

previous_tag="$(git describe --tags --abbrev=0 2>/dev/null || true)"
if [ -n "$previous_tag" ]; then
  log_range="${previous_tag}..HEAD"
else
  log_range="HEAD"
fi

changelog_entry="$(mktemp)"
trap 'rm -f "$changelog_entry"' EXIT

{
  echo "## ${new_tag} — $(date +%Y-%m-%d)"
  echo
  git log --reverse --pretty='format:- %s' "$log_range"
  echo
  echo
} > "$changelog_entry"

# Insert the new section right before the first existing "## " release
# heading (falling back to end-of-file when there isn't one yet) rather
# than at the very top of the file, so CHANGELOG.md's own leading
# explanation of what it is stays first.
insert_line="$(grep -n '^## ' CHANGELOG.md | head -1 | cut -d: -f1)"
insert_line="${insert_line:-$(($(wc -l < CHANGELOG.md) + 1))}"
{
  head -n "$((insert_line - 1))" CHANGELOG.md
  cat "$changelog_entry"
  tail -n "+${insert_line}" CHANGELOG.md
} > CHANGELOG.md.new
mv CHANGELOG.md.new CHANGELOG.md

git add VERSION CHANGELOG.md data-adapters/pyproject.toml dispatcher/pyproject.toml
git commit -m "chore(release): ${new_tag}"
git tag -a "$new_tag" -m "$new_tag"

echo "== done: committed and tagged ${new_tag} locally =="
echo "== push when ready: git push && git push --tags =="
