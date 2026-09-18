#!/usr/bin/env bash
# Re-download the two libraries the pages use. They are committed, so this is
# only for changing versions -- bump these and run it.
#
# They are committed rather than pulled from a CDN at page load because the
# CDN was a silent single point of failure: a browser that could not reach
# cdnjs showed the LaTeX source and uncoloured code with no indication that
# anything had gone wrong.
#
# Downloads land in a staging directory and are checked against SHA256SUMS
# before anything here is replaced. After a deliberate version bump, review
# the new files and run with --update to record their hashes.
set -euo pipefail
cd "$(dirname "$0")"
KATEX=0.16.11
HLJS=11.9.0
B=https://cdnjs.cloudflare.com/ajax/libs
UPDATE=0; [ "${1:-}" = "--update" ] && UPDATE=1

T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/katex/fonts" "$T/hljs"
curl -sSf -o "$T/katex/katex.min.js"  "$B/KaTeX/$KATEX/katex.min.js"
curl -sSf -o "$T/katex/katex.min.css" "$B/KaTeX/$KATEX/katex.min.css"
curl -sSf -o "$T/hljs/highlight.min.js"    "$B/highlight.js/$HLJS/highlight.min.js"
curl -sSf -o "$T/hljs/github-dark.min.css" "$B/highlight.js/$HLJS/styles/github-dark.min.css"

# Only woff2: every browser that reaches this dashboard supports it, and the
# woff/ttf copies triple the size for nothing.
for f in $(grep -o 'fonts/[A-Za-z0-9_-]*\.woff2' "$T/katex/katex.min.css" | sort -u); do
  curl -sSf -o "$T/katex/$f" "$B/KaTeX/$KATEX/$f"
done

if [ "$UPDATE" = 0 ]; then
  (cd "$T" && grep -E '  (katex|hljs)/' "$OLDPWD/SHA256SUMS" | sha256sum -c --quiet) \
    || { echo "hash mismatch: nothing replaced (after a version bump, use --update)" >&2; exit 1; }
fi
cp -r "$T/katex" "$T/hljs" .
(find katex hljs inter -type f | sort | xargs sha256sum) > SHA256SUMS
echo "katex $KATEX, highlight.js $HLJS: $(du -sh . | cut -f1)"
