#!/usr/bin/env bash
# First run: write the smallest config.json that works, and say what is left
# to do. Two answers are needed — the folder your repositories live in, and
# the tailnet login allowed to use the dashboard — and both are guessed, so
# the usual run is three presses of Enter.
#
#   ./onboarding.sh                     ask, with guesses filled in
#   ./onboarding.sh --root ~/code --login you@example.com --yes
#   ./onboarding.sh --force             overwrite an existing config.json
#
# Sessions are NOT configured here. Add them from the dashboard: the + button
# in the top bar browses this root and starts a session in the repository you
# pick.
set -uo pipefail
cd "$(dirname "$0")"

ROOT=""; LOGIN=""; YES=0; FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --root) ROOT="${2:-}"; shift 2 ;;
    --login) LOGIN="${2:-}"; shift 2 ;;
    --yes|-y) YES=1; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

if [ -f config.json ] && [ "$FORCE" = 0 ]; then
  echo "config.json already exists — nothing to do (use --force to rewrite it)."
  exit 0
fi

# The login tailscaled will stamp on your own requests. Asking tailscale
# beats asking you: this is the value the dashboard compares against, so a
# typo here is a locked door.
if [ -z "$LOGIN" ]; then
  LOGIN=$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
except ValueError:
    print(""); raise SystemExit
me = (d.get("Self") or {}).get("UserID")
print(((d.get("User") or {}).get(str(me)) or {}).get("LoginName", ""))' 2>/dev/null)
fi
[ -z "$ROOT" ] && ROOT="$HOME/src"

if [ "$YES" = 0 ]; then
  printf 'folder your repositories live in [%s]: ' "$ROOT"
  read -r reply
  [ -n "$reply" ] && ROOT="$reply"
  printf 'tailnet login allowed to use the dashboard [%s]: ' "${LOGIN:-none found}"
  read -r reply
  [ -n "$reply" ] && LOGIN="$reply"
fi

ROOT="${ROOT/#\~/$HOME}"
if [ ! -d "$ROOT" ]; then
  echo "no such folder: $ROOT" >&2
  exit 1
fi
if [ -z "$LOGIN" ]; then
  echo "a tailnet login is required: an empty allow_logins would serve a shell to every peer, and the server refuses to start that way." >&2
  exit 1
fi

ROOT="$ROOT" LOGIN="$LOGIN" python3 -c 'import json, os
cfg = {
    "_written_by": "onboarding.sh — add sessions from the dashboard (+)",
    "src_dir": os.environ["ROOT"],
    "allow_logins": [os.environ["LOGIN"]],
    "socket": "~/.roost/roost.sock",
    "ttyd_socket": "~/.dtach/ttyd.sock",
    "folders": [],
}
open("config.json", "w").write(json.dumps(cfg, indent=2) + "\n")
os.chmod("config.json", 0o600)'
[ $? = 0 ] || { echo "could not write config.json" >&2; exit 1; }

umask 077
mkdir -p "$HOME/.roost" "$HOME/.dtach"
chmod 700 "$HOME/.roost" "$HOME/.dtach"

echo
echo "config.json written:"
echo "  repositories : $ROOT"
echo "  login        : $LOGIN"
echo
echo "next:"
echo "  ./term.sh                       # the terminal service"
echo "  tmux new-session -d -s roost-web -c \"$PWD\""
echo "  tmux send-keys -t roost-web ./run.sh Enter"
echo "  sudo tailscale serve --bg --https=8444 unix:\$HOME/.roost/roost.sock"
echo
echo "then open https://<machine>.<tailnet>.ts.net:8444/ and press + to add"
echo "your first session. SECURITY.md is worth reading before you do."
