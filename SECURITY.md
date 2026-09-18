# Security

roost puts a **writable terminal** and a **file reader** for a developer
workstation on a network. That is the point of it, and it is also the whole
of the risk: anyone who can reach the page and pass its identity check has a
shell as the user running it. Everything below exists to make "can reach" and
"pass" hard, and to be explicit about what is not defended.

Read this before standing it up on a machine that is not your own.

---

## Threat model

**Defended against**

| | |
|---|---|
| the public internet | nothing is exposed to it; `tailscale serve` is tailnet-only |
| another device on the same LAN or Wi-Fi | roost and ttyd listen on Unix sockets, not TCP |
| another local account, a container, or (under WSL2) any Windows process | the sockets sit in `0700` directories; loopback TCP would have been open to all of them |
| another *tailnet* member who is not you | every request carries an identity stamped by `tailscaled`, checked against `allow_logins` |
| a website you visit while logged in | mutating routes and the terminal websocket must be same-origin; pages refuse to be framed by another site; unknown `Host` names are refused (DNS rebinding) |
| a link in a transcript pointing somewhere it should not | path containment, symlink resolution, ownership checks, and an outright refusal for credential files |
| a generated HTML file that wants to be a page | served with a sandbox CSP: an opaque origin, no `connect-src` |

**Not defended against, by design**

- **The agent inside a session.** Claude Code and codex run as you and can do
  anything you can. roost does not sandbox them; it is a window onto them.
- **root, and processes running as you.** They can open the sockets and set
  their own identity header. Both already own the account.
- **You, mistyping.** The terminal is a real terminal.
- **A compromised tailnet identity.** If someone can authenticate to your
  tailnet as you, the identity check will believe them.

---

## The layers, and where they are

### 1. Reachability

`server.py` listens on `~/.roost/roost.sock` and ttyd on `~/.dtach/ttyd.sock`,
both in `0700` directories. Neither has a TCP port. The only route in is a
`tailscale serve` mapping (`unix:` target), which is tailnet-scoped;
`tailscaled` runs as root and can open the socket.

Loopback TCP is **not** private, and roost used to rely on it being so. Any
process that can open `127.0.0.1` can send its own `Tailscale-User-Login`, and
that includes other accounts, host-networked containers and — under WSL2,
where Windows and Linux share loopback — every Windows process. That was
verified on a mirrored-mode WSL2 machine: a Windows `curl.exe` got the
dashboard and a terminal websocket with a forged header.

> **Never `tailscale funnel` any of these ports.** Funnel publishes to the
> internet; serve does not.

### 2. Identity

`tailscaled`'s serve proxy sets `Tailscale-User-Login` on every request it
forwards, **overwriting any copy the client sent** — verified, not assumed: a
request carrying `Tailscale-User-Login: mallory@example.com` arrives with the
real login. `allow_logins` in `config.json` is checked at the top of both
`do_GET` and `do_POST`, before any routing, so it covers every route
including `/term`.

That property is why roost is reachable only through serve. Anything that
could reach it another way — the tailnet IP, a loopback port — could send its
own header.

> **An empty `allow_logins` stops the server from starting.** Serving every
> tailnet peer needs `"allow_anyone": true`, spelled out. The template ships a
> placeholder login, which refuses everyone until you replace it with your
> own. A request carrying more than one `Tailscale-User-Login` header is
> treated as having none. There is deliberately no exemption for
> a request with no identity: a tagged device gets none from serve and is
> refused rather than treated as local.

The terminal is relayed *through* roost (`term_relay`) rather than mapped
straight to ttyd precisely so that it sits behind this check. The cost is that
restarting roost drops every terminal websocket; the page reconnects itself.

### 3. Cross-site

Identity says *who*; it cannot say *who asked*. A page you merely visit could
otherwise make your browser drive the dashboard as you. Every POST requires
`Sec-Fetch-Site` to be absent, `same-origin` or `none` — not `same-site`,
which covers every other machine of the tailnet and every other port here —
and any `Origin` present must match the requested host (`same_site`) —
`X-Forwarded-Host` when serve sent one (it always does for a `unix:` target,
where `Host` is just `localhost`, and it replaces any client copy), else
`Host`. Browsers set both
headers themselves; page script cannot.

- **Cross-site GETs** are refused too, except a top-level visit to `/`:
  another site may link to the dashboard, but may not make your browser
  fetch transcripts, files or icons (a way to spend the server's CPU and
  memory as you).
- **The terminal websocket** is a GET, but it gets the same check: a
  websocket is not covered by CORS, so without it any site could open a
  shell. A websocket handshake with no `Origin` at all is refused. ttyd also
  runs with `-O` (check-origin) as a second lock.
- **Framing.** Every response carries `X-Frame-Options: SAMEORIGIN` and
  `frame-ancestors 'self'` — including file views, raw files, and ttyd's
  own page, whose headers roost adds as it relays them: a click or a
  keystroke inside a frame is a same-origin request, so no other page may
  frame the dashboard or a terminal. Every response also carries `nosniff`
  and `Referrer-Policy: no-referrer`.
- **DNS rebinding.** A rebound name makes `Origin` and `Host` agree on the
  attacker's domain, and a browser on the workstation can reach the loopback
  port with a `Tailscale-User-Login` header of the page's choosing. So the
  `Host` must be loopback, a `*.ts.net` name, or one listed under `"hosts"`
  in `config.json`; anything else is refused before the identity check.

### 4. What the file viewer will serve

One function decides (`_under_root`), and every route goes through it:

1. the path is resolved **through symlinks** before anything else, so a link
   pointing out of a root does not escape;
2. it must sit under a root — each session's own folder, `src_dir`, `/tmp`,
   `$HOME`, `~/.roost/pasted`. A session's folder counts only if it lies
   under `$HOME` or `src_dir` (a definition that says `cd /` does not make
   the filesystem a root);
3. on sticky ground (`/tmp`, which anyone can write to) it must be **owned by
   you**, so another user's files are neither served nor listed;
4. it must not be a credential. Refused wherever they live, not merely hidden
   from listings: `.ssh`, `.gnupg`, `.aws`, `.azure`, `.config`, `.kube`,
   `.docker`, `.terraform.d`, `.password-store`, keyrings, `pki`, the caches
   and agent shell snapshots; `.credentials.json`, `credentials(.toml)`,
   `.netrc`, `.npmrc`, `.pgpass`, `.vault-token`, `.git-credentials`,
   `.claude.json` and its backups, codex's `auth.json` and `config.toml`,
   `.env`, `.env.*`, `.envrc`, `.gitconfig`, Terraform state and `.tfvars`,
   other agents' folders (`.gemini`, `.cursor`, `.copilot`, …); anything ending
   `.pem`, `.key`,
   `.p12`, `.pfx`, `.keystore`, `.ppk`; anything starting `id_rsa`,
   `id_ed25519`, `id_ecdsa`, `id_dsa`; shell histories; and any file with
   more than one hard link (a second name says nothing about what it is).

`$HOME` is a root because work lives outside `src_dir`. That is exactly why
the deny list exists, and why it is enforced in the containment function
rather than in each route. A deny list is only as good as its entries: if you
keep a credential somewhere unusual under `$HOME`, add it to `_DENY_*` in
`server.py`.

### 5. Untrusted content in the page

- Every page is sent with a strict CSP: `default-src 'none'`, scripts and
  style elements only from this origin (which serves them from `vendor/` and
  nowhere else) or carrying the response's own nonce,
  `connect-src 'self'`, `form-action 'none'`. Injected markup could not run
  script even if escaping failed somewhere. Style *attributes* are allowed —
  ANSI colours, table cells and KaTeX use them, with server-built values.
  Markdown is rendered by roost's own subset renderer, which escapes before
  it formats; a link whose text is not its address shows the address's host
  beside it.
- An `.html` file is served with `Content-Security-Policy: sandbox
  allow-scripts; default-src 'none'; …` — an **opaque origin**, so it shares
  nothing with the dashboard, and no `connect-src`, so its script cannot
  fetch and loads nothing from elsewhere. It can still navigate itself, so a
  hostile file can tell a server that it was opened and what it contains —
  but it cannot read anything else.
- Terminal output reaching the dashboard (card snippets) is set with
  `textContent`, and every value embedded in a page script is JSON with `<`,
  `>`, `&`, U+2028 and U+2029 escaped (`<!--<script>` alone would otherwise
  swallow the page).
- Messages passed between agents (`ccmsg`, handover, slots) go only to a
  session that is an agent and nothing else: under dtach, the only
  processes below the session may be `bash -c` wrappers and the agent (an
  interactive shell, a REPL or a leftover background job disqualifies it);
  under tmux, the terminal's foreground process must be the agent. `node`
  counts only when it is running claude or codex. Control characters are
  removed, so quoted text cannot act as keystrokes. The receiving agent
  still reads the text as a prompt, indistinguishable from yours: what one
  agent read can steer another.
- A sticky auto route sends by itself only an answer to a person — never an
  answer to something that was itself forwarded — and at most once a minute,
  so two routes pointing at each other cannot loop.
- **Start** and **restart?** type nothing into a session that is waiting on a
  question (a permission or trust prompt would take the Enter as "yes"), and
  the Enter that clears the `/rc` panel is sent only when the panel's own
  words are on the last lines of the screen.
- Session records and transcripts under `~/.claude` and `~/.codex` are
  written by agents; a malformed one is skipped, not fatal, and an
  unexpected error answers 500 rather than dropping the page.
- Pages load nothing from other hosts: KaTeX, highlight.js and the fonts are
  served from `vendor/`, whose hashes are pinned in `vendor/SHA256SUMS`.

### 6. Git, run on your behalf

Hovering a path runs a few read-only git commands in its repository. A
repository's own `.git/config` can name commands that git runs during an
ordinary `git status` (fsmonitor, clean filters) — so every git call roost
makes switches off fsmonitor and hooks, blanks every filter driver the
repository defines, and ignores submodules. Git metadata is read only when it
is a regular file, so a FIFO planted as `.git/HEAD` cannot hang a thread.

### 7. Files at rest

The server runs with `umask 077` and makes `~/.roost` and `~/.dtach`
private; `config.json` and `slots.json` are replaced atomically, under a
lock. Session definitions in `~/.dtach/*.cmd` are shell commands — keep them
free of secrets anyway.

---

## Standing it up safely

- [ ] `allow_logins` contains your tailnet login, and nobody else's.
- [ ] `tailscale serve status` shows only the ports you intend, and
      `tailscale funnel status` shows nothing.
- [ ] `ss -ltn` shows `:8444` on the tailnet address — that is `tailscaled` —
      and nothing at all for roost or ttyd, which have no port. `ls -ld
      ~/.roost ~/.dtach` shows `drwx------`.
- [ ] `tailscale serve status` maps `:8444` to `unix:…/roost.sock`, not to
      `127.0.0.1`.
- [ ] The machine's own login is protected — roost inherits whatever access the
      account has.
- [ ] You are content that anyone on the tailnet ACL who is also in
      `allow_logins` may run commands as you.

Both checks matter: the tailnet ACL decides who can *reach* the port,
`allow_logins` decides who roost will *answer*. Removing either leaves the
other alone in front of a shell.

---

## Secrets in this repository

There are none, and this has been checked rather than assumed: every blob in
every branch — not just the current tree — was scanned for GitHub, Anthropic,
OpenAI, AWS, Google, Slack and Tailscale key formats, JWTs, private-key PEM
blocks, `Authorization: Bearer` headers and generic `password`/`secret`/
`api_key` assignments. No matches. No `.env`, `*.pem`, `*.key`, `id_rsa*`,
`.netrc` or `.credentials.json` has ever been committed.

The live `config.json` is **not tracked** — the dashboard writes to it, and it
holds one machine's folder list and one person's tailnet login.
`config.example.json` is what ships.

The vendored libraries under `vendor/` are byte-identical to upstream
(`katex.min.js` 0.16.11 and `highlight.min.js` 11.9.0 from cdnjs, verified by
comparison; the fonts are Inter and KaTeX's own faces, both OFL).
`vendor/fetch.sh` re-downloads them if a version ever moves.

---

## Known limitations

- **The tailscale operator setting.** Leave it unset. With
  `tailscale set --operator=$USER`, any process you run — an agent included —
  can `tailscale funnel` the dashboard to the internet without sudo.
- **No rate limiting, no lockout, no audit log.** Requests must arrive within
  30 seconds, but the thread count is not capped. Request logging is
  suppressed to keep the server's pane readable. The record of what was done
  in a session is the session's own transcript.
- **The identity check trusts one header** — correctly, given that only
  serve and your own account can reach the socket, but it is a single
  assumption. `/api/whoami` shows what serve is
  asserting, and is the way to check it on a new machine.
- **`allow_logins` is all-or-nothing.** There are no per-route or read-only
  roles: a login either may drive every session or may not use the dashboard.
- **A message from another agent reads like you typing.** It arrives on the
  same input the owner uses, and the `[forwarded by @name]` prefix is
  ordinary text that any content can contain. An agent that has been steered
  by something it read can steer another agent in your voice.
- **The cards believe the session records.** Status, the snippet, the history
  tab and the forwarding routes are read from the files the agents write
  under `~/.claude` and `~/.codex`. Anything running as you can put words in
  a session's mouth there.
- **The file viewer's `$HOME` root is a deny list.** Known credential files
  are refused wherever they live, but a secret kept somewhere unusual is a
  document like any other. Add it to `_DENY_*` in `server.py`, or narrow
  `file_roots` to the trees you actually read.
- **Sandboxed HTML runs script.** It cannot fetch or reach the dashboard's
  origin, but it executes and can navigate itself (see above).
- **`/tmp` is a root.** Files there are only served if you own them, but the
  contents of your own scratch files are reachable from the viewer.

---

## Reporting

Please report vulnerabilities privately, through GitHub's **Report a
vulnerability** button on the repository's Security tab, rather than in a
public issue. Anything that is not itself sensitive can go in an ordinary
issue.
