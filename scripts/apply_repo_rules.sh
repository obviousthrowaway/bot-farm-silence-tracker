#!/usr/bin/env bash
# Applies the main branch's ruleset (.github/rulesets/main.json) to a GitHub
# repository, creating it or updating the ruleset with the same name, so it is safe to
# run again. See "Contributing" in the README.
#
# Usage: scripts/apply_repo_rules.sh [--dry-run] <owner/repo>
#
# --dry-run prints the payload and what would be done, and calls no API.
# Otherwise it needs the GitHub CLI (gh), logged in as an admin of the repository,
# and python3 to read the ruleset file.
set -euo pipefail

usage() {
  echo "usage: $0 [--dry-run] <owner/repo>" >&2
  exit 2
}

dry_run=false
repo=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry_run=true ;;
    -h | --help) usage ;;
    -*) echo "unknown option: $arg" >&2; usage ;;
    *)
      if [ -n "$repo" ]; then
        usage
      fi
      repo="$arg"
      ;;
  esac
done
if [ -z "$repo" ]; then
  usage
fi
if ! printf '%s' "$repo" | grep -Eq '^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$'; then
  echo "not an owner/repo name: $repo" >&2
  exit 2
fi

root="$(cd "$(dirname "$0")/.." && pwd)"
ruleset="$root/.github/rulesets/main.json"
if [ ! -f "$ruleset" ]; then
  echo "missing ruleset file: $ruleset" >&2
  exit 1
fi

# The owner placeholder, written in two parts so that the replace command in
# "Publishing your copy" (README) rewrites CODEOWNERS but leaves this check intact.
placeholder="_""_OWNER__"
codeowners="$root/.github/CODEOWNERS"
if [ -f "$codeowners" ] && grep -qF "$placeholder" "$codeowners"; then
  echo "warning: .github/CODEOWNERS still names the @$placeholder placeholder. GitHub cannot resolve it, so it owns nothing and no file needs a code owner's review. Replace it first (see \"Publishing your copy\" in the README)." >&2
fi

# The ruleset file must be valid JSON with a name of plain characters (it is used in
# a jq filter below).
name="$(python3 -c '
import json, re, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
name = data.get("name")
if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9 ._-]+", name):
    sys.exit("the ruleset needs a name of letters, digits, spaces and . _ -")
print(name)
' "$ruleset")"

if [ "$dry_run" = true ]; then
  echo "Dry run: would create or update the ruleset \"$name\" on $repo"
  echo "(POST repos/$repo/rulesets, or PUT repos/$repo/rulesets/<id> when it exists)."
  echo "Payload:"
  cat "$ruleset"
  exit 0
fi

if ! command -v gh > /dev/null 2>&1; then
  echo "the GitHub CLI (gh) is needed" >&2
  exit 1
fi

ids="$(gh api --paginate "repos/$repo/rulesets?includes_parents=false&per_page=100" \
  --jq ".[] | select(.name == \"$name\" and .source_type == \"Repository\") | .id")"
count="$(printf '%s' "$ids" | grep -c . || true)"
if [ "$count" -gt 1 ]; then
  echo "more than one ruleset is named \"$name\" on $repo; remove the extra ones first" >&2
  exit 1
fi

if [ "$count" -eq 0 ]; then
  echo "Creating the ruleset \"$name\" on $repo."
  id="$(gh api --method POST "repos/$repo/rulesets" --input "$ruleset" --jq .id)"
else
  id="$ids"
  echo "Updating the ruleset \"$name\" (id $id) on $repo."
  gh api --method PUT "repos/$repo/rulesets/$id" --input "$ruleset" --jq .id > /dev/null
fi

gh api "repos/$repo/rulesets/$id" --jq '
  "Ruleset \(.name) (id \(.id)): \(.enforcement), on \(.conditions.ref_name.include | join(", "))",
  "Rules: \([.rules[].type] | join(", "))",
  "Bypass: \([.bypass_actors[] | "\(.actor_type) \(.actor_id // "") (\(.bypass_mode))"] | join(", "))"'

# CODEOWNERS entries GitHub cannot resolve (a misspelled user or team, say) own nothing,
# so code owner review would then require nothing for those files.
if errors="$(gh api "repos/$repo/codeowners/errors" --jq '.errors[] | "line \(.line): \(.message | split("\n")[0])"' 2> /dev/null)"; then
  if [ -n "$errors" ]; then
    echo "warning: CODEOWNERS has errors; files whose owner GitHub cannot resolve need no code owner review:" >&2
    printf '%s\n' "$errors" >&2
  fi
fi
