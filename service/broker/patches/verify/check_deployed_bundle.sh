#!/usr/bin/env bash
#
# Is a frontend change actually live? Grep the deployed chunks for it.
#
#   ./check_deployed_bundle.sh https://www.jinshiming.net 'title:e.description'
#   ./check_deployed_bundle.sh https://www.jinshiming.net 'no worker schema' '&&delete'
#
# The page is a SPA whose routes are code-split, so the code a given change lives in is NOT
# in the bundle the HTML references -- it is in a per-route chunk loaded on navigation, and
# the chunk's name changes on every build. Grepping `index-*.js` therefore answers "is the
# shell deployed", not "is my change deployed", which is how a redeployed fix looked absent
# while the rebrand next to it was clearly live.
#
# Patterns are greps, not regexes you have to escape: use minified text (`title:e.description`)
# rather than source text, because the bundle has comments stripped, names shortened and
# whitespace removed.
set -euo pipefail

site="${1:?usage: $0 <site url> <pattern> [<pattern>...]}"; shift
[ "$#" -gt 0 ] || { echo "give at least one pattern to look for" >&2; exit 2; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

shell="$(curl -fsS "$site/" | grep -oE 'src="[^"]+\.js"' | head -1 | cut -d'"' -f2)"
[ -n "$shell" ] || { echo "could not find the entry bundle at $site" >&2; exit 1; }
curl -fsS "$site$shell" -o "$tmp/shell.js"
printf 'entry  %s (%s bytes)\n' "$shell" "$(wc -c < "$tmp/shell.js")"

# Every chunk the shell mentions. Deliberately loose: the router's manifest is minified and
# the references are not quoted paths, so an anchored pattern found 12 of 33 chunks and
# silently skipped the one that held the change -- which is how a deployed fix looked absent.
grep -oE '[A-Za-z0-9_-]+-[A-Za-z0-9_-]{6,10}\.js' "$tmp/shell.js" | sort -u > "$tmp/chunks.txt" || true
printf 'chunks %s mentioned by the entry bundle\n' "$(wc -l < "$tmp/chunks.txt" | tr -d ' ')"

found=0
for name in $(cat "$tmp/chunks.txt") "$shell"; do
  case "$name" in
    "$shell") file="$tmp/shell.js" ;;
    *) file="$tmp/$name"; curl -fsS "$site/assets/$name" -o "$file" 2>/dev/null || continue ;;
  esac
  hits=""
  for pattern in "$@"; do
    # -F: the patterns are minified *text*, full of regex metacharacters
    count="$(grep -cF -- "$pattern" "$file" || true)"
    [ "${count:-0}" -gt 0 ] && hits="$hits  $pattern:$count"
  done
  [ -n "$hits" ] || continue
  found=$((found + 1))
  printf '  %-28s %7s bytes%s\n' "$name" "$(wc -c < "$file")" "$hits"
done

if [ "$found" -eq 0 ]; then
  printf 'none of those patterns appear in %s chunks: the change is NOT deployed\n' \
    "$(wc -l < "$tmp/chunks.txt" | tr -d ' ')"
  exit 1
fi
printf '%d chunk(s) matched: the change is deployed (or something very like it)\n' "$found"
