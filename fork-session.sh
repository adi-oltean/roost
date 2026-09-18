#!/usr/bin/env bash
# Fork a new roost session: give a subtree its own button, tmux session and
# remote-control name, so long-running work stops sharing a session with
# whatever else lives in the parent folder.
#
#   ./fork-session.sh <path-under-src_dir> [name]
#
#   ./fork-session.sh work/project/docs docs
#   ./fork-session.sh my-repo
#
# <path> is relative to src_dir and must already exist — forking splits work
# that is already on disk. (Missing folders are the button's clone-on-tap job,
# which needs a clone_urls entry when the name doesn't match the repo.)
# [name] defaults to the path's basename; give one when the directory name
# isn't what you want on a button or after /rc.
#
# What it does: adds the entry to config.json, reloads the dashboard, starts
# the session, and reports its remote-control link. restart.sh reads the same
# config, so the fork is covered by reboot recovery from here on with no
# second edit.
set -uo pipefail
cd "$(dirname "$0")"

REL="${1:-}"; NAME="${2:-}"
[ -n "$REL" ] || { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
REL="${REL#/}"; REL="${REL%/}"
[ -n "$NAME" ] || NAME="$(basename "$REL")"
case "$NAME" in
  ""|*[!A-Za-z0-9_-]*) echo "name must be [A-Za-z0-9_-]; give one explicitly" >&2; exit 2 ;;
esac
export NAME

SRC=$(python3 -c 'import json,os;print(os.path.expanduser(json.load(open("config.json")).get("src_dir","~/src")))')
# The dashboard listens on a Unix socket, not a TCP port (see SECURITY.md).
SOCK=$(python3 -c 'import json,os;print(os.path.expanduser(json.load(open("config.json")).get("socket","~/.roost/roost.sock")))')
DIR="$SRC/$REL"; export DIR
# The dashboard refuses a request with no Tailscale identity; see restart.sh.
WHO=$(python3 -c 'import json
a = json.load(open("config.json")).get("allow_logins") or []
print(a[0] if a else "")')
AUTH=(); [ -n "$WHO" ] && AUTH=(-H "Tailscale-User-Login: $WHO")

[ -d "$DIR" ] || { echo "no such directory: $DIR" >&2; exit 1; }
if tmux has-session -t "=$NAME" 2>/dev/null; then
  echo "a tmux session named '$NAME' already exists — pick another name" >&2; exit 1
fi
if [ -e "$HOME/.dtach/$NAME.cmd" ]; then
  echo "a session definition named '$NAME' already exists — pick another name" >&2; exit 1
fi

# 1. config entry. Plain string when the name is just the basename; the
#    {path,name} form only when it actually differs, to keep config readable.
python3 - "$REL" "$NAME" <<'PY' || exit 1
import json, sys, pathlib
rel, name = sys.argv[1], sys.argv[2]
p = pathlib.Path("config.json"); cfg = json.loads(p.read_text())
names = [e["name"] if isinstance(e, dict) else pathlib.PurePath(e).name for e in cfg["folders"]]
if name in names:
    print(f"'{name}' is already a button", file=sys.stderr); sys.exit(1)
entry = rel if pathlib.PurePath(rel).name == name else {"path": rel, "name": name}
cfg["folders"].append(entry)
# Ordinary indented JSON: the server and the dashboard write it that way.
out = json.dumps(cfg, indent=2)
p.write_text(out + "\n")
print(f"added {entry!r} to config.json")
PY

# 2. Reload the dashboard. run.sh restarts it, so killing is enough.
pkill -f "^python3 server.py$" >/dev/null 2>&1
for _ in $(seq 1 60); do curl -fsS --max-time 2 "${AUTH[@]}" --unix-socket "$SOCK" "http://localhost/" >/dev/null 2>&1 && break; sleep 2; done
curl -fsS --max-time 2 "${AUTH[@]}" --unix-socket "$SOCK" "http://localhost/" >/dev/null 2>&1 \
  || { echo "dashboard did not come back — tmux attach -t roost-web" >&2; exit 1; }
echo "dashboard reloaded; button '$NAME' is live"

# 3. Start the session.
echo -n "starting session: "
curl -s --max-time 25 -X POST "${AUTH[@]}" --unix-socket "$SOCK" "http://localhost/api/press?name=$NAME"; echo

# 4. Report. A folder Claude Code has not seen stops on a trust prompt, and
#    the option order is NOT stable — never answer it blind from a script.
for _ in $(seq 1 30); do
  rc=$(curl -s "${AUTH[@]}" --unix-socket "$SOCK" "http://localhost/api/status" | python3 -c "
import json,os,sys
s=[x for x in json.load(sys.stdin) if x['name']==os.environ['NAME']]
print('1' if s and s[0]['rc'] else '0')" 2>/dev/null)
  [ "$rc" = "1" ] && break
  sleep 5
done
curl -s "${AUTH[@]}" --unix-socket "$SOCK" "http://localhost/api/status" | python3 -c "
import json, os, sys
name = os.environ['NAME']
s = [x for x in json.load(sys.stdin) if x['name'] == name][0]
print('  tmux    :', name)
print('  cwd     :', os.environ['DIR'])
print('  state   :', s['state'], '| model:', s.get('model') or '-')
print('  /rc name:', s['label'])
print('  URL     :', s.get('link') or '(none yet)')
if not s['rc']:
    print()
    print('  No remote-control link yet. Most likely a trust prompt:')
    print('    tmux attach -t', name)
    print('  Read it before answering — the option order is not stable.')
"
echo "restart.sh now covers this session too (it reads config.json)."
