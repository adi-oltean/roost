# roost — onboarding

You have a Claude Code or codex session running on a workstation. You are not
at that workstation. **roost** is the page that gets you back to it: every
session on the machine as a card, a real terminal for each one, the whole
conversation as a readable transcript, and the files those conversations point
at — from a phone, over Tailscale, with nothing exposed to the internet.

This document is for someone picking the repository up cold. `README.md` is
the reference for each feature; this is the shape of the thing, how to get it
running, and where to look when you change it.

---

## What it is not

No framework, no package manager, no build step, no database, no daemon.
`server.py` is one file of **standard-library Python 3** that you run in a
terminal. The only third-party code in the repository is two JavaScript
libraries and two fonts under `vendor/`, served as files. That is deliberate:
the thing has to survive a reboot, a version bump and six months of neglect on
a machine nobody is administering.

---

## The moving parts

```
phone / laptop
   │  https://<machine>.<tailnet>.ts.net:8444
   ▼
tailscale serve ──── stamps Tailscale-User-Login, terminates TLS
   │  unix:~/.roost/roost.sock
   ▼
server.py ─────────── the dashboard, the file viewer, the transcripts,
   │                  and a relay for /term
   │  unix:~/.dtach/ttyd.sock
   ▼
ttyd  ─────────────── browser ⇄ pty  (xterm.js in the page)
   │
   ▼
dtach ─────────────── keeps the session alive when nobody is attached
   │
   ▼
claude / codex ────── the agent itself
```

Nothing but `tailscale serve` listens on a TCP port: roost and ttyd use Unix
sockets in private directories. The tailnet reaches one port, only through
serve, and no local process of another account (nor, under WSL2, any Windows
process) can reach roost at all, which is what makes the
identity check below trustworthy.

State roost reads but never owns:

| path | what |
|---|---|
| `~/.claude/sessions/<pid>.json` | Claude Code's own status records — pid, cwd, sessionId, bridge |
| `~/.claude/projects/<cwd>/<sid>.jsonl` | Claude transcripts |
| `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` | codex transcripts |
| `~/.dtach/<name>` | the session's dtach socket |
| `~/.dtach/<name>.cmd` | how to start that session, if it is not running |

State roost owns: `config.json` (folders, order, favourites, identity allow
list) and `~/.roost/` (messaging slots, pasted images).

---

## Standing it up on a fresh machine

### 1. Prerequisites

`python3` (3.12 here; nothing exotic is used), `git`, `tmux`, plus two small
binaries that need no root and live in `~/.local/bin`:

```sh
# dtach — ~75 KB, built from source with the system gcc
sudo apt-get install -y dtach            # or:
curl -fsSLO https://sourceforge.net/projects/dtach/files/dtach/0.9/dtach-0.9.tar.gz
tar xf dtach-0.9.tar.gz && cd dtach-0.9 && ./configure && make
install -Dm755 dtach ~/.local/bin/dtach

# ttyd 1.7.x — a 1.4 MB static binary
curl -fsSL -o ~/.local/bin/ttyd \
  https://github.com/tsl0922/ttyd/releases/latest/download/ttyd.x86_64
chmod +x ~/.local/bin/ttyd
ttyd --version     # 1.7.7 here

# both are invoked by name, so a shell that starts a session has to find them
export PATH="$HOME/.local/bin:$PATH"        # and put this line in ~/.profile
```

### 2. Tailscale

```sh
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up                 # opens a browser URL; log in as yourself
tailscale status                  # confirm the machine is on the tailnet
tailscale cert --help             # HTTPS needs MagicDNS + HTTPS enabled
```

HTTPS certificates require **MagicDNS** and **HTTPS Certificates** to be
enabled for the tailnet (admin console → DNS). They are required: without
them `--https` will not issue, and there is no supported plain-HTTP setup.

Leave the tailscale **operator** unset (`sudo tailscale set --operator=` clears
it). With it set, any process running as you — the agents included — can run
`tailscale funnel` without sudo and publish the terminal to the internet. Add
the one serve mapping with `sudo`; it persists across reboots.

The identity check depends on `tailscale serve` and nothing else: serve stamps
`Tailscale-User-Login` on every request it proxies and overwrites any copy the
client sent. Put your own login in `allow_logins`, or roost will refuse you.

### 3. roost itself

```sh
git clone <this repo> ~/src/roost && cd ~/src/roost

./onboarding.sh                      # asks two questions, writes config.json
                                     # (cp config.example.json config.json
                                     #  and editing it by hand also works)

tmux new-session -d -s roost-web -c ~/src/roost
tmux send-keys -t roost-web ./run.sh Enter    # restarts the server if it exits

./term.sh                                    # starts ttyd on ~/.dtach/ttyd.sock

sudo tailscale serve --bg --https=8444 unix:$HOME/.roost/roost.sock   # root: a unix: target
```

Change the serve mapping **before** the server moves to a new socket, or the
dashboard is unreachable in between.

**One mapping, and no others.** No plain-HTTP port (a second door onto the
same dashboard, and not a secure context, so the browser withholds the
clipboard API there), and nothing pointing at ttyd -- a mapping for `/term`,
or straight to ttyd's own port, hands a writable shell to anyone the tailnet
ACL admits, because roost never sees the request and never checks
`allow_logins`. `/term` is relayed through the server instead.

`tailscale serve status` shows the mappings; `tailscale serve --https=8444 off`
removes one.

### 4. The URL to keep

**`https://<machine>.<tailnet>.ts.net:8444/`** — the dashboard. That is the
one to bookmark, and the one to put on a phone's home screen; everything else
is reachable from it. `tailscale status` prints the machine's name, and
`tailscale serve status` prints the URL it is serving on, which is the
authoritative answer on any given host:

```sh
tailscale serve status        # https://<machine>.<tailnet>.ts.net:8444 -> unix:~/.roost/roost.sock
```

On a phone, open it in the browser and use **Add to Home Screen** (iOS Safari:
Share → Add to Home Screen; Android Chrome: ⋮ → Add to Home screen). roost
ships a web app manifest and icons, so it installs as a standalone app with no
browser chrome — which is what makes it usable one-handed. Log in to Tailscale
on the phone first, or the name will not resolve.

Individual session pages (`/t?name=<session>`) are worth bookmarking too if
you live in one session, but they are all one tap from the dashboard.

`./restart.sh` does all of the above idempotently, restores the serve
mapping if it went missing, and brings every configured session back — after
the first setup it is the only command you need to remember. It reads
`config.json`, so `./onboarding.sh` comes first, and it cannot add the serve
mapping the first time: a `unix:` target needs root, and it prints the `sudo`
line to run.

> ⚠️ The terminal is **writable** — that is the point of it, and it means
> anyone who can reach the URL gets a shell as you. `tailscale serve` is
> tailnet-only; never `tailscale funnel` this.

---

## Adding a repository, and a session for it

### A Claude Code session

One command, from an existing checkout under `src_dir`:

```sh
./fork-session.sh work/platform/sim           # card named "sim"
./fork-session.sh work/platform/firmware  fw   # …or name it yourself
```

That adds the entry to `config.json`, reloads the dashboard, starts the
session and prints its link. `restart.sh` reads the same file, so the new card
is covered by reboot recovery with no second edit.

For a repository that is **not** checked out yet, add it by hand and let the
card clone it on first Start:

```json
"folders": ["…", "myrepo"],
"clone_urls": { "myrepo": "https://github.com/<org>/myrepo.git" }
```

Without a `clone_urls` entry the URL is `clone_base/<name>.git`. Use the
`{"path": …, "name": …}` form for a repo nested inside another workspace — a
bare name always resolves to `src_dir/<name>`, and if that does not exist the
card will clone a *second* copy there instead of opening the one you have.

### A codex session

Codex sessions are not in `config.json` at all. They are a definition file
under `~/.dtach/`, written by `term.sh`:

```sh
./term.sh data 'cd ~/src/data-analysis && codex'
```

That writes `~/.dtach/data.cmd`; the card appears immediately, and **Start**
launches it. To resume a specific past conversation instead of starting a new
one, put that in the command:

```sh
./term.sh t42 'cd ~/src/work/platform/docs/t42-review && codex resume <session-id>'
```

`./term.sh --list` shows the definitions, `--forget <name>` removes one (the
running session is untouched). The convention on a machine with a
`display_suffix` is that a codex terminal takes the bare name and the Claude
session takes the suffixed one — `data` and `data2` are different sessions on
the same folder.

A folder codex has not seen before opens on its **trust prompt**; answer it in
the terminal once and the session is live.

## config.json

**The live `config.json` is not in version control.** `config.example.json`
is, and the server copies it on first start if no config is there yet:

```
config.json created from config.example.json -- edit it
(allow_logins and folders at least), then restart
```

It is untracked for two reasons: one machine's folder list is not another's,
and **the dashboard writes to it** — `card_order` every time you drag a card,
`favorites` every time you pin one. A tracked file that rewrites itself under
you is a permanent dirty diff.

Edit at minimum:

| key | why it matters on a new machine |
|---|---|
| `allow_logins` | **an empty list stops the server from starting** — the terminal is a writable shell. Put your own tailnet login here; serving every tailnet peer needs `allow_anyone: true`, spelled out. |
| `folders` | the Claude sessions you want cards for; the template ships two placeholders |
| `display_suffix` | set it if you run roost on more than one machine, so the cards are distinguishable |
| `clone_base` / `clone_urls` | where a missing folder gets cloned from |

Keys beginning with `_` in the template are notes to the reader; the server
strips them, so you can leave them in place or delete them.


```json
{
  "src_dir": "~/src",
  "display_suffix": "2",
  "allow_logins": ["you@example.com"],
  "folders": ["roost", "blog", {"path": "work/platform/firmware", "name": "fw"}],
  "clone_base": "https://github.com/<owner>",
  "clone_urls": {"backend": "https://github.com/<org>/backend.git"}
}
```

`folders` are Claude sessions, one card each. Codex terminals are not in
`config.json` at all: they are a `~/.dtach/<name>.cmd` file, written by
`./term.sh <name> '<command>'`. `card_order` and `favorites` are written by
the page when you drag or pin, so expect that file to change under you.

---

## Using it

- **A card** opens that session's page. **Start** beside it brings the session
  up, or nudges one that is already running; pressing it again when nothing
  moves offers a restart.
- **Hold a card** (or drag its grip with a mouse) to reorder. The order is
  saved server-side, so it is the same on every device.
- **The session page** has two tabs: the live **terminal**, and the
  **history** — the real transcript read from the agent's own log, rendered as
  markdown with maths and code, not scraped from the screen.
- **Paths in the terminal are links**: absolute, `~`-relative, relative to the
  session's folder, and bare filenames. They open in the **file viewer**,
  which renders markdown, JSON, CSV, source and HTML, with a file tree, three
  reading widths, and a hover box saying which session and which git worktree
  a file belongs to and whether it has been pushed.
- **Forward / Auto-fw / Reply** move a session's last answer to another
  session, through named slots, so two agents can hand work over without
  either of them knowing the other exists.

---

## Security model

The dashboard is a **writable shell** on a workstation. The summary is below;
**[`SECURITY.md`](SECURITY.md)** is the full threat model, including what is
deliberately not defended and the checklist before standing this up on a
machine that is not your own.

1. **Reachability.** roost and ttyd listen on Unix sockets, not ports. The only route in is a
   `tailscale serve` mapping, so the tailnet ACL is the outer gate.
2. **Identity.** `tailscaled` stamps `Tailscale-User-Login` on everything it
   proxies and overwrites any copy the client sent. `allow_logins` in
   `config.json` is checked at the top of every GET and POST — including
   `/term`, which is why the terminal websocket is relayed through roost
   instead of being mapped straight to ttyd.
3. **Cross-site.** Every mutating route requires `Sec-Fetch-Site: same-origin`
   or a matching `Origin`, so a page you merely visit cannot drive the
   dashboard as you.
4. **File containment.** The viewer serves only under known roots (each
   session's folder, `~/src`, `/tmp`, `$HOME`), resolves symlinks before
   checking, requires ownership on sticky ground like `/tmp`, and refuses
   credentials outright wherever they live — `.ssh`, `.aws`, `.config`,
   `*.pem`, `*.key`, `.credentials.json`, shell histories, and more.
5. **Untrusted HTML.** A `.html` file renders in a frame whose response
   carries `sandbox` — opaque origin, no `connect-src`, inline script only —
   so a generated report reads as itself and can do nothing else.

---

## The code

`server.py` is ~9,400 lines in one file, in rough order:

| region | what lives there |
|---|---|
| top | config, `FOLDERS`, dtach/tmux helpers, `type_into`, `reorder` |
| identity | `caller_login`, `allowed`, `same_site` |
| files | `_roots`, `root_of`, `_under_root`, `denied`, `git_root`, `git_state`, `where` |
| rendering | `md_html` and friends, `_json_view`, `_csv_view`, `file_view` |
| pages | `FILE_PAGE`, `PAGE`, `CODEX_PAGE`, `TERMWRAP_PAGE` — HTML+CSS+JS as Python strings |
| transcripts | `rollout_conv`, `_claude_rows`, `codex_html` |
| messaging | slots: `slot_defs`, `slot_send`, `reply_target` |
| handler | `do_GET` / `do_POST`, one branch per route |

Companion scripts: `onboarding.sh` (the first `config.json`), `run.sh`
(restart loop), `term.sh` (ttyd + session definitions), `attach.sh` (what ttyd
runs: attach to a session, or create it from its definition), `restart.sh`
(everything, after a reboot), `fork-session.sh` (give a subtree its own card),
`update-claude.sh` (update the agent and restart idle sessions), `ccmsg`
(session-to-session messaging from inside an agent), `check-page.js` (see
below).

### Changing it safely

- **The pages are Python strings, so escaping has two levels.** A JavaScript
  `"\\b"` written once in a template reaches the browser as `\b` — a
  backspace, not a word boundary. This has caused real bugs twice. When you
  write a regex or an escape in page JavaScript, check the *served* output,
  not the source.
- **`node check-page.js <saved-page.html>`** runs a page's inline script
  against a DOM stub and reports what it throws. `node --check` only parses;
  this catches the class of bug that silently kills everything below it.
- **For behaviour, drive the real served page.** `npm i jsdom` in a scratch
  directory and dispatch events at it; that is how the drag, the reconnect
  watchdog and the terminal link provider were verified.
- **`node check-terminal.js`** drives two real Chromium pages against a
  running server and checks what a DOM stub cannot see: how many clients are
  attached to the dtach socket and what size the pty ended up with. Needs
  playwright — `PLAYWRIGHT_PATH=<dir>/node_modules node check-terminal.js`.
  It found a relay bug jsdom had been hiding for weeks.
- **`node check-order.js`** clicks the star on the bottom card and checks it
  arrives at the top. It works on the live `config.json` and puts it back
  afterwards, failures included.
- **`node check-scroll.js <saved-page.html>`** is one of those kept around,
  because the bug it covers came back three times: the terminal being dragged
  back to the last line while someone is reading it. Needs jsdom —
  `JSDOM_PATH=<dir>/node_modules node check-scroll.js page.html`.
- **Restart the server once per batch of edits, not per edit.** The terminal
  websocket is relayed through the process, so every restart disconnects
  every open terminal.
- **Names collide in one big module.** Two helpers called `_git` and two
  caches called `_GIT_CACHE` have each broken something unrelated. Grep
  before you name.

### Things that will surprise you

- **xterm uses the WebGL renderer**, so terminal text is painted, not markup:
  the browser's own Copy is greyed out, and copying goes through
  `term.getSelection()`. Claude Code also enables mouse reporting, so
  selecting inside a Claude terminal needs **shift**-drag.
- **dtach's `MSG_PUSH`** writes straight to the pty without attaching, which
  is how roost types into a session without resizing it.
- **`/tmp` is swept.** systemd-tmpfiles empties it at boot and ages files out
  after 30 days. Agents put working documents there; roost puts nothing it
  cares about there.
