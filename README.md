# roost

Remote control for the agent sessions running on a workstation — **Claude Code
and codex alike** — from **any device on your tailnet, in an ordinary browser**:
a phone on mobile data, a tablet on the sofa, another laptop, or the
workstation itself. Nothing to install, nothing on the internet.

One page lists every session as a card. Tapping a card opens that session: a
real terminal on one tab, and on the other its **transcript**, read from the
agent's own log rather than scraped off a screen, rendered as markdown with
maths, code and clickable paths. Those paths open in a **file viewer** with a
tree, three reading widths, and a hover box saying which session and which git
worktree a file belongs to and whether it has been pushed.

New here? **[`ONBOARDING.md`](ONBOARDING.md)** is the tour — the moving parts,
standing it up on another machine, and a map of the code.
**[`SECURITY.md`](SECURITY.md)** is the threat model, and worth reading before
you expose this anywhere. This file is the reference for each feature.

---

## Where you open it

It is a web page. Any current browser will do — there is no app, no extension
and no client to install; the device only has to be on the tailnet.

| device | how |
|---|---|
| **phone** | Tailscale app connected, then `https://<machine>.<tailnet>.ts.net:8444/`. Use **Add to Home Screen** (iOS Safari: Share → Add to Home Screen; Android Chrome: ⋮ → Add to Home screen) — roost ships a web app manifest and icons, so it installs as a standalone app with no browser chrome, which is what makes it usable one-handed. |
| **tablet / another laptop** | the same URL, same tailnet |
| **the workstation itself** | the same URL again — roost has no local port to open. It listens on a private Unix socket that only `tailscale serve` (and your own account) can reach. |
| **anything off the tailnet** | nothing. `serve` is tailnet-only, and there is no `funnel`. |

Three controls sit in the top bar, and nothing else: the **roost mark**,
which brings the list back up to date without leaving the page; **+**, which
opens a tree of the repositories under your root and starts a session in the
one you pick, as Claude Code or as codex; and the **files** icon, which opens
the file viewer in a tab of its own.

The interface is built for a thumb first: cards are tap targets, a card is
held rather than grabbed to reorder it, the terminal scrolls with inertia and
a rubber-band at the ends, and the transcript reflows to a reading column. It
is the same page on a 27-inch monitor; the file viewer is where the reading
widths live.

---

## It is not the only way in

roost does not own a session. It attaches to one, the same way you would, so
everything else you already use keeps working at the same time.

| alongside | how it coexists |
|---|---|
| **a terminal on the machine** | `dtach -a ~/.dtach/<name>` attaches to the very session the browser tab is showing. Both clients stay attached; output appears in both, and input from either reaches the same pty. |
| **Claude Code's own remote control** | `/rc` mints a `claude.ai/code` link and the card shows it. Drive the session from the Claude app or the web while watching it here. |
| **codex's app / remote control** | a codex session driven from its own remote control is still a dtach session writing a rollout file, so its terminal and its transcript are both here. |
| **ssh, a second roost, a colleague** | any number of readers. The history tab reads the agent's log file; it never types, so having it open cannot disturb a run whoever is driving. |

The one thing to expect is the obvious one: **two inputs into one pty
interleave.** Typing in the browser while typing in an attached terminal
produces exactly what you would get from two hands on one keyboard. Reading
is free; driving is best done from one place at a time.

---

## Why this, and not the agent's own remote control

Claude Code mints a `claude.ai/code` link with `/rc`, and codex has its app.
Both drive **one session from anywhere**, through the vendor's bridge, with
nothing for you to run — and on a train, with no tailnet, that is the right
tool. Use them; roost shows their links on the cards and does not interfere.

roost answers the other question: **what are all of these sessions doing on
my machine, and what have they touched?**

| | roost | the agents' own remote controls |
|---|---|---|
| **what you see** | the real terminal: the agent's full-screen interface, its colours, its permission prompts — because a pty is relayed, not re-rendered | the conversation, rendered by the vendor |
| **how many at once** | every session on the machine as cards, Claude Code and codex side by side, with what each is doing right now | the session whose link you opened |
| **paths in the output** | clickable, in the terminal *and* in the transcript: they open the file itself | text |
| **the rest of the machine** | a file tree beside the document, with the session and the git state of each file — and a bare name off a terminal line resolved to the file it means | not what they are for |
| **the transcript** | read from the agent's own log on disk, so nothing is lost to scrollback or to a dropped connection | the vendor's copy of the conversation |
| **between agents** | one can hand its answer to another, and you can watch it arrive | — |
| **where the bytes go** | your tailnet, between your own devices; nothing is exposed to the internet | the vendor's bridge |
| **what it costs to run** | one Python file, one socket, `tailscale serve` | nothing |

Where theirs win, plainly: they work from any network without a VPN, there is
nothing to install, patch or secure, and they can notify you when a session
wants something. roost is a writable terminal that you are responsible for —
read [`SECURITY.md`](SECURITY.md) before you put it anywhere.

---

## How a session runs

Every agent session lives under **dtach**, not tmux:

```
ttyd  (browser ⇄ pty)  →  dtach  (persistence)  →  claude | codex
```

dtach rather than tmux precisely because it does *no* terminal emulation and
no redraw — it only detaches the process from its terminal, so a full-screen
TUI behaves as it would natively while surviving every disconnect. Close the
tab and the work continues; reopen the card to reattach, or attach from a
terminal with `dtach -a ~/.dtach/<name>`.

tmux appears in exactly one place: the dashboard server itself runs in a
long-lived pane called `roost-web`. No session depends on it.

| kind | defined in | started by | has |
|---|---|---|---|
| **Claude Code** | `config.json` → `folders` | **Start** on its card | terminal, transcript, `/rc` remote-control link |
| **codex** | `~/.dtach/<name>.cmd`, written by `./term.sh` | **Start** on its card | terminal, transcript |

### Running the dashboard

```sh
tmux new-session -d -s roost-web -c ~/src/roost
tmux send-keys -t roost-web ./run.sh Enter    # restarts the server if it exits
./term.sh                                    # the terminal service (ttyd)
```

`server.py` is stdlib-only Python and listens on a **Unix socket**
(`~/.roost/roost.sock`) — no TCP port at all. After a reboot, `./restart.sh` does all of it and brings the
sessions back too.

---

## The dashboard

Each row is a card and a **Start** button.

| session state | Start does |
|---|---|
| not running | starts it from its definition |
| running, remote control off (Claude) | types `/rc <name><suffix>` |
| running | nudges it; the card's snippet says whether anything moved |

Press it again when nothing has moved and it arms: the button turns red and
reads **restart?**, and a second press within a few seconds acts. For a Claude
session that means exiting claude and bringing it back with
`--resume <sessionId>`, so the conversation and its remote-control bridge
survive and your pinned claude.ai link keeps working — the one-tap fix for a
session left stale by `claude update`, or one wedged on a prompt. The
confirmation step is there because a mistap would lose work in flight.

A card's colour is the one thing the caption cannot say at a glance across
fifteen cards:

| colour | the session is |
|---|---|
| green | working |
| blue | idle — finished, waiting for the next instruction |
| purple | stopped **on a question**: a prompt, a permission, a trust dialog |
| grey | down |

For Claude all three come from its own status record (`busy`, `idle`,
`waiting`), which roost already reads — the colours cost nothing beyond a
field that was in the payload anyway.

Codex keeps no such record, and records no event when it asks something: a
session that has stopped to ask looks exactly like one that has finished,
because the transcript stops growing either way. Green and blue come from
that silence (40 seconds, or no transcript at all). **Purple comes from the
screen** — but only while a tab is open on that session. The page watches
its own terminal, and when the screen settles on something that reads as a
question it tells the dashboard, which colours the card and puts the
question on it. Nothing polls: xterm reports when the screen changed, that
is debounced, and only the last dozen rows of the viewport are read.

This deliberately does not read codex's own files. It has them — a state
database, a log with every internal event — and they would answer the
question directly, but they are private, undocumented and version-specific.
The screen is the interface codex actually promises. The server's terminal
relay carries the same bytes and could be tapped instead, with exactly the
same coverage, since both need a tab open; it is read from the page because
a bug in a reader must not be able to break the terminal it is reading.

Status is read-only. For Claude it comes from Claude Code's own
`~/.claude/sessions/<pid>.json` — the same data `/status` shows: idle/busy,
model, and whether a remote-control bridge exists. The snippet under each card
is the session's last answer, read from its transcript. Nothing is ever typed
into a session except by your explicit tap, so watching costs no tokens.

### Names

Every session has an `@name` — the one on its card, beside a chip saying
which agent it runs, **Claude Code** or **codex**. `display_suffix` makes the
name per-machine (`roost2`, `work2` on a machine with suffix `2`), so two
machines' pages are distinguishable and `/rc` names do not collide. Folder
names are never suffixed; the card label and the dtach socket both carry it
(the folder `roost` gets `~/.dtach/roost2`).

### Reordering

A **⠿ grip** for the mouse; with a finger, **hold a card for a third of a
second** and it lifts (a short buzz says so). Move before that and it was a
scroll, release before that and it was a tap. On release the order is written
to `config.json` as `card_order`, so it survives a reload, a restart and a
reboot, and is the same on every device.

`card_order` covers **both** kinds of card. Pinning (the ★) moves a card to
the front of the saved order and keeps the pinned ones together; within each
group dragging decides, and unpinning leaves a card where it now sits. Pointer events are used rather than
HTML5 drag-and-drop, which is unusable on touch. If the page's list has gone
stale — a fork added from another machine — the server refuses the reorder
rather than dropping or duplicating a card, and the page resyncs.

---

## A session page

`/t?name=<session>` — two tabs over the same session.

### Terminal

The live pty, through ttyd. It scrolls with a finger (inertial, with a
rubber-band at the ends) and with a wheel.

- **Paths are links.** Absolute, `~`-relative, relative to the session's own
  folder, and bare filenames — resolved against that folder, then the folders
  below it. They open in the file viewer, in a window of their own. Only
  paths under roots the viewer will actually serve are underlined.
- **Copying** goes through the **copy** button or **ctrl-shift-C**, because
  xterm paints the screen with WebGL rather than emitting markup: there is no
  DOM text for the browser's own Copy to take. In a Claude session, select
  with **shift**-drag — Claude Code turns on mouse reporting, so a plain drag
  belongs to the program.
- **Pasting a picture** uploads it and types its path into the session,
  without pressing Enter, so you can say what to do with it. The **img**
  button does the same from a phone's camera roll. Images land in
  `~/.roost/pasted`, which the viewer can open.
- **Pull past the top** to open the history; **hold it stretched past the
  bottom** for a beat and the terminal reattaches and redraws. Distance is
  the wrong trigger at the end — that is where a flick naturally lands — so
  that one is a hold, and it says "hold to refresh" then "release to refresh"
  while you do it.
- **Reconnecting is automatic.** The terminal's websocket is relayed through
  roost, so restarting the server drops it; ttyd's own retry gives up and waits
  for a keypress, so the page watches the server and reloads the frame when it
  returns. dtach still holds the session, so the same screen comes back.

### History

The real transcript, read from the agent's own log — `~/.claude/projects/…`
for Claude, `~/.codex/sessions/…` for codex. There is no scrollback limit to
run past and nothing is scraped.

**Everything is collapsed except the conversation.** A run buries the thread
under diffs and command dumps, so tool calls and their output start shut,
showing their first line and size. `expand all`, `essential` and `collapse
all` cover the rest, and open entries survive the refresh. Three checkboxes:
**tool calls** (show the noise), **detailed** (headers and borders, or plain
concatenated text), **render as .md**.

Markdown is rendered server-side: headings, lists, tables, code with syntax
colouring, and maths through KaTeX — both libraries served from `vendor/`, not
a CDN, so it works on a tailnet with no route out. Paths and URLs are links
here too, and a GitHub link is offered beside a path that is in a pushed repo.

---

## Files

The viewer renders markdown, JSON, CSV, source, images, PDFs and HTML (in a
sandboxed frame, so a generated report renders as itself and can do nothing
else).

- A **file tree** on the left, resizable, with a location picker listing every
  session's folder plus `src_dir`, `/tmp`, `~/.roost/pasted` and `$HOME`. The
  innermost root wins, so a file inside a session's folder is read against
  that session.
- **Sort by time or by name.** A folder's timestamp is the newest thing
  anywhere inside it, which is what makes "newest first" useful.
- **Three widths** — a reading column, a wider one, the whole window — and a
  reading typeface rather than the interface's.
- **Hover a row** for its path, age, size, session, git root, branch, worktree
  and whether it is untracked, ignored, committed or pushed.
- **⇄ find** puts the tree back around the document you are reading.

What may be served is decided in one place, and that is the security-relevant
part: see [`SECURITY.md`](SECURITY.md).

---

## Colouring a table cell

Markdown has no syntax for it, and letting a document write raw
`<td style=…>` would undo the reason file content is safe to render at all.
So a cell may open with a colour in braces, which is stripped from the text:

```
| Check  | Status            | Note        |
|--------|-------------------|-------------|
| parser | {green} pass      | 6387 tests  |
| linker | {red} fail        | two cases   |
| docs   | {amber} partial   | in review   |
| perf   | {#1f6feb} 12ms    | measured    |
```

Named colours — `red`, `green`, `amber`/`yellow`, `orange`, `blue`,
`purple`, `grey`, `none` — are translucent tints, so they sit on the theme
and leave the text legible. A plain `#rgb` or `#rrggbb` is taken as given.

Anything else in braces is left exactly as written: `{notacolour}` and `{x}`
are text, not markup. Only a name from that list or a valid hex triple ever
reaches the style attribute, so a document cannot smuggle CSS through it.

## Sessions talking to each other

`ccmsg` and the **Forward / Auto-fw / Reply** controls move a session's last
answer into another session through a named slot. The point of the slot is
that neither agent needs to know the other exists: you dump an answer into
"for @app2" and it arrives there with a note saying where it came from. A reply
routes back to whoever asked, once — unless you make the route sticky, and
then the chip turns purple to say so.

---

## Claude Code specifics

### The `/rc` dialog is dismissed for you

`/rc` answers with a Remote Control panel ("Continue" / "Enter to select")
that **blocks the session until someone presses Enter**. Until then it accepts
nothing from the web and looks dead — while the card still shows the bridge is
up. Two sessions were wedged this way, so the button meant to restore remote
control was itself taking sessions off the air.

After sending `/rc`, roost waits for that panel and presses Enter. It matches
on the dialog's own text rather than sending Enter blind, so a stray keystroke
never lands in a half-typed message. If no panel appears the press reports
`(no dialog seen)` and nothing is sent.

### Fallback model

The `||` leg — a folder with no conversation to resume — uses `fallback_model`
from `config.json` (default `opus`). Deliberately not the priciest tier: a
brand-new session in an empty folder is the last place to pay a premium. Set
it to `sonnet` to spend less still. Resumed sessions are unaffected; they keep
the model they were already on.

### Keeping resumed sessions whole

Sessions launch with `--autocompact 1m`. Left at the default, resuming a long
conversation compacts it — the wrong trade for sessions whose value *is* the
accumulated history. 1M is the maximum the flag takes (below 100k is rejected,
and there is no "off"), matching the 1M-context models they run on. A session
already running keeps whatever it started with; use `/autocompact 1m` inside
it to change that.

### Updating Claude Code

`claude update` replaces the binary, but a running session keeps the old code
in memory until it restarts. That surfaces later as:

> API Error: 400 Claude Code 2.1.248 does not support this model; version
> 2.1.251 or newer is required.

The binary is fine there — the *session* is stale.

```sh
./update-claude.sh            # update, then restart idle stale sessions
./update-claude.sh --check    # report only, change nothing
./update-claude.sh --force    # also restart busy ones (loses in-flight work)
./update-claude.sh --only NAME
```

It restarts with `--resume <sessionId>`, never `--continue`, so each session
comes back as the *same* conversation with the same bridge and your pinned
links keep working. Busy sessions are skipped by default, and so is the
session it is running in — restart that one yourself.

---

## codex specifics

codex sessions are not in `config.json`. Each is a definition file under
`~/.dtach/`, written by the **+** button in the top bar, or by `term.sh`:

```sh
./term.sh data 'cd ~/src/data-analysis && codex'              # define; card appears
./term.sh t42  'cd ~/src/docs/t42 && codex resume <id>'       # resume one
./term.sh shell bash                                          # just a shell
./term.sh --list                                              # what is defined
./term.sh --forget <name>                                     # forget one
./term.sh --stop                                              # stop serving them
```

**Start** launches it. A folder codex has not seen before opens on its trust
prompt — answer that once, in the terminal.

codex writes every session to `~/.codex/sessions/<Y>/<M>/<D>/rollout-*.jsonl`:
structured, complete, independent of any terminal. That is what the history
tab reads, and what `/codex` renders as a standalone page listing recent
sessions by folder and time.

---

## Forking a session

When one folder's work outgrows a single session — a paper campaign inside a
project that also has its own evolution to track — give the subtree its own
card, session and remote-control name:

```sh
./fork-session.sh <path-under-src_dir> [name]
./fork-session.sh work/platform/docs notes   # -> card "notes2"
```

It adds the config entry, reloads the dashboard, starts the session and prints
its `/rc` name and URL. The path must already exist — forking splits work that
is on disk; clone-on-Start handles the other case. **Nothing else to update:**
`restart.sh` reads the same `config.json`, so the fork is covered by reboot
recovery immediately.

Two things it leaves to you: it will not answer a trust prompt (the option
order is not stable), and a brand-new session has no transcript, so it shows
no model until it has answered something once.

---

## After a reboot

A reboot takes everything with it. `restart.sh` brings the stack back and is
idempotent, so re-running it is safe:

```sh
./restart.sh              # dashboard + every session
./restart.sh --dash-only  # just the dashboard
```

It adopts the pane you are in, starts the dashboard, re-adds the tailnet
`serve` mapping only if it actually went missing, then presses every card and
reports what came up.

**It will not answer a trust prompt for you.** A folder Claude Code has not
seen stops on "do you trust this folder", and the option order is not stable —
one session showed `No, exit` first, so a canned "1" would exit rather than
trust. The script names any session still lacking remote control and tells you
which one to attach to; read the prompt before answering.

---

## Tailscale — tailnet only, HTTPS only

```sh
sudo tailscale serve --bg --https=8444 unix:$HOME/.roost/roost.sock
tailscale serve status      # prints the URL for this host
```

Open `https://<machine>.<tailnet>.ts.net:8444/` from any tailnet device.
`serve` (as opposed to `funnel`) is reachable only inside the tailnet.

**One mapping, and no others.** There is deliberately no plain-HTTP port: it
was a second door onto the same dashboard and not a secure context, so the
browser withholds the clipboard API there. And there is no mapping for `/term`
or straight to ttyd — either would hand a writable shell to anyone the tailnet
ACL admits, since roost would never see the request and never check
`allow_logins`. `/term` is relayed through the server instead.

The first HTTPS request triggers Let's Encrypt issuance via DNS-01, which can
fail for a while and then succeed. Failures are self-perpetuating: each
attempt burns one of the 5 failed-authorizations-per-hour the rate limit
allows, so a bad hour looks permanent. Wait out the window and re-test before
concluding anything — check the cert with
`openssl s_client -connect <host>:8444 | openssl x509 -noout -dates`.

---

## Who may use it

`allow_logins` in `config.json` is the gate:

```json
"allow_logins": ["you@example.com"]
```

`tailscaled`'s serve proxy stamps `Tailscale-User-Login` on every request it
forwards and **overwrites any copy the client sent**, so the header is safe to
authenticate on — but only because nothing else can reach roost: it listens
on a private Unix socket, not a port. The check runs at
the top of every GET and POST, `/term` included, and every mutating route
additionally requires a same-origin fetch.

**An empty list stops the server from starting**; serving every tailnet peer
is `"allow_anyone": true`, spelled out, and the terminal is a writable shell.
See
[`SECURITY.md`](SECURITY.md) for the whole model, the deny list that keeps
credentials out of the file viewer, and the checklist before standing this up
somewhere new. `/api/whoami` shows what serve is asserting about you.

---

## Config

`./onboarding.sh` writes the first one: it asks for the folder your
repositories live in and the tailnet login allowed in, guesses both, and
leaves the session list empty — the **+** button in the top bar fills it by
browsing that root.

`config.json` is **not tracked** — the dashboard writes `card_order`,
`favorites` and new sessions into it, and one machine's folder list is not
another's.
Run it **before** starting the server: with no config of its own the server
copies `config.example.json`, whose placeholder login refuses everybody, and
`onboarding.sh` then finds a config already there — `--force` rewrites it.
Keys beginning with `_` in the template are notes; the server strips them.

```json
{ "display_suffix": "2", "src_dir": "~/src",
  "allow_logins": ["you@example.com"],
  "folders": ["blog", "work", "tools", "backend", "roost"] }
```

| key | what |
|---|---|
| `socket` | the Unix socket the server listens on (default `~/.roost/roost.sock`) |
| `ttyd_socket` | the terminal service's Unix socket (default `~/.dtach/ttyd.sock`) |
| `src_dir` | where `folders` are resolved from (default `~/src`), and what the **+** picker searches |
| `allow_anyone` | `true` lets the server start with an empty `allow_logins`, serving every tailnet peer. A shell for anyone the ACL admits: say it deliberately or not at all |
| `folders` | the Claude sessions, one card each |
| `allow_logins` | tailnet logins allowed to use the dashboard at all; the server refuses to start with it empty |
| `hosts` | extra `Host` names to accept besides loopback and `*.ts.net` (e.g. a custom domain in front of serve) |
| `display_suffix` | per-machine postfix on card labels and `/rc` names |
| `fallback_model` | model for a session with nothing to resume |
| `clone_base`, `clone_urls` | where a missing folder is cloned from |
| `file_roots` | override the viewer's roots |
| `card_order`, `favorites` | written by the page; do not hand-edit while it runs |

A `folders` entry may be a bare name (resolved to `src_dir/<name>`), a path
relative to `src_dir`, or `{"path": …, "name": …}` when the card should not be
called after the directory:

```json
"folders": ["work", {"path": "work/platform/firmware", "name": "fw"}]
```

Use the explicit form for a nested repo: a bare `"sim"` resolves to
`src_dir/sim`, and since that does not exist the card would *clone a
second copy* there instead of opening the one you have. A `clone_urls` entry
is keyed by the **name**, so an overridden one needs it.

If a folder is missing on this machine, Start clones it first — visibly, in
the session's own terminal — then launches claude. The URL is
`clone_urls[name]` if present, else `clone_base/<name>.git`:

```json
"clone_base": "https://github.com/your-org",
"clone_urls": { "myrepo": "https://github.com/another-org/myrepo.git" }
```

---

## Watching a tmux pane: `/logs`

A leftover from when sessions ran under tmux, and still useful for the one
pane that does: the dashboard's own. `/logs` lists tmux sessions, polls
`capture-pane` every 2s, translates ANSI colour to spans, and has an input box
that sends whole lines. Agent sessions do not appear there any more — they are
under dtach, and their card's terminal tab is the real thing.

---

## License

MIT — see [`LICENSE`](LICENSE). The vendored libraries under `vendor/` keep
their own licenses (KaTeX: MIT; highlight.js: BSD-3-Clause; Inter and the KaTeX fonts:
SIL OFL 1.1).
