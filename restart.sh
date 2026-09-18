#!/usr/bin/env bash
# Bring the whole roost stack back after a reboot (or any time it's all down).
#
#   ./restart.sh              dashboard + every session
#   ./restart.sh --dash-only  just the dashboard
#
# Idempotent: anything already running is left alone, so it's safe to re-run.
set -uo pipefail
cd "$(dirname "$0")"

# The dashboard listens on a Unix socket, not a TCP port (see SECURITY.md).
SOCK=$(python3 -c 'import json,os;print(os.path.expanduser(json.load(open("config.json")).get("socket","~/.roost/roost.sock")))')
NAMES=$(python3 -c '
import json
for e in json.load(open("config.json"))["folders"]:
    print(e["name"] if isinstance(e, dict) else e.rstrip("/").split("/")[-1])')
DASH_ONLY=0
for a in "$@"; do
  case "$a" in
    --dash-only) DASH_ONLY=1 ;;
    *) echo "unknown flag: $a" >&2; exit 2 ;;
  esac
done

# The dashboard checks the caller's Tailscale identity now, and a local curl
# carries none — every call from here would be refused. tailscaled stamps the
# header on anything arriving through serve and overwrites a client's copy, so
# setting it locally is not a way in from the tailnet; it is how a process on
# this machine, which already has the machine, identifies itself.
WHO=$(python3 -c 'import json
a = json.load(open("config.json")).get("allow_logins") or []
print(a[0] if a else "")')
AUTH=(); [ -n "$WHO" ] && AUTH=(-H "Tailscale-User-Login: $WHO")

up() { curl -fsS --max-time 2 "${AUTH[@]}" --unix-socket "$SOCK" http://localhost/ >/dev/null 2>&1; }
TTYD_SOCK=$(python3 -c 'import json,os;print(os.path.expanduser(json.load(open("config.json")).get("ttyd_socket","~/.dtach/ttyd.sock")))')
export ROOST_TTYD_SOCK="$TTYD_SOCK"   # term.sh uses the same one

# 1. This pane. A reboot leaves you in a tmux session named "0"; the roost
#    button targets a session named "roost", so without this you'd get a
#    second claude in this folder (an orange button) instead of adopting
#    the one you're sitting in.
here=$(tmux display-message -p '#{session_name}' 2>/dev/null || echo "")
if [ "$here" = "0" ] && ! tmux has-session -t =roost 2>/dev/null; then
  tmux rename-session -t 0 roost && echo "adopted this pane as the 'roost' session"
fi

# 2. Dashboard. Lives in its own session so the plain 'roost' name stays free.
if up; then
  echo "dashboard already up on unix:$SOCK"
else
  tmux has-session -t =roost-web 2>/dev/null || tmux new-session -d -s roost-web -c "$PWD"
  tmux send-keys -t roost-web ./run.sh Enter
  printf "starting dashboard"
  for _ in $(seq 1 60); do up && break; printf .; sleep 2; done
  up && echo " up on unix:$SOCK" || { echo " FAILED — tmux attach -t roost-web"; exit 1; }
fi

# 3. Tailnet exposure. Survives a reboot in tailscaled's state, so each of
#    these only gets re-added if it actually went missing.
#
#    ONE mapping: https, the dashboard, and nothing else.
#
#    Not two more for /term. A serve mapping to ttyd -- on its own port or on
#    a path -- is a writable shell for anyone the tailnet ACL admits, because
#    roost never sees the request and so never checks allow_logins. /term is
#    relayed through the server instead (term_relay), which puts the terminal
#    behind the same identity check as everything else. These lines used to
#    restore that bypass on every reboot.
#
#    And not one for plain http on :8480 either: same dashboard, second door,
#    and not a secure context, so the browser withholds the clipboard API.
#    The tailnet encrypts either way; https is what the page is built for.
# Checked per port AND per path, because grepping the whole listing for a
# path matched the mapping on another port and quietly skipped a missing one.
serve_have() {  # port  path  target
  tailscale serve status --json 2>/dev/null | PORT="$1" P="$2" T="$3" python3 -c '
import json, os, sys
try: d = json.load(sys.stdin)
except ValueError: sys.exit(1)
port, path, target = ":" + os.environ["PORT"], os.environ["P"], os.environ["T"]
for hostport, v in (d.get("Web") or {}).items():
    h = (v.get("Handlers") or {}).get(path) or {}
    if hostport.endswith(port) and h.get("Proxy") == target:
        sys.exit(0)
sys.exit(1)'
}
serve_one() {  # scheme  port  path  target
  serve_have "$2" "$3" "$4" && return 0
  echo "  (re)setting $2$3 -> $4"
  if [ "$3" = "/" ]; then
    tailscale serve --bg "--$1=$2" "$4" >/dev/null 2>&1
  else
    tailscale serve --bg "--$1=$2" --set-path="$3" "$4" >/dev/null 2>&1
  fi || echo "    (failed — add it once with sudo: sudo tailscale serve --bg --$1=$2 $4)"
  # Not --operator=\$USER: that lets every process you run -- the agents
  # included -- change serve and funnel without sudo, and one funnel command
  # would put this terminal on the internet. The mapping persists anyway.
}
serve_one https 8444 / "unix:$SOCK"

# 3b. The terminal service. Without it every terminal link is a dead page,
#     and the dtach sessions behind them survive a restart of it untouched.
# With a header: ttyd runs with -H and answers 407 to a request without one.
if curl -fsS --max-time 2 -H "Tailscale-User-Login: probe@localhost" \
     --unix-socket "$TTYD_SOCK" http://localhost/ >/dev/null 2>&1; then
  echo "terminals already up on unix:$TTYD_SOCK"
else
  ./term.sh >/dev/null 2>&1 && echo "started ttyd on unix:$TTYD_SOCK" \
    || echo "  (term.sh failed — see ~/.dtach/ttyd.log)"
fi

[ "$DASH_ONLY" = 1 ] && { echo "dash-only: skipping sessions"; exit 0; }

# 4. Sessions. Press each card: a dead one is started from its own dtach
#    definition where it has one, and under tmux where it does not.
echo "reviving sessions:"
while IFS= read -r n; do
  [ -n "$n" ] || continue
  [ "$n" = "roost" ] && [ "$(tmux display-message -p '#{session_name}' 2>/dev/null)" = "roost" ] \
    && { echo "  roost: this pane, skipped"; continue; }
  printf "  %-12s " "$n"
  # --get puts the urlencoded name in the query string; -X keeps it a POST.
  timeout 25 curl -s --max-time 20 "${AUTH[@]}" </dev/null \
    --get --data-urlencode "name=$n" -X POST \
    --unix-socket "$SOCK" http://localhost/api/press || printf '(no response)'
  echo
done <<<"$NAMES"

# 5. Report, and name anything still waiting on a human. A fresh folder stops
#    on Claude Code's trust prompt, which no script should answer blindly:
#    the option order is NOT stable (one folder showed "No, exit" first), so a
#    canned "1" can exit the session instead of trusting it. Read the pane.
echo "settling..."
for _ in $(seq 1 24); do
  pending=$(curl -s "${AUTH[@]}" --unix-socket "$SOCK" http://localhost/api/status \
    | python3 -c 'import json,sys; print(" ".join(s["name"] for s in json.load(sys.stdin) if not s["rc"]))' 2>/dev/null)
  [ -z "$pending" ] && break
  sleep 5
done
curl -s "${AUTH[@]}" --unix-socket "$SOCK" http://localhost/api/status | python3 -c '
import json, sys
d = json.load(sys.stdin)
# %-formatting, not f-strings: this lives inside a single-quoted shell
# argument, so quotes nested in {} would need escaping the shell eats.
for s in d:
    state = "green" if s["rc"] else s["state"]
    print("  %-14s %-6s %s" % (s["label"], state, s.get("model") or "-"))
green = [s for s in d if s["rc"]]
print("%d/%d with remote control" % (len(green), len(d)))
stuck = [s["name"] for s in d if not s["rc"]]
if stuck:
    print("check these by hand (likely a trust prompt — read it, do not blind-answer):")
    for n in stuck:
        print("  tmux attach -t %s" % n)'
