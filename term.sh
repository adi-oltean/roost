#!/usr/bin/env bash
# One browser terminal service for every session — one socket, one URL.
#
#   ./term.sh api 'cd ~/src/x && codex resume <id>'    # define (and serve) it
#   ./term.sh                                          # URLs + session list
#   ./term.sh --list
#   ./term.sh --stop            # stop serving; sessions keep running
#   ./term.sh --forget <name>   # drop the definition (session unaffected)
#
# Sessions are selected by query string (?arg=<name>) via ttyd's --url-arg,
# not by port. An earlier version gave each session its own port, which meant
# a URL per session and a tailscale serve mapping per session — and those
# mappings shadowed the ports, so connecting by IP hit serve's Host-header
# check and returned 404 instead of the terminal.
#
# Layers: ttyd (browser <-> pty) -> dtach (persistence) -> your command.
# dtach rather than tmux because it does no terminal emulation and no redraw,
# so a full-screen TUI like codex renders natively while still surviving a
# disconnect.
set -uo pipefail
umask 077
cd "$(dirname "$0")"

TSOCK="${ROOST_TTYD_SOCK:-}"                        # ttyd itself: a Unix socket
if [ -z "$TSOCK" ]; then
  TSOCK=$(python3 -c 'import json,os
try: cfg = json.load(open("config.json"))
except OSError: cfg = {}
print(os.path.expanduser(cfg.get("ttyd_socket", "~/.dtach/ttyd.sock")))' 2>/dev/null)
fi
[ -n "$TSOCK" ] || TSOCK="$HOME/.dtach/ttyd.sock"
TLS_PORT="${ROOST_TERM_TLS:-8444}"   # the dashboard's https port, where /term is
HTTP_PORT="${ROOST_TERM_HTTP:-8480}" # and its plain-http twin, same sub-path
BASE="/term"                         # sub-path under that port
TTYD="${TTYD:-$(command -v ttyd || echo ~/.local/bin/ttyd)}"
# Session definitions are shell commands, and the sockets are terminals:
# neither is anyone else's business on a shared machine.
DIR="$HOME/.dtach"; mkdir -p "$DIR"; chmod 700 "$DIR"
HOST=$(tailscale status --json 2>/dev/null \
       | python3 -c "import json,sys;print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))" 2>/dev/null)
IP=$(tailscale ip -4 2>/dev/null | head -1)

# With an identity header, because ttyd runs with -H and answers 407 without
# one. Unheaded, this check read a healthy ttyd as dead, and every call then
# tried to start a second one on a port already taken -- "ttyd failed to
# start", from the one place that could see it was already running.
serving() {
  curl -fsS --max-time 2 -H "Tailscale-User-Login: probe@localhost" \
       --unix-socket "$TSOCK" http://localhost/ >/dev/null 2>&1
}

urls() {
  echo "  https://${HOST:-localhost}:$TLS_PORT$BASE/?arg=NAME"
}

list() {
  for f in "$DIR"/*.cmd; do
    [ -e "$f" ] || continue
    b=$(basename "$f" .cmd)
    live="  "; [ -S "$DIR/$b" ] && live=" *"
    printf "%s %-12s %s\n" "$live" "$b" "$(head -c 60 "$f")"
  done
}

case "${1:-}" in
  --stop)
    pkill -f "ttyd .*-i $TSOCK" && echo "stopped ttyd on unix:$TSOCK" || echo "nothing on unix:$TSOCK"
    # Only this sub-path. Dropping the whole listener would take the
    # dashboard's own mapping down with it.
    tailscale serve --https="$TLS_PORT" --set-path="$BASE" off >/dev/null 2>&1
    tailscale serve --http="$HTTP_PORT" --set-path="$BASE" off >/dev/null 2>&1
    echo "sessions keep running; re-serve with: $0"
    exit 0 ;;
  --list) list; exit 0 ;;
  --forget)
    n="${2:?name}"
    case "$n" in *[!A-Za-z0-9_-]*) echo "name must be [A-Za-z0-9_-]" >&2; exit 2 ;; esac
    rm -f "$DIR/$n.cmd" && echo "forgot $n (its session, if running, is untouched)"; exit 0 ;;
esac

NAME="${1:-}"; shift || true
if [ -n "$NAME" ] && [ $# -gt 0 ]; then
  case "$NAME" in *[!A-Za-z0-9_-]*) echo "name must be [A-Za-z0-9_-]" >&2; exit 2 ;; esac
  # The session is told its own roost name, so an agent inside it can say who
  # it is without being asked -- ccmsg whoami, and the "handed over by" note.
  printf 'export ROOST_NAME=%s; %s' "$NAME" "$*" > "$DIR/$NAME.cmd"
  echo "defined '$NAME': $*"
fi

# One ttyd for everything. -a passes ?arg=<name> through to attach.sh.
if serving; then
  echo "service already up on unix:$TSOCK"
else
  # disableLeaveAlert: ttyd otherwise fires a beforeunload "Leave site?"
  # confirm on every navigation away, which makes leaving the page feel
  # broken.
  # Leaving is safe here — dtach holds the session, so nothing is lost.
  # -i <socket>: ttyd is a WRITABLE terminal. Bound to 0.0.0.0 it was an
  # unauthenticated shell for every tailnet peer; bound to 127.0.0.1 it was
  # one for every local process -- and under WSL2 every Windows process,
  # which shares the loopback interface. A Unix socket in the 0700 ~/.dtach
  # is reachable by this account alone. roost relays /term to it after its
  # own identity check.
  # -H: refuse anything arriving without a Tailscale identity. serve stamps
  # one on every tailnet request, so this fails closed for a tagged device or
  # for anything that reaches ttyd without passing through serve. It checks
  # that an identity exists, not which one -- the ACL is what says who -- so
  # it is a second lock on the same door, not a replacement for one.
  # -O: refuse a websocket whose Origin is not this host. The identity header
  # rides along on a request any web page makes, so without this a site you
  # visit could open a terminal. roost checks the same thing before relaying.
  rm -f "$TSOCK"                   # stale: nothing answered on it
  nohup "$TTYD" -W -a -O -i "$TSOCK" -H Tailscale-User-Login \
        -t fontSize=13 \
        -t disableLeaveAlert=true \
        -t 'theme={"background":"#0e0e11"}' "$PWD/attach.sh" \
        >"$DIR/ttyd.log" 2>&1 &
  for _ in $(seq 1 20); do sleep .5; serving && break; done
  serving || { echo "ttyd failed to start — see $DIR/ttyd.log" >&2; exit 1; }
  echo "started ttyd on unix:$TSOCK"
fi

# No serve mapping for /term, on purpose. serve can say who may
# open a connection but not which account may use a path, so a mapping
# straight to ttyd would hand the terminal to every peer the ACL admits.
# roost relays /term itself, behind allow_logins. Remove any mapping left
# over from the previous arrangement.
for pair in "--https=$TLS_PORT" "--http=$HTTP_PORT"; do
  tailscale serve "$pair" --set-path="$BASE" off >/dev/null 2>&1
done

echo
echo "sessions:"; list
echo
if [ -n "$NAME" ]; then
  echo "open '$NAME':"
  echo "  https://${HOST:-localhost}:$TLS_PORT$BASE/?arg=$NAME"
else
  urls
fi
