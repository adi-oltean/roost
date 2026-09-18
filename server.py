#!/usr/bin/env python3
"""roost — every agent session on this machine, from a phone.

A roost is where the flock settles and you can see all of it at once. One
card per session, Claude Code and codex alike: what it is doing, whether it
has stopped on a question, a snippet of what it said. Tapping one opens its
terminal; the conversation is a tab in there, read from the transcript the
agent writes rather than from the screen.

Also, because a session is a thing you do work with and not just watch: a
file viewer with markdown, maths and syntax highlighting; paths in the
terminal turned into links; images pasted in from a phone; and messages
routed from one session to another.

  browser  ->  roost (this file)  ->  ttyd  ->  dtach  ->  claude | codex

dtach rather than tmux because it does no terminal emulation and no redraw,
so a full-screen TUI renders natively and still survives a disconnect.

Listens on a Unix socket (~/.roost/roost.sock), never on TCP; reach it over
`tailscale serve`, which stamps the caller's identity on every request —
that header is what the allow-list checks. Standard library only: no dependencies, no build, no daemons.
"""

import json
import math
import os
import signal
import struct
import sys
import subprocess
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Everything this process creates -- config, slots, pasted images, session
# definitions -- is the user's alone.
os.umask(0o077)

def write_private(path, text):
    """Replace a file atomically: a reader sees the old or the new, never a
    truncated half, and a crash mid-write leaves the old one in place."""
    path = Path(path)
    tmp = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)

def _read_small(path, limit=65536):
    """A regular file's head, or "" -- never blocks on a FIFO or a device,
    which a checkout could plant where git keeps HEAD."""
    import stat as _stat
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return ""
    try:
        if not _stat.S_ISREG(os.fstat(fd).st_mode):
            return ""
        return os.read(fd, limit).decode("utf-8", "replace")
    except OSError:
        return ""
    finally:
        os.close(fd)

def _jobj(text):
    """A JSON object, or None. Files under ~/.claude and ~/.codex are written
    by agents, so a record may be anything: null, a list, a number where a
    string belongs. One bad file must not take the dashboard down."""
    try:
        o = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return o if isinstance(o, dict) else None

_REC_TYPES = {"pid": int, "cwd": str, "tmux": str, "sessionId": str,
              "name": str, "status": str, "bridgeSessionId": str,
              "statusUpdatedAt": (int, float)}

def session_record(path):
    """~/.claude/sessions/<pid>.json with every known field of the right type
    (a wrong-typed field is dropped), or None."""
    try:
        o = _jobj(path.read_text())
    except OSError:
        return None
    if o is None:
        return None
    for k, t in _REC_TYPES.items():
        if k in o and (not isinstance(o[k], t) or isinstance(o[k], bool)):
            del o[k]
    sid = o.get("sessionId", "")
    if sid and not all(c.isalnum() or c in "-_" for c in sid):
        del o["sessionId"]
    return o

import threading as _thr
_CFG_LOCK = _thr.RLock()        # config.json: read-modify-write
_SLOTS_LOCK = _thr.RLock()      # ~/.roost/slots.json: same

def _locked(lock):
    def wrap(fn):
        def inner(*a, **k):
            with lock:
                return fn(*a, **k)
        inner.__name__, inner.__doc__ = fn.__name__, fn.__doc__
        return inner
    return wrap

for _d in (Path.home() / ".roost", Path.home() / ".dtach"):
    try:
        _d.mkdir(mode=0o700, exist_ok=True)
        os.chmod(_d, 0o700)
    except OSError:
        pass

# The live config is not in version control: the dashboard writes card_order
# and favorites back to it, and one machine's folder list is not another's. A
# fresh clone therefore starts from the template rather than from a traceback.
# This runs before anything else reads the file -- ccmsg, imported below,
# reads it too.
if not (ROOT / "config.json").exists() and (ROOT / "config.example.json").exists():
    write_private(ROOT / "config.json", (ROOT / "config.example.json").read_text())
    print("config.json created from config.example.json -- edit it "
          "(allow_logins and folders at least), then restart")

# The messaging bridge is a script first and a route second, so that an agent
# can reach another agent with or without this server up.
import importlib.machinery as _ilm
import importlib.util as _ilu
_spec = _ilu.spec_from_loader("ccmsg", _ilm.SourceFileLoader(
    "ccmsg", str(Path(__file__).resolve().parent / "ccmsg")))
ccmsg = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(ccmsg)

def _build_id():
    """Short commit + the file's own mtime: the commit alone would not move
    while a change is uncommitted, which is exactly when it is being tested."""
    sha = ""
    try:
        sha = subprocess.run(["git", "-C", str(Path(__file__).resolve().parent),
                              "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        pass
    stamp = time.strftime("%H:%M", time.localtime(Path(__file__).stat().st_mtime))
    return (sha + "@" + stamp) if sha else stamp

BUILD = _build_id()

# Changes on every start. /term is relayed through this process now, so a
# restart drops every terminal websocket -- something that never happened
# when tailscale serve proxied straight to ttyd. The page watches this and
# reloads the terminal by itself rather than leaving a dead pane.
import uuid as _uuid
SERVER_ID = _uuid.uuid4().hex[:8]
SESS_DIR = Path.home() / ".claude" / "sessions"
CFG = json.loads((ROOT / "config.json").read_text())
# Keys beginning with "_" are the template's own notes to the reader.
CFG = {k: v for k, v in CFG.items() if not k.startswith("_")}
# Unix sockets, not loopback TCP. Anything that can open 127.0.0.1 can send
# its own Tailscale-User-Login header, and "anything" is wider than it looks:
# under WSL2 every Windows process shares the loopback interface, other
# accounts on the machine share it, and so does every container with host
# networking. A socket in a 0700 directory is reachable by this account and
# by root (tailscaled) and by nothing else.
SOCK = Path(CFG.get("socket", "~/.roost/roost.sock")).expanduser()
TTYD_SOCK = Path(CFG.get("ttyd_socket", "~/.dtach/ttyd.sock")).expanduser()
SRC = Path(CFG.get("src_dir", "~/src")).expanduser()
SRC.mkdir(parents=True, exist_ok=True)  # brand-new machine: clones land here

def _folders(entries):
    """name -> absolute path.

    An entry is either a folder directly under src_dir ("blog") or a path
    relative to it ("work/platform/sim") for a repo checked out inside
    another workspace. The basename becomes the button label and the tmux
    session name, so nesting stays invisible everywhere but the path.

    An entry may instead be {"path": ..., "name": ...} to override that name —
    for a directory whose on-disk name isn't what you want to see on a button
    or type after /rc (firmware on disk, "fw" here, so the button reads
    fw2 and remote control answers to fw2)."""
    out = {}
    for entry in entries:
        if isinstance(entry, dict):
            rel, name = entry["path"].strip("/"), entry["name"]
        else:
            rel = entry.strip("/")
            name = Path(rel).name
        if name in out:
            raise SystemExit(f"config.json: two folders both named {name!r}")
        out[name] = SRC / rel
    return out


FOLDERS = _folders(CFG.get("folders") or [])
# Cosmetic postfix on button labels (e.g. "2" -> "roost2") to tell this
# machine's page apart from another machine's; tmux/folder names unchanged.
SUFFIX = CFG.get("display_suffix", "")
# Where to clone a folder from when it's missing on this machine:
# clone_urls[name] if set, else clone_base/<name>.git.
CLONE_BASE = CFG.get("clone_base", "").rstrip("/")
CLONE_URLS = CFG.get("clone_urls", {})

CLAUDE_CMDS = {"claude", "node"}

# Resume the folder's last conversation; if there is none to continue,
# fall back to a fresh session on fallback_model.
#
# --autocompact 1m keeps the resumed conversation whole. The default ("auto")
# lets Claude Code compact on resume, which is the wrong trade here: these
# sessions are long-lived and their value is the accumulated history. 1M is the
# ceiling the flag accepts (values below 100k are rejected, and there is no
# "off"), and it matches the 1M-context model these sessions run on.
AUTOCOMPACT = "--autocompact 1m"
# Model for the fallback leg only — a folder with no conversation to resume.
# Not fable: it is the priciest tier ($10/$50 per MTok vs opus $5/$25, sonnet
# $3/$15), and a brand-new session in an empty folder is the last place that
# premium is worth paying. Resumed sessions are unaffected — they keep whatever
# model they were already on.
FALLBACK_MODEL = CFG.get("fallback_model", "opus")
LAUNCH = (f"claude --continue {AUTOCOMPACT} || "
          f"claude --model {FALLBACK_MODEL} {AUTOCOMPACT}")

# name -> {"t": press time, "hash": pane snapshot at press}. If the pane's
# transcript region hasn't changed since a press, the session isn't replying;
# the page shows "no reply since HH:MM". In-memory only — best effort.
PRESSED = {}


def tmux(*args):
    """Run a tmux command; return (exit_code, stdout)."""
    try:
        p = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=5)
        return p.returncode, p.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return 1, ""



# ------------------------------------------------- tmux or dtach, per session
# A session lives under one or the other. tmux was the original and can be
# read with capture-pane; dtach cannot be read at all, which is why every
# screen-dependent thing above was replaced with a signal that does not need
# one. These four answer "which is it" and route accordingly, so the two
# kinds can coexist while sessions are moved across one at a time.

def dtach_sock(name):
    return Path.home() / ".dtach" / (name + SUFFIX)

def is_dtach(name):
    """True when this session is supervised by dtach rather than tmux."""
    try:
        return dtach_sock(name).is_socket() and _session_cmd(name) and \
            tmux("has-session", "-t", f"={name}")[0] != 0
    except OSError:
        return False

def _dtach_push_path(sock, text, newline=True):
    import socket as _s
    data = (text + ("\r" if newline else "")).encode()
    c = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
    try:
        c.connect(str(sock))
        for i in range(0, len(data), 8):
            chunk = data[i:i + 8]
            c.sendall(bytes([0, len(chunk)]) + chunk.ljust(8, b"\0"))
    finally:
        c.close()

def state_of(name):
    """dead | shell | claude, whichever supervisor is holding the session."""
    if is_dtach(name):
        # No pane to inspect: a live claude process for this folder is the
        # only thing that says "claude", and the socket says the wrapper is
        # still there.
        return "claude" if claude_infos(name, FOLDERS.get(name, Path.home())) \
            else "shell"
    rc, _ = tmux("has-session", "-t", f"={name}")
    if rc != 0:
        return "dead"
    _, cmd = tmux("display-message", "-p", "-t", f"{name}:", "#{pane_current_command}")
    return "claude" if cmd in CLAUDE_CMDS else "shell"


def pane_hash(name):
    """Did this session move? Under dtach there is no pane, so the answer
    comes from the transcript growing instead of the screen changing."""
    if is_dtach(name):
        infos = claude_infos(name, FOLDERS.get(name, Path.home()))
        sid = infos[0].get("sessionId") if infos else ""
        f = claude_path(sid) if sid else None
        try:
            return "%d" % f.stat().st_size if f else ""
        except OSError:
            return ""
    return _pane_hash_tmux(name)

def _pane_hash_tmux(name):
    """Hash of the pane's transcript region (input box + status bar excluded,
    so idle repaints don't count as a reply)."""
    _, txt = tmux("capture-pane", "-p", "-t", f"{name}:")
    return hash("\n".join(txt.splitlines()[:-6]))


def _owner(cwd, tmux_sess):
    """Which configured folder a session record belongs to, or None.

    The tmux session name wins when it matches a folder. Otherwise the folder
    whose path is the *longest* prefix of the session's cwd owns it: a repo
    checked out inside another workspace (work/platform/sim) sits under
    the parent's path too, and without the longest-match rule the parent would
    also claim the child's session — reporting the wrong remote-control link
    and a phantom second session on the parent's button."""
    if tmux_sess in FOLDERS:
        return tmux_sess
    best, best_len = None, -1
    for n, p in FOLDERS.items():
        s = str(p)
        if (cwd == s or cwd.startswith(s + "/")) and len(s) > best_len:
            best, best_len = n, len(s)
    return best


def claude_infos(name, path):
    """Live status straight from Claude Code's own ~/.claude/sessions/<pid>.json
    (what /status shows), read-only: nothing is typed into the session.
    Returns every record whose process is alive and belongs to this tmux
    session or folder, freshest first — more than one means parallel sessions."""
    recs = []
    for f in SESS_DIR.glob("*.json"):
        j = session_record(f)
        if not j or "pid" not in j:
            continue
        try:
            comm = Path(f"/proc/{j['pid']}/comm").read_text().strip()
        except OSError:
            continue
        if comm != "claude":
            continue  # stale file from a dead or recycled pid
        if _owner(j.get("cwd", ""), j.get("tmux", "").split(":")[0]) != name:
            continue
        recs.append(j)
    recs.sort(key=lambda j: j.get("statusUpdatedAt", 0), reverse=True)
    return recs


def model_of(j):
    """Model the session is on, e.g. "fable 5". The status file doesn't carry
    it, but the session's transcript jsonl does — read the tail and take the
    newest main-chain assistant message (subagents may run other models)."""
    sid = j.get("sessionId", "")
    f = claude_path(sid) if sid else None
    if not f:
        return ""
    try:
        with open(f, "rb") as fh:
            fh.seek(max(fh.seek(0, 2) - 65536, 0))
            tail = fh.read().decode(errors="replace")
    except OSError:
        return ""
    for line in reversed(tail.splitlines()):
        obj = _jobj(line)
        msg = obj.get("message") if obj else None
        model = msg.get("model") if isinstance(msg, dict) else ""
        if isinstance(model, str) and model and not obj.get("isSidechain"):
            return model.removeprefix("claude-").replace("-", " ")
    return ""


def claude_last_said(name, limit=200):
    """The last thing this session actually said, from its own transcript.

    Better than scraping the pane even where a pane exists: the screen holds
    a spinner and a token counter, and this holds the sentence."""
    infos = claude_infos(name, FOLDERS.get(name, Path.home()))
    sid = infos[0].get("sessionId") if infos else ""
    f = claude_path(sid) if sid else None
    if not f:
        return ""
    c = rollout_conv(f)
    for role, body in reversed(c["rows"][-40:]):
        if role == "assistant" and (body or "").strip():
            return " ".join(body.split())[:limit]
    return ""

def snippet(name):
    """The last two meaningful lines of what the session said, ~200 chars.
    A dtach session is read from its transcript, which is the usual case; a
    tmux session is read from its pane. Either way it costs nothing and
    touches nothing."""
    if is_dtach(name):
        return claude_last_said(name)
    _, txt = tmux("capture-pane", "-p", "-t", f"{name}:")
    lines = [l.strip() for l in txt.splitlines()[:-6]]
    lines = [l for l in lines
             if l and not l.startswith(("❯", "─", "⏵")) and set(l) != {"─"}]
    return " ".join(lines[-2:])[:200]


def note_for(name, st):
    """'no reply since HH:MM' if a press went unanswered, else ''."""
    rec = PRESSED.get(name)
    if not rec:
        return ""
    if st != "claude" or pane_hash(name) != rec["hash"]:
        del PRESSED[name]  # session reacted (or state changed) — clear
        return ""
    return time.strftime("no reply since %H:%M", time.localtime(rec["t"]))


def type_into(name, text):
    """Type text into the session and press Enter (small pause so
    Claude Code's slash-command menu settles before submit)."""
    if is_dtach(name):
        _dtach_push_path(dtach_sock(name), text)
        return
    tmux("send-keys", "-t", f"{name}:", "-l", text)
    time.sleep(0.4)
    tmux("send-keys", "-t", f"{name}:", "Enter")

def paste_into(name, text):
    """Type text into the session and stop there.

    An image's path is the start of a sentence, not the whole of it: you
    almost always want to say what to do with it before submitting."""
    if is_dtach(name):
        _dtach_push_path(dtach_sock(name), text, newline=False)
        return
    tmux("send-keys", "-t", f"{name}:", "-l", text)

def send_enter(name):
    if is_dtach(name):
        _dtach_push_path(dtach_sock(name), "")
        return
    tmux("send-keys", "-t", f"{name}:", "Enter")


def dismiss_rc_dialog(name, tries=8):
    """Clear the modal that /rc leaves on screen.

    /rc answers with a Remote Control panel ("Continue" / "Enter to select")
    that blocks the session until someone presses Enter — until then it accepts
    nothing from the web and looks dead, while the dashboard still shows green
    because the bridge is up. Two sessions were wedged this way, so the button
    that restores remote control was itself taking sessions off the air.

    Not sent blind: an Enter into whatever else happened to be on screen
    could submit a half-typed message or answer a different prompt.

    The cue is the session's own record, not the screen. Claude Code sets
    status to "waiting" the moment the modal goes up and back to "idle" the
    moment it is dismissed -- measured at 0.10s and 0.11s either side, in
    step with the pane. That matters twice over: it is the one signal that
    survives without a terminal to read, which is what /rc needs if these
    sessions ever move off tmux; and it does not depend on matching the
    dialog's wording, which belongs to Claude Code and can change under us.

    The pane is still consulted when there is one, as a second opinion --
    "waiting" on its own can mean any prompt, and this runs right after
    typing /rc, when it should mean this one.
    """
    for _ in range(tries):
        time.sleep(1)
        if not rc_modal_up(name):
            continue
        send_enter(name)
        return True
    return False


def waiting_on_question(name):
    """True when the session is stopped on a prompt of any kind. Nothing is
    typed into such a session: an Enter would answer it, and the highlighted
    answer to a permission or trust question is usually "yes"."""
    infos = claude_infos(name, FOLDERS.get(name, Path.home()))
    return bool(infos) and infos[0].get("status") == "waiting"


def rc_modal_up(name):
    """Is the /rc panel -- and not some other prompt -- up right now?

    Only called after /rc was typed into a session that was not waiting, so
    "waiting" now is the panel's doing. Where a pane can be read, the panel's
    own words must be in its last lines as well; text elsewhere on screen
    (a file name, a command) does not count."""
    infos = claude_infos(name, FOLDERS.get(name, Path.home()))
    if not infos or infos[0].get("status") != "waiting":
        return False
    rc, txt = tmux("capture-pane", "-p", "-t", f"{name}:")
    if rc == 0 and txt.strip():
        tail = "\n".join([x for x in txt.splitlines() if x.strip()][-8:])
        return "Remote Control" in txt and "Enter to select" in tail
    return True                      # no pane: the transition decides



# --- who is reading a terminal ------------------------------------------
# A dtach session has one pty, one size, and the last client to attach sets
# it. Two pages open on the same session therefore fight over the geometry,
# and the one not holding it draws its bottom rows at the wrong height: a
# clipped composer, garbled last lines. Nothing is gained by two live
# readers, so there is one. Opening or refreshing a page claims the
# terminal; a page that held it before stands down and says so.
#
# In memory on purpose. A reader that outlives a restart is a reader nobody
# can find, and after a restart every page reconnects anyway.
TERM_READER = {}                 # session name -> the reader's id

def term_clients(sock):
    """Every attached dtach client for this socket, and never the master.

    A client has no children; the master is the one running the program.
    That is the only distinction needed here, and /proc can answer it --
    which beats matching patterns against command lines, since the cost of
    being wrong is somebody's session.
    """
    out = []
    want = str(sock).encode()
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            argv = (d / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if len(argv) > 2 and argv[0].endswith(b"dtach") and \
           argv[1] in (b"-a", b"-A") and argv[2] == want:
            pid = int(d.name)
            if not _children_of(pid):        # no children: a client, not a master
                out.append(pid)
    return out

def claim_term(name, cid):
    """Hand the terminal to one page and disconnect everyone else.

    Saying who holds it is not enough. A page loaded before this existed
    never asks, and a page whose browser is gone cannot answer -- and both
    keep a connection open, which means both keep imposing a window size on
    the one pty everyone shares. So the claim evicts: the clients go, the
    master stays, and the claiming page connects afterwards.
    """
    TERM_READER[name] = cid
    sock = Path.home() / ".dtach" / name
    if not sock.is_socket():
        return cid
    for pid in term_clients(sock):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    return cid

def dtach_cmd_path(name):
    return Path.home() / ".dtach" / (name + SUFFIX + ".cmd")

def _children_of(pid):
    """Direct children of a pid, from /proc. No ps, no shell, no pattern."""
    out = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            st = (d / "stat").read_text()
        except OSError:
            continue
        # comm can hold spaces and brackets, so the fields after it are read
        # from the last ")" rather than by splitting the whole line.
        tail = st[st.rfind(")") + 1:].split()
        if len(tail) > 1 and tail[1] == str(pid):
            out.append(int(d.name))
    return out

def dtach_tree(sock):
    """The dtach master holding `sock`, and everything it is running.

    Killing the master alone is not enough: dtach's child is its own session
    leader, so it survives the master's death with a deleted pty -- alive,
    unreachable, and still holding whatever locks it held. A codex orphaned
    that way refuses the next resume with "this conversation is open in
    another app", which is a restart that quietly does not restart.
    """
    master = None
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            argv = (d / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        # The socket path as its own argument, next to the mode flag: an
        # exact match on this session, not a pattern over a command line.
        #
        # Any of -n, -A or -a, because what makes a master is running the
        # program, not which flag started it. attach.sh creates them with -n
        # today, but a tab that opened a session back when it used -A is a
        # master too -- and matching only -n meant that session's program
        # never got the clean SIGTERM a restart owes it.
        if len(argv) > 2 and argv[0].endswith(b"dtach") and \
           argv[1] in (b"-n", b"-A", b"-a") and argv[2] == str(sock).encode() \
           and _children_of(int(d.name)):
            master = int(d.name)
            break
    if master is None:
        return [], []
    kids, seen = [], set()
    queue = [master]
    while queue:
        pid = queue.pop()
        for c in _children_of(pid):
            if c not in seen:
                seen.add(c)
                kids.append(c)
                queue.append(c)
    return [master], kids

def start_terminal(term):
    """Start any dtach session from its own .cmd, by terminal name.

    One definition per session, whether it runs codex or claude: the same
    file the browser terminal attaches to. Starting it here rather than by
    opening a terminal means a stopped session can be brought back from the
    card without a browser tab."""
    d = Path.home() / ".dtach"
    sock, cmdf = d / term, d / (term + ".cmd")
    try:
        cmd = cmdf.read_text().strip()
    except OSError:
        return False
    if sock.exists() and not sock.is_socket():
        return False
    if sock.is_socket():
        return True                      # already up; attaching is the browser's job
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CLAUDE") and k not in ("TMUX", "TMUX_PANE")}
    subprocess.run(["dtach", "-n", str(sock), "-r", "winch", "bash", "-lc", cmd],
                   capture_output=True, text=True, timeout=20, env=env)
    return True

def start_dtach(name):
    """Bring up a dtach-supervised Claude session from its own .cmd."""
    sock = dtach_sock(name)
    try:
        cmd = dtach_cmd_path(name).read_text().strip()
    except OSError:
        return False
    if sock.exists() and not sock.is_socket():
        return False
    # A session started from inside another Claude session inherits markers
    # -- CLAUDE_CODE_CHILD_SESSION and friends -- and Claude Code then treats
    # the new one as a child: it writes no transcript and no session record,
    # so it has no history tab and the dashboard cannot see its status. It
    # says so on screen and is easy to miss. The server's own environment is
    # clean today; this makes it not matter whose it is.
    # TMUX too: the server runs inside a tmux pane, and a child that sees
    # TMUX records itself as living in that pane. The record is what the
    # dashboard reads, so a dtach session was claiming to be in the
    # dashboard's own window.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CLAUDE") and k not in ("TMUX", "TMUX_PANE")}
    subprocess.run(["dtach", "-n", str(sock), "-r", "winch",
                    "bash", "-lc", cmd],
                   capture_output=True, text=True, timeout=20, env=env)
    return True

def _session_cmd(name):
    """This session's own dtach definition exists -- as opposed to the
    attach-to-tmux terminal that ensure_claude_term writes under the same
    name, which starts a tmux client and never a claude."""
    txt = _read_small(dtach_cmd_path(name))
    return bool(txt.strip()) and "tmux new-session" not in txt


def press(name):
    """The one button action: revive whatever layer is down."""
    st = state_of(name)
    if st == "claude" and waiting_on_question(name):
        return "it is waiting on a question — answer it in the terminal"
    path = FOLDERS[name]
    if st == "dead":
        if not path.exists():
            # Fresh machine: clone inside the pane (non-blocking, progress
            # visible in the button snippet), then launch claude in it.
            url = CLONE_URLS.get(name) or (CLONE_BASE and f"{CLONE_BASE}/{name}.git")
            if not url:
                return f"{path} missing and no clone_base configured"
            # Clone into the entry's own parent, so a nested checkout lands
            # beside its siblings rather than at the top of src_dir.
            parent = path.parent
            parent.mkdir(parents=True, exist_ok=True)
            tmux("new-session", "-d", "-s", name, "-c", str(parent))
            time.sleep(0.3)
            import shlex
            type_into(name, "git clone -- %s %s && cd %s && (%s)"
                      % (shlex.quote(url), shlex.quote(name),
                         shlex.quote(name), LAUNCH))
            return f"cloning {name}, then launching Claude Code"
        if _session_cmd(name):
            start_dtach(name)
            return "dtach session started, Claude Code launched"
        tmux("new-session", "-d", "-s", name, "-c", str(path))
        time.sleep(0.3)
        type_into(name, LAUNCH)
        return "tmux restarted, Claude Code launched"
    if st == "shell":
        # A dtach session whose claude has exited: restart it from its own
        # definition. Typing the launch line into the socket instead was how
        # three sessions ended up running inside a leftover terminal
        # attachment rather than in a session of their own.
        if _session_cmd(name) and not is_dtach(name):
            start_dtach(name)
            return "dtach session started, Claude Code launched"
        type_into(name, LAUNCH)
        return "Claude Code launched"
    snap = pane_hash(name)
    # Canonical remote-control name: repo name + machine postfix ("roost2" on
    # a machine with suffix "2", bare "roost" on machines with no display_suffix configured).
    type_into(name, f"/rc {name}{SUFFIX}")
    PRESSED[name] = {"t": time.time(), "hash": snap}
    dismissed = dismiss_rc_dialog(name)
    return f"/rc {name}{SUFFIX} sent" + ("" if dismissed else " (no dialog seen)")


def restart(name):
    """Exit the session's claude and bring it back on the same conversation.

    Resumes by sessionId rather than --continue: the remote-control bridge
    follows the conversation, so a pinned claude.ai link keeps working. This is
    what makes the button safe to use after a `claude update` — the binary is
    replaced on disk but a running session keeps the old code until it restarts.
    """
    path = FOLDERS[name]
    infos = claude_infos(name, path)
    sid = infos[0].get("sessionId", "") if infos else ""
    if infos and infos[0].get("status") == "waiting":
        return "it is waiting on a question — answer it in the terminal"

    if is_dtach(name):
        # The session is the dtach socket: /exit ends claude, the wrapper
        # ends with it, and the definition starts it again. There is no
        # shell to type a resume line into.
        if state_of(name) == "claude":
            type_into(name, "/exit")
            for _ in range(30):
                time.sleep(1)
                if not is_dtach(name) or state_of(name) != "claude":
                    break
            else:
                return "still running — could not stop it"
        if is_dtach(name):
            return "Claude Code exited but its session is still up — use Start"
        start_dtach(name)
        return "restarted from its definition"

    if state_of(name) == "claude":
        type_into(name, "/exit")           # not C-c: that only clears the input
        for _ in range(30):                # wait for the shell to come back
            time.sleep(1)
            _, cmd = tmux("display-message", "-p", "-t", f"{name}:",
                          "#{pane_current_command}")
            if cmd not in CLAUDE_CMDS:
                break
        else:
            return "still running — could not reach a shell"

    if state_of(name) == "dead":
        tmux("new-session", "-d", "-s", name, "-c", str(path))
        time.sleep(0.3)

    if sid:
        type_into(name, f"claude --resume {sid} {AUTOCOMPACT}")
        return f"restarted on {sid[:8]} (link preserved)"
    type_into(name, LAUNCH)
    return "restarted (no prior conversation to resume)"


_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
              ".mypy_cache", ".pytest_cache", "target", "dist", "build",
              ".next", ".cache", "vendor"}


def _is_repo(d):
    try:
        return (d / ".git").exists()
    except OSError:
        return False


def repo_tree(limit=400, depth=5, budget=8000):
    """Every repository under src_dir, and the folders that lead to one.

    Depth-limited, pruned and capped: src_dir is somebody's whole working
    life and this runs while a picker is opening. A checkout inside a
    checkout is found -- that is how a workspace holding several repos looks
    -- but a repository's own .git is never walked.

    Folders with no repository beneath them are left out: every branch here
    leads somewhere, which is what makes the tree worth scrolling."""
    root = SRC.resolve()
    have = {str(x) for x in FOLDERS.values()}
    found, stack, seen, cut = [], [(root, 0)], 0, False
    while stack:
        d, lvl = stack.pop()
        try:
            kids = sorted(d.iterdir(), key=lambda x: x.name.lower())
        except OSError:
            continue
        for x in kids:
            seen += 1
            if seen > budget or len(found) >= limit:
                cut = True
                break
            if x.name.startswith(".") or not x.is_dir():
                continue
            if _is_repo(x):
                found.append(x)
            if lvl + 1 < depth and x.name not in _SKIP_DIRS:
                stack.append((x, lvl + 1))
        if cut:
            break

    # Fold the paths into a tree. A node exists because a repository is at it
    # or under it, so "children" is never a dead end.
    nodes = {}

    def node(path):
        key = str(path)
        if key not in nodes:
            nodes[key] = {"name": path.name, "path": key, "repo": False,
                          "added": key in have, "kids": []}
            if path != root:
                node(path.parent)["kids"].append(nodes[key])
        return nodes[key]

    node(root)
    nodes[str(root)]["name"] = root.name
    for r in sorted(found, key=lambda x: str(x).lower()):
        node(r)["repo"] = True

    def tidy(n):
        n["kids"].sort(key=lambda k: (not k["repo"], k["name"].lower()))
        for k in n["kids"]:
            tidy(k)
        return n

    return {"root": str(root), "nodes": tidy(nodes[str(root)])["kids"],
            "repos": len(found), "cut": cut}


def add_session(path, name="", agent="claude"):
    """Make a new session for a folder. Returns (card name, error).

    The two agents are kept where they already live rather than given a
    common form: a Claude Code session is an entry in config.json, pressed
    into a session the way every other card is, and a codex session is a
    definition in ~/.dtach that the terminal service starts. That is what
    each one already was before there was a button for it."""
    try:
        d = Path(path).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        return "", "not a path"
    if not d.is_dir():
        return "", "no such folder"
    if not d.is_relative_to(SRC.resolve()):
        return "", "outside %s" % SRC
    name = (name or d.name).strip()
    if not name or not all(c.isalnum() or c in "-_" for c in name):
        return "", "a name may hold letters, digits, - and _ only"
    if name in card_names():
        return "", "there is already a card called %s" % name
    if agent not in ("claude", "codex"):
        return "", "unknown agent"
    cmdf = Path.home() / ".dtach" / (name + ".cmd")
    if agent == "codex":
        if cmdf.exists():
            return "", "there is already a terminal called %s" % name
        import shlex
        # Exactly what term.sh writes, so a session made here and one made
        # from a shell are the same thing afterwards.
        write_private(cmdf, "export ROOST_NAME=%s; cd %s && codex"
                      % (name, shlex.quote(str(d))))
        return "term:" + name, ""
    if cmdf.exists():
        return "", "there is already a terminal called %s" % name
    rel = str(d.relative_to(SRC.resolve()))
    entry = rel if Path(rel).name == name else {"path": rel, "name": name}
    with _CFG_LOCK:
        cfg = json.loads((ROOT / "config.json").read_text())
        folders = cfg.get("folders") or []
        folders.append(entry)
        cfg["folders"] = folders
        write_private(ROOT / "config.json", json.dumps(cfg, indent=2) + "\n")
        FOLDERS.clear()
        FOLDERS.update(_folders(folders))
    return name, ""


def card_names():
    """Every card the dashboard shows, by the name the page uses for it."""
    return list(FOLDERS) + [e["name"] for e in term_entries()]

@_locked(_CFG_LOCK)
def reorder(names):
    """Persist a new card order and apply it without a restart.

    The order covers BOTH kinds of card. It used to be stored as the order of
    config.json's "folders", which describes configured Claude sessions and
    nothing else -- so a list containing a codex terminal ("term:t42") never
    matched, every drag was refused, and the card sprang back to where it
    started. That is the whole bug: the dragging worked, the saving did not.

    config.json stays the single source of truth -- restart.sh and
    fork-session.sh read the same file -- so "folders" keeps the relative
    order of the sessions it does describe, and "card_order" records the full
    arrangement. Entries keep whichever form they had (plain string or
    {path, name})."""
    cfg = json.loads((ROOT / "config.json").read_text())
    by_name = {}
    for e in cfg.get("folders") or []:
        n = e["name"] if isinstance(e, dict) else Path(str(e).strip("/")).name
        by_name[n] = e
    if set(names) != set(card_names()):
        # A stale page (a fork added elsewhere, a terminal that has since been
        # defined) must not silently drop or duplicate a card -- refuse and
        # let the client reload.
        return False, "order does not match the dashboard; refresh and retry"
    cfg["card_order"] = list(names)
    cfg["folders"] = [by_name[n] for n in names if n in by_name]
    write_private(ROOT / "config.json", json.dumps(cfg, indent=2) + "\n")
    fresh = _folders(cfg["folders"])
    FOLDERS.clear()
    FOLDERS.update(fresh)   # dicts keep insertion order, which is button order
    return True, "order saved"


# ---------------------------------------------------------------- log viewer
# /rc is Claude-specific: it needs a session that understands the command and
# an Anthropic bridge. Anything else in tmux — codex, a build, a long test —
# has no such channel. But tmux itself can always be read, so a read-only pane
# view covers every session uniformly, with no agent cooperation at all.

import html as _html
import re as _re

def _js(v):
    """A value as a JavaScript literal that is safe inside <script>: no "<"
    at all ("<!--<script>" alone swallows the rest of the page), and none of
    the characters that end a line in JavaScript but not in JSON."""
    return (json.dumps(v).replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026").replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))

_SGR = _re.compile(r"\x1b\[([0-9;]*)m")
_OTHER_ESC = _re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][A-B]")
_BASIC = ["#000000", "#cc4436", "#3fa34a", "#c9a227", "#4a7fd4", "#a45ec4",
          "#3fa0a0", "#c8c8c8"]


def _sgr_to_style(params, cur):
    """Fold one SGR parameter list into the running (fg, bg, bold) state."""
    fg, bg, bold = cur
    codes = [int(c) for c in params.split(";") if c != ""] or [0]
    i = 0
    while i < len(codes):
        c = codes[i]
        if c == 0:
            fg, bg, bold = None, None, False
        elif c == 1:
            bold = True
        elif c == 22:
            bold = False
        elif c in (38, 48) and i + 1 < len(codes) and codes[i + 1] == 2:
            rgb = codes[i + 2:i + 5]
            if len(rgb) == 3:
                col = "#%02x%02x%02x" % tuple(rgb)
                if c == 38:
                    fg = col
                else:
                    bg = col
            i += 4
        elif 30 <= c <= 37:
            fg = _BASIC[c - 30]
        elif 90 <= c <= 97:
            fg = _BASIC[c - 90]
        elif c == 39:
            fg = None
        elif 40 <= c <= 47:
            bg = _BASIC[c - 40]
        elif c == 49:
            bg = None
        i += 1
    return fg, bg, bold


def ansi_to_html(text):
    """Render a pane's escape sequences as spans. Content is HTML-escaped
    first, so terminal output can never inject markup — the same hazard the
    embedded status JSON hit with a literal </script>."""
    out, cur, open_span = [], (None, None, False), False
    for chunk in _re.split(r"(\x1b\[[0-9;]*m)", text):
        m = _SGR.fullmatch(chunk)
        if m:
            cur = _sgr_to_style(m.group(1), cur)
            if open_span:
                out.append("</span>")
                open_span = False
            fg, bg, bold = cur
            style = "".join([f"color:{fg};" if fg else "",
                             f"background:{bg};" if bg else "",
                             "font-weight:600;" if bold else ""])
            if style:
                out.append(f'<span style="{style}">')
                open_span = True
            continue
        out.append(_html.escape(_OTHER_ESC.sub("", chunk)))
    if open_span:
        out.append("</span>")
    return "".join(out)


def tmux_sessions():
    """Every tmux session, not just configured folders — codex and friends
    live outside config.json."""
    _, out = tmux("list-sessions", "-F", "#{session_name}\t#{pane_current_command}")
    rows = []
    for line in out.splitlines():
        name, _, cmd = line.partition("\t")
        if name:
            rows.append({"name": name, "cmd": cmd})
    return rows


def pane_html(name, lines):
    """Coloured HTML for the last <lines> rows of a pane's scrollback."""
    rc, txt = tmux("capture-pane", "-pe", "-S", f"-{lines}", "-t", f"{name}:")
    if rc != 0:
        return "<em>no such tmux session</em>"
    return ansi_to_html(txt)


# ------------------------------------------------------------ codex sessions
# Codex renders badly under tmux, so scraping its pane is the wrong source.
# It already writes the whole session to ~/.codex/sessions/<Y>/<M>/<D>/
# rollout-*.jsonl — structured, complete, and independent of any terminal.
# Reading that gives the entire conversation rather than a scrollback window.

CODEX_DIR = Path.home() / ".codex" / "sessions"


def codex_files(limit=40):
    """Newest rollout files first, labelled with the cwd from their header."""
    try:
        files = sorted(CODEX_DIR.rglob("rollout-*.jsonl"),
                       key=lambda f: f.stat().st_mtime, reverse=True)[:limit]
    except OSError:
        return []
    out = []
    for f in files:
        cwd = ""
        try:
            with open(f, "r", errors="replace") as fh:
                for _ in range(5):          # session_meta is at the top
                    line = fh.readline()
                    if not line:
                        break
                    o = _jobj(line)
                    if o and o.get("type") == "session_meta":
                        pl = o.get("payload")
                        pl = pl if isinstance(pl, dict) else {}
                        ses = pl.get("session")
                        cwd = pl.get("cwd") or (ses.get("cwd") if isinstance(ses, dict) else "")
                        cwd = cwd if isinstance(cwd, str) else ""
                        break
        except OSError:
            pass
        st = f.stat()
        out.append({"file": f.name, "cwd": cwd, "mb": round(st.st_size / 1e6, 1),
                    "mt": st.st_mtime,
                    "when": time.strftime("%m-%d %H:%M", time.localtime(st.st_mtime))})
    return out


def _codex_path(name):
    """Resolve a transcript reference to a real path, refusing anything
    outside the two session trees (the name arrives from a query string)."""
    if name.startswith("claude:"):
        return claude_path(name[len("claude:"):])
    if "/" in name or ".." in name or not name.startswith("rollout-"):
        return None
    for f in CODEX_DIR.rglob(name):
        return f
    return None


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def _summary(role, body):
    """One line that says enough to decide whether to open it."""
    first = next((l.strip() for l in body.splitlines() if l.strip()), "")
    if len(first) > 110:
        first = first[:110] + "…"
    n = len(body)
    size = f"{n/1000:.1f}k" if n >= 1000 else str(n)
    lines = body.count("\n") + 1
    return first, f"{lines} lines · {size}"


def codex_target(cwd):
    """The tmux session that is the live end of a rollout, or "".

    A rollout is a file: it has no session attached, which is why this viewer
    began read-only. The link back is the working directory — the file's
    session_meta records a cwd, and a pane running codex in that directory is
    that session. Empty when the run has ended, in which case the transcript is
    history and there is nothing to type into."""
    if not cwd:
        return ""
    for sess in tmux_sessions():
        if "codex" not in (sess.get("cmd") or ""):
            continue
        _, path = tmux("display-message", "-p", "-t", f"{sess['name']}:",
                       "#{pane_current_path}")
        if path.strip() == cwd:
            return sess["name"]
    return ""


# Codex injects these as user messages. They are context for the model, not
# anything a person said, and they are long.





def term_relay(h):
    """Proxy this request to ttyd, websocket and all. True if it was handled.

    Returns False when ttyd is not answering, so the caller can explain
    itself instead of leaving a dead socket."""
    import socket, threading
    try:
        up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        up.settimeout(5)
        up.connect(str(TTYD_SOCK))
        # Whatever answers must be ours: the relayed request carries the
        # login and, from here on, keystrokes.
        pid, uid, gid = struct.unpack("3i", up.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        if uid != os.getuid():
            up.close()
            return False
    except OSError:
        return False
    try:
        head = ["%s %s HTTP/1.1" % (h.command, h.path[len("/term"):] or "/"),
                # ttyd -O compares Origin with Host, and through a unix:
                # serve target Host is "localhost" -- give it the real one.
                "Host: %s" % req_host(h.headers)]
        for k, v in h.headers.items():
            if k.lower() in ("connection", "keep-alive", "proxy-connection",
                             "host"):
                continue                      # hop-by-hop, or set above
            head.append("%s: %s" % (k, v))
        # Never keep-alive, unless this is the websocket handshake. Relaying
        # hands this socket to ttyd for good: from here on the bytes are
        # pumped, not parsed. Tell the browser it may send another request
        # on it and the next one goes to ttyd -- which answers /api/anything
        # with its own 404. That is not a terminal bug; it is every other
        # part of the page silently talking to the wrong server, and it was
        # found by watching a page fetch its own API and get
        # "server: ttyd/1.7.7" back.
        #
        # An Upgrade must keep its Connection header or there is no
        # websocket; that connection is dedicated by definition.
        upgrade = "upgrade" in (h.headers.get("Connection", "").lower())
        head.append("Connection: %s"
                    % (h.headers.get("Connection") if upgrade else "close"))
        up.sendall(("\r\n".join(head) + "\r\n\r\n").encode("latin-1"))
        n = int(h.headers.get("Content-Length") or 0)
        while n > 0:                          # a body, if there is one
            chunk = h.rfile.read(min(n, 65536))
            if not chunk:
                break
            up.sendall(chunk)
            n -= len(chunk)

        # ttyd's own response carries no framing or caching policy, and its
        # page is a live terminal. Read its head and add ours before the
        # bytes are handed over.
        resp = b""
        while b"\r\n\r\n" not in resp and len(resp) < 65536:
            b = up.recv(65536)
            if not b:
                break
            resp += b
        if b"\r\n\r\n" in resp:
            first, rest = resp.split(b"\r\n", 1)
            extra = (b"X-Frame-Options: SAMEORIGIN\r\n"
                     b"Content-Security-Policy: frame-ancestors 'self'\r\n"
                     b"X-Content-Type-Options: nosniff\r\n"
                     b"Referrer-Policy: no-referrer\r\n")
            if not first.split(b" ")[1:2] == [b"101"]:
                extra += b"Cache-Control: no-store\r\n"
            resp = first + b"\r\n" + extra + rest
        h.connection.sendall(resp)

        down = h.connection
        h.close_connection = True             # this socket is ours now
        # The 5s was for connecting. Left in place it becomes an IDLE
        # timeout: a terminal sitting quietly for five seconds looked like a
        # dead upstream, the relay tore the connection down, and the page
        # said "disconnected" over a blank screen. A relayed connection has
        # no idle limit — that is the whole point of a terminal.
        up.settimeout(None)
        down.settimeout(None)
        def pump(src, dst):
            try:
                while True:
                    b = src.recv(65536)
                    if not b:
                        break
                    dst.sendall(b)
            except OSError:
                pass
            finally:
                for sock in (src, dst):
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
        t = threading.Thread(target=pump, args=(down, up), daemon=True)
        t.start()
        pump(up, down)
        t.join(timeout=1)
    finally:
        try:
            up.close()
        except OSError:
            pass
    return True

# ------------------------------------------------------------- who is this
# tailscaled's serve proxy injects the caller's identity and overwrites any
# copy the client sent -- verified here, not assumed: a request carrying
# "Tailscale-User-Login: mallory@example.com" arrives with the real login.
# That makes the header safe to authenticate on, but only because nothing
# else can reach roost: it listens on a private Unix socket, so a request
# either came through serve or came from this account (which already has the
# machine). A TCP port -- even on 127.0.0.1 -- would let any local process,
# or any Windows process under WSL2, set its own header.
#
# allow_logins in config.json lists who may in. Empty refuses to start
# unless "allow_anyone" says otherwise. There is deliberately no exemption for a missing header: a
# tagged device gets no identity from serve, and should be refused rather
# than mistaken for a local process.

ALLOW_LOGINS = [x.strip().lower() for x in CFG.get("allow_logins", []) if x.strip()]
if not ALLOW_LOGINS:
    # Open to every tailnet peer means a shell for every tailnet peer. That
    # has to be asked for in so many words, not reached by leaving a list
    # empty.
    if not CFG.get("allow_anyone"):
        raise SystemExit("config.json: allow_logins is empty. Put your tailnet "
                         "login in it (or set \"allow_anyone\": true to serve "
                         "every tailnet peer a shell).")

def caller_login(headers):
    # Exactly one. serve overwrites a client's copy today; if that ever
    # changed to appending, taking the first would take the forged one.
    vals = headers.get_all("Tailscale-User-Login") or []
    return vals[0].strip().lower() if len(vals) == 1 else ""

def allowed(headers):
    return (not ALLOW_LOGINS) or caller_login(headers) in ALLOW_LOGINS

def req_host(headers):
    """The host the browser addressed. Through a unix: serve target, Host is
    always "localhost"; serve puts the real one in X-Forwarded-Host, replacing
    any copy the client sent. Only serve and this account can reach the
    socket, so the header is as trustworthy as the login next to it."""
    fwd = headers.get_all("X-Forwarded-Host") or []
    if len(fwd) == 1:
        return fwd[0].strip()
    if fwd:
        return ""                         # ambiguous: matches nothing
    return (headers.get("Host") or "").strip()

def same_site(headers):
    """False when another site caused the browser to make this request.

    Identity alone does not settle authorisation here. tailscaled stamps the
    caller's login on every request that reaches serve, including one that
    some page on the internet told the browser to send — so a site you merely
    visit could drive this dashboard as you: restart a session, or type into
    one through /api/type. The login header says who; it cannot say who
    asked.

    Browsers send Sec-Fetch-Site on every request and Origin on any
    cross-origin POST, and neither can be set by page script. Absent both, it
    is not a browser — curl, or something local — and those already have the
    machine."""
    site = (headers.get("Sec-Fetch-Site") or "").lower()
    # "same-site" is not enough: every *.ts.net machine of the tailnet, and
    # every other port on this one, is same-site.
    if site and site not in ("same-origin", "none"):
        return False
    origin = headers.get("Origin") or ""
    if origin:
        from urllib.parse import urlparse
        if urlparse(origin).netloc != req_host(headers):
            return False
    elif (headers.get("Upgrade") or "").lower() == "websocket":
        # A websocket is not covered by CORS, and a handshake carries no
        # Sec-Fetch-Site in every browser; what every browser does send is
        # Origin. One without it is not a browser, and gets no terminal.
        return False
    return True

# Host names a request may be addressed to. Without this, DNS rebinding makes
# a page on some other site same-origin with 127.0.0.1:<port> -- Origin and
# Host then agree on the attacker's name, and a browser on this machine will
# happily add a Tailscale-User-Login header of the page's choosing, since
# nothing but serve stands between it and the loopback port. A page cannot
# make the browser send a Host it does not own, so naming the good ones is
# enough: loopback (for curl and the scripts here), the tailnet's *.ts.net
# names serve forwards, and anything listed under "hosts" in config.json.
ALLOW_HOSTS = {h.strip().lower() for h in CFG.get("hosts", []) if h.strip()}

def good_host(headers):
    host = req_host(headers).lower()
    m = _re.fullmatch(r"(\[::1\]|[a-z0-9.-]+)(?::\d{1,5})?", host)
    if not m:
        return False
    name = m.group(1)
    return (name in ("127.0.0.1", "localhost", "[::1]")
            or name.endswith(".ts.net") or name in ALLOW_HOSTS)

DENIED = """<!doctype html><meta charset="utf-8">
<title>not for this account</title>
<style>body{margin:0;padding:2rem;background:#151515;color:#f0efec;
font:15px/1.6 system-ui,"Segoe UI",Roboto,sans-serif}code{background:
rgba(255,255,255,.08);border-radius:4px;padding:.1rem .35rem}</style>
<p><strong>This dashboard is limited to one Tailscale account.</strong></p>
<p>You are signed in as <code>__WHO__</code>.</p>
<p style="color:#898781">The check is on the Tailscale login of the device
you are connecting from, so it follows the account rather than the machine.
Sign in to Tailscale as the owning account and reload.</p>
"""

# ------------------------------------------------------------ github links
# A path that resolves locally often also exists on GitHub, and the useful
# link is to the exact revision the document is talking about. That is
# derivable rather than guessable: the repo containing the file knows its
# own remote and its own HEAD.
#
# The one thing that cannot be derived is whether that commit was ever
# pushed. An unpushed sha produces a link that looks authoritative and 404s,
# so a commit that is not on a remote branch gets no GitHub link at all.

_GIT_CACHE = {}

# A repository's own .git/config is not ours to trust: it can name commands
# that git runs during ordinary read-only work -- an fsmonitor, a clean
# filter during `git status`, a gpg program during `git log` on a signed
# commit. roost runs git in whatever folder you hover, so a checkout that
# arrived with a hostile .git (an unpacked tarball, say) would run code on a
# mouse-over. Every such key with a fixed name is pinned on the command line,
# which beats repository config, include.path and includeIf alike. Filters
# have names the repository chooses, so those are listed first (reading
# config runs nothing) and each one is blanked -- git treats an empty filter
# as none.
_GIT_PINNED = [
    "core.fsmonitor=false", "core.hooksPath=/dev/null", "core.sshCommand=",
    "core.askPass=", "core.editor=false", "core.pager=cat",
    "core.alternateRefsCommand=", "core.attributesFile=/dev/null",
    "log.showSignature=false", "gpg.program=false", "gpg.ssh.program=false",
    "gpg.x509.program=false", "diff.external=", "credential.helper=",
    "sequence.editor=false", "protocol.allow=never",
    "uploadpack.packObjectsHook=", "status.submoduleSummary=false",
    "mailmap.file=/dev/null", "mailmap.blob=",
]
_GIT_ENV = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
                GIT_ALLOW_PROTOCOL="", GIT_ASKPASS="", SSH_ASKPASS="",
                GIT_EXTERNAL_DIFF="", GIT_PAGER="cat", GIT_ATTR_NOSYSTEM="1",
                GIT_OPTIONAL_LOCKS="0")
_SAFE_CACHE = {}

def _run_git(argv, cwd, timeout):
    """(rc, stdout). git in its own process group, and the whole group
    killed on timeout: a helper git spawned would otherwise outlive it."""
    import signal as _sig
    try:
        p = subprocess.Popen(argv, cwd=str(cwd), env=_GIT_ENV,
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True,
                             start_new_session=True)
    except OSError:
        return 1, ""
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out.strip()
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, _sig.SIGKILL)
        except OSError:
            pass
        p.communicate()
        return 124, ""

def _git_safe_args(cwd):
    """The -c list for one repository, or None if its config cannot be read
    promptly. Cached: a hover runs a dozen git commands, and a config that
    hangs (an include pointing at a FIFO) must cost one timeout, not twelve."""
    key = str(cwd)
    now = time.time()
    hit = _SAFE_CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    args = []
    for kv in _GIT_PINNED:
        args += ["-c", kv]
    rc, out = _run_git(["git", "-c", "core.fsmonitor=false", "config",
                        "--name-only", "--get-regexp",
                        r"^filter\..*\.(clean|smudge|process)$"], cwd, 2)
    if rc not in (0, 1):                 # 1: no filters defined
        args = None
    else:
        for k in out.split():
            args += ["-c", k + "="]
    if len(_SAFE_CACHE) > 2000:
        _SAFE_CACHE.clear()
    _SAFE_CACHE[key] = (now + 30.0, args)
    return args

def _git(args, cwd, timeout=4):
    safe = _git_safe_args(cwd)
    if safe is None:
        return ""
    rc, out = _run_git(["git"] + safe + args, cwd, timeout)
    return out if rc == 0 else ""

def _gh_slug(remote):
    """github.com owner/repo out of either remote form, or ""."""
    r = remote.strip()
    if r.startswith("git@github.com:"):
        r = r[len("git@github.com:"):]
    elif r.startswith(("https://github.com/", "http://github.com/",
                       "ssh://git@github.com/")):
        r = r.split("github.com/", 1)[1]
    else:
        return ""
    if r.endswith(".git"):
        r = r[:-4]
    return r.strip("/") if r.count("/") == 1 else ""

def _pushed_sha(d, head):
    """A commit GitHub actually has: HEAD if it is on a remote branch, else
    the merge base with the remote's default branch — the newest ancestor
    that was pushed. Returns (sha, exact)."""
    if _git(["branch", "-r", "--contains", head], d, timeout=8):
        return head, True
    ref = _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], d)
    cands = [ref] if ref else []
    cands += ["origin/main", "origin/master"]
    for c in cands:
        if not c:
            continue
        mb = _git(["merge-base", "HEAD", c], d)
        if mb:
            return mb, False
    return "", False

def git_repo(path):
    """(slug, sha, root, exact) for the repo holding `path`, when it is on
    GitHub. `exact` is False when the local HEAD is unpushed and the link
    points at the last commit the remote has. Cached per directory."""
    d = str(path if path.is_dir() else path.parent)
    if d in _GIT_CACHE:
        return _GIT_CACHE[d]
    out = (None, None, None, False)
    root = _git(["rev-parse", "--show-toplevel"], d)
    if root:
        slug = _gh_slug(_git(["remote", "get-url", "origin"], d))
        head = _git(["rev-parse", "HEAD"], d)
        if slug and head:
            sha, exact = _pushed_sha(d, head)
            if sha:
                out = (slug, sha, Path(root), exact)
    if len(_GIT_CACHE) > 500:
        _GIT_CACHE.clear()
    _GIT_CACHE[d] = out
    return out

def github_url(abspath):
    """(url, exact) for a local file on GitHub, or ("", False)."""
    f = Path(abspath)
    slug, sha, root, exact = git_repo(f)
    if not slug:
        return "", False
    try:
        rel = f.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return "", False
    kind = "tree" if f.is_dir() else "blob"
    tail = "" if rel.as_posix() == "." else "/" + rel.as_posix()
    return "https://github.com/%s/%s/%s%s" % (slug, kind, sha, tail), exact

# ------------------------------------------------------------- local files
# Transcripts are full of paths — receipts, JSON evidence, generated docs —
# and on a phone they are unreachable text. These serve them read-only.
#
# Everything is resolved and then checked to be under $HOME, after symlinks,
# so a path in a transcript cannot walk out of it. Nothing is served as HTML:
# an HTML file from this origin could script the dashboard, so anything that
# is not an image or a PDF goes out as text/plain with nosniff.

FILE_MAX = 25 * 1024 * 1024          # full read; bigger files are previewed

def _size_of(n):
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return "%.1f %s" % (n / div, unit)
    return "%d B" % n
FILE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".pdf": "application/pdf",
}

# Everything served or browsed lives under these roots. Not $HOME: that also
# holds ~/.ssh, ~/.claude and the shell history, and none of that is what a
# transcript link is for. /tmp is here because the agents write their working
# documents there -- a handoff a transcript points at is usually in /tmp.
#
# /tmp is shared ground, though: it has the sticky bit and root's systemd
# private directories sit in it. So under a sticky root a file must also be
# owned by the user running this, which excludes those without a blocklist to
# maintain.
# Secrets are not documents. The viewer refuses these wherever they live, so
# that widening the roots -- home is one of them now -- cannot turn a file
# browser into a way to read a private key over HTTPS. Refused, not hidden
# from listings only: the check is in the containment function every route
# goes through.
_DENY_DIRS = {".ssh", ".gnupg", ".aws", ".config", ".kube", ".docker",
              ".password-store", ".cache", ".npm", ".nv", ".azure",
              ".terraform.d", "keyrings", "pki", ".mozilla", "session-env",
              "shell-snapshots", "shell_snapshots", ".gemini",
              ".grok", ".cursor", ".copilot", ".m2", ".gradle", ".cargo",
              ".vscode-server", ".local"}
_DENY_NAMES = {".credentials.json", ".netrc", ".npmrc", ".git-credentials",
               ".claude.json", ".pypirc", ".dockercfg", ".env", ".pgpass",
               ".vault-token", "credentials", "credentials.toml",
               ".gitconfig", ".envrc", ".boto", ".s3cfg", ".zhistory",
               ".histfile", ".lesshst", ".viminfo", "terraform.tfstate",
               "terraform.tfstate.backup", ".my.cnf",
               ".htpasswd", "kubeconfig"}
_DENY_SUFFIX = (".pem", ".key", ".p12", ".pfx", ".keystore", ".ppk",
                ".tfvars", ".kdbx", ".jks", ".p8", ".age", ".gpg", ".asc")
_DENY_STEMS = ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")

def denied(path):
    """True for anything that is a credential rather than a document."""
    try:
        # A second name for a file says nothing about what the file is: a
        # hardlink in /tmp to ~/.ssh/id_rsa passes every name test below.
        if path.is_file() and path.stat().st_nlink > 1:
            return True
    except OSError:
        return True
    name = path.name
    if name in _DENY_NAMES or name.endswith(_DENY_SUFFIX) \
       or name.startswith(_DENY_STEMS) or name.endswith("_history") \
       or name.startswith((".env.", ".claude.json")):
        return True
    # codex keeps its login next to its transcripts, so the directory itself
    # stays readable and only the token file is refused.
    if name in ("auth.json", "config.toml") and ".codex" in path.parts:
        return True
    return any(part in _DENY_DIRS for part in path.parts)

def _session_dirs():
    """Every session's own folder, labelled by session.

    A path is easiest to read against the session it belongs to, so each
    session's folder is a place the viewer can start from -- and, because
    the innermost root wins below, the tree stops there rather than at ~/src.
    Terminal-only sessions are not in config.json, so their folder comes from
    the "cd" in the definition that starts them."""
    out = {}
    for name, d in FOLDERS.items():
        try:
            p = Path(d).expanduser().resolve()
            if p.is_dir():
                # Configured sessions are named without the machine postfix
                # the cards carry; a terminal definition's stem already has it.
                out.setdefault(str(p), name + SUFFIX)
        except (OSError, ValueError, RuntimeError):
            pass
    try:
        for f in sorted((Path.home() / ".dtach").glob("*.cmd")):
            # The leading cd only, and only somewhere under home or src_dir:
            # "cd /" would otherwise make the whole filesystem a root --
            # /proc/<pid>/environ included.
            m = _re.match(r"(?:\s*export\s+\S+;)*\s*cd\s+(\S+)", _read_small(f))
            if not m:
                continue
            try:
                p = Path(m.group(1)).expanduser().resolve()
            except (OSError, ValueError, RuntimeError):
                continue
            home = Path.home().resolve()
            if p in (home, Path("/")) or not any(
                    p.is_relative_to(b) for b in (home, SRC.resolve())):
                continue
            if p.is_dir():
                out.setdefault(str(p), f.stem)   # already the card's name
    except OSError:
        pass
    return out

ROOT_LABEL = {}

def _roots():
    want = CFG.get("file_roots") or [str(SRC), "/tmp"]
    # Home last, so it is the fallback rather than the default: everything
    # under it that matters has a nearer root above. And roost's own folder
    # for what you paste in -- a picture you cannot open again is not much
    # of a paste.
    want = list(want) + [str(Path.home() / ".roost" / "pasted"), str(Path.home())]
    sessions = _session_dirs()
    out, seen = [], set()
    # Sessions first in the picker: they are what a path is usually about.
    for r in list(sessions) + want:
        try:
            d = Path(r).expanduser().resolve()
            if not d.is_dir() or str(d) in seen:
                continue
            seen.add(str(d))
            out.append((d, bool(d.stat().st_mode & 0o1000)))   # sticky?
            if r in sessions:
                ROOT_LABEL[str(d)] = "@" + sessions[r]
        except OSError:
            pass
    return out

FILE_ROOTS = _roots()
FILE_ROOT = FILE_ROOTS[0][0] if FILE_ROOTS else Path.home()
_UID = os.getuid()

def root_of(path):
    """(root, owner_only) containing `path`, or (None, False).

    The innermost root wins, not the first one listed: a file inside a
    session's folder belongs to that session, and reading its path against
    ~/src instead would be reading it against the wrong thing."""
    best, sticky_of = None, False
    for d, sticky in FILE_ROOTS:
        if path == d or d in path.parents:
            if best is None or len(str(d)) > len(str(best)):
                best, sticky_of = d, sticky
    return best, sticky_of

def _under_root(path):
    """Resolve through symlinks, confirm it is under a root, and on shared
    ground confirm it is ours."""
    try:
        f = Path(path).expanduser()
        if not f.is_absolute():
            return None
        f = f.resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    root, owner_only = root_of(f)
    if root is None or denied(f):
        return None
    # The root itself is exempt from the owner test: /tmp belongs to root and
    # always will, so requiring ours would mean the one folder you can never
    # open is the root you were offered. What is IN it is still checked.
    if owner_only and f != root:
        try:
            if f.stat().st_uid != _UID:
                return None
        except OSError:
            return None
    return f

def safe_file(path):
    """A readable file under the root, or None."""
    f = _under_root(path)
    try:
        return f if (f is not None and f.is_file()) else None
    except OSError:
        return None

def safe_dir(path):
    """A readable directory under the root, or None."""
    f = _under_root(path)
    try:
        return f if (f is not None and f.is_dir()) else None
    except OSError:
        return None

def file_ref(target, base):
    """A transcript path -> the URL that serves it, or "" if it is not one.

    `base` is the session's cwd, which is what the relative paths in a
    transcript are relative to."""
    t = (target or "").strip()
    if not t or t.startswith(("http://", "https://", "#", "mailto:")):
        return ""
    t = t.split("#", 1)[0]
    cand = Path(t).expanduser()
    if not cand.is_absolute():
        if not base:
            return ""
        cand = Path(base) / t
    f = safe_file(cand) or safe_dir(cand)
    return ("file?path=" + _url_q(str(f))) if f else ""

def ref_and_path(target, base):
    """(url, resolved path) for something a transcript names, or (None, None)."""
    t = (target or "").strip()
    if not t or t.startswith(("http://", "https://", "#", "mailto:")):
        return None, None
    t = t.split("#", 1)[0]
    cand = Path(t).expanduser()
    if not cand.is_absolute():
        if not base:
            return None, None
        cand = Path(base) / t
    f = safe_file(cand) or safe_dir(cand)
    if f is None and base and "/" in t and not Path(t).expanduser().is_absolute():
        # A document often names a path relative to the checkout it describes
        # rather than to itself: CURRENT-STATE.md says
        # spec/reviews/handoff/09-…md, which lives under its work/app2. Look a
        # couple of levels down for it. Bounded by directory count, not file
        # count, and the answer is cached.
        f = _find_below(Path(base), t)
    return ("file?path=" + _url_q(str(f)), str(f)) if f else (None, None)

# The descent is the expensive part of resolving a path, and it runs for
# every token that does not resolve directly. On a big transcript that is
# thousands of tokens, each walking the tree again: the notes history took
# 46 seconds to render and the conversation view timed out entirely.
#
# Two things fix it. The directory list per base is computed once instead of
# per token, and a base that keeps missing stops being descended into at all
# -- if forty tokens in a row were not found below it, the forty-first will
# not be either, and a transcript full of prose reads like paths.
_BELOW_DIRS = {}
_BELOW_MISS = {}
_BELOW_GIVE_UP = 40

def _below_dirs(base, levels=2, fanout=60):
    key = str(base)
    if key in _BELOW_DIRS:
        return _BELOW_DIRS[key]
    dirs, frontier = [], [base]
    for _ in range(levels):
        nxt = []
        for d in frontier:
            try:
                kids = [x for x in d.iterdir() if x.is_dir()][:fanout]
            except OSError:
                continue
            dirs.extend(kids)
            nxt.extend(kids)
        frontier = nxt[:fanout]
    if len(_BELOW_DIRS) > 200:
        _BELOW_DIRS.clear()
    _BELOW_DIRS[key] = dirs[:fanout * 2]
    return _BELOW_DIRS[key]

def _find_below(base, rel):
    """The first place `rel` exists below `base`, or None."""
    key = str(base)
    if _BELOW_MISS.get(key, 0) >= _BELOW_GIVE_UP:
        return None
    for k in _below_dirs(base):
        hit = safe_file(k / rel) or safe_dir(k / rel)
        if hit is not None:
            _BELOW_MISS[key] = 0
            return hit
    _BELOW_MISS[key] = _BELOW_MISS.get(key, 0) + 1
    return None

def _url_q(v):
    from urllib.parse import quote
    return quote(v, safe="")

# A framed page fills the pane: it is a whole document, not a block inside
# one, so the reading measure does not apply to it.
_HTML_VIEW_CSS = """
  main { padding: 0; }
  main > iframe.page { display: block; width: 100%; max-width: none;
                       margin: 0; border: 0; height: 100%;
                       min-height: calc(100vh - 3.2rem); background: #fff; }
"""

_MD_VIEW_CSS = """
  /* A page from a paper, not a control panel.
   *
   * Three things were making these documents hard to read: the line ran the
   * whole width of the window, which at 1600px is twice what the eye tracks
   * comfortably; the UI's sans at UI sizes gave every heading and every
   * **bold** run the loudness of a button; and the spacing was set for a
   * dense list, not for prose.
   *
   * So: one column of about 70 characters, centred; Computer Modern for the
   * text, which is the font LaTeX sets papers in and -- because KaTeX ships
   * it and roost now carries KaTeX -- is already on disk here, so the prose
   * and the maths in it are finally the same typeface; and the vertical
   * rhythm opened up. Code, tables and formulas keep their own width,
   * because wrapping those to a reading measure helps nobody.
   */
  main { font: .95rem/1.68 var(--c-read); color: var(--c-read-ink);
         font-weight: 350; letter-spacing: .002em;
         -webkit-font-smoothing: antialiased;
         -moz-osx-font-smoothing: grayscale;
         padding-top: 1.3rem; }
  main > * { max-width: var(--c-measure); margin-inline: auto; }
  /* A wider column: still a measure, but one that suits a document of
     tables and paths rather than of sentences. */
  body.mid main > * { max-width: var(--c-measure-wide); }
  /* Full width: the same typography, no measure at all. For a table that
     does not fit, or a screen you would rather fill than centre. */
  body.wide main > * { max-width: none; margin-inline: 0; }
  /* One ceiling, and the toolbar sets it. Code, tables and formulas used to
     carry a fixed 62rem of their own, so "reader" gave a 37rem column of
     prose beside a 62rem table -- wider, in fact, than the middle setting
     the toolbar offered. Every block obeys the chosen width now, and
     anything that still cannot fit scrolls inside its own box. */
  main > h1, main > h2, main > h3, main > h4 {
      font-weight: 600; line-height: 1.28; margin: 1.6rem auto .45rem;
      color: #f0eeea; }
  main > h1 { font-size: 1.32rem; margin-top: .3rem; }
  main > h2 { font-size: 1.1rem; }
  main > h3 { font-size: 1rem; }
  main > h4 { font-size: 1rem; color: var(--c-muted); }
  main p, main ul, main ol, main blockquote { margin: .7rem auto; }
  main ul, main ol { padding-left: 1.4rem; }
  main li { margin: .3rem 0; }
  main li > ul, main li > ol { margin: .3rem 0; }
  /* One step up from the text, not three. At 700 on a dark screen every
     emphasised phrase became the loudest thing on the page. */
  main strong, main b { font-weight: 600; color: #f0eeea; }
  main em, main i { font-style: italic; }
  main code { background: rgba(255,255,255,.06); border-radius: 3px;
              padding: .04rem .28rem; font: .84em/1.4 var(--c-mono);
              color: #d8d5cf; }
  main pre.code { background: var(--c-deep); border: 1px solid var(--c-line);
                  border-radius: 8px; padding: .6rem .7rem; overflow-x: auto;
                  white-space: pre; font: 12.5px/1.55 var(--c-mono); }
  main pre.code code { background: none; padding: 0; font-size: 1em; }
  main blockquote { border-left: 2px solid var(--c-line-strong);
                    padding-left: .9rem; color: var(--c-muted);
                    font-style: italic; }
  main hr { border: 0; border-top: 1px solid var(--c-line); margin: 1.6rem auto; }
  main table { font-size: .9rem; }
  .math, .imath { font: 13px/1.5 var(--c-mono); }
  /* Only as wide as the formula. A full-width band for a six-character
     inequality reads as a section break rather than as one line of maths;
     fit-content sizes the box to what is in it, and the cap plus the scroll
     handle a formula wider than the column. */
  .math { display: block; width: fit-content; max-width: 100%;
          white-space: pre-wrap; overflow-x: auto;
          background: var(--c-deep); border: 1px solid var(--c-line);
          border-radius: 8px; margin: .55rem 0; padding: .5rem .6rem; }
  /* KaTeX centres display maths. In a document of left-aligned prose that
     leaves each formula stranded in the middle of a wide line, disconnected
     from the sentence that introduces it, so it is set flush left like
     everything else. The outer .math box keeps the horizontal scroll for a
     formula wider than the column. */
  .math .katex-display,
  .math .katex-display > .katex { text-align: left; }
  .math .katex-display { margin: 0; }
  /* Source shown because the typesetter never arrived, not because the
     document meant it that way. */
  .noks { border-color: var(--c-clay) !important; opacity: .85; }
  /* A long URL wraps rather than running out of the column. anywhere, not
     break-word: a URL has no spaces to break at, so break-word never fires
     and the line simply overflowed the measure. */
  main, main p, main li, main td, main th { overflow-wrap: anywhere; }
  main a .u { color: var(--c-muted); font-size: .84em; overflow-wrap: anywhere; }
  /* A link whose address is on the hover rather than on the page says so
     with a dotted underline; the button beside it copies the address. */
  a.named { text-decoration: none; border-bottom: 1px dotted currentColor; }
  .linkhost { opacity: .6; font-size: .85em; }

  /* Copy the link, because selecting a URL that wraps over three lines with a
     thumb is not a thing anybody manages. Click copies the address; hold
     shift and it copies the path or URL as written instead. */
  .cp { font: inherit; font-size: .8em; line-height: 1; cursor: pointer;
        vertical-align: baseline; margin-left: .25em; padding: .1em .3em;
        color: var(--c-muted); background: none;
        border: 1px solid var(--c-line); border-radius: 5px;
        -webkit-tap-highlight-color: transparent; }
  .cp:hover { color: var(--c-text); border-color: var(--c-line-strong); }
  .cp.done { color: #89d185; border-color: #89d185; }

  main a.pathref .u { display: block; }
  main a.gh { display: block; color: var(--c-accent); font-size: .84em;
             word-break: break-all; opacity: .85; }
"""


def _crumbs(d):
    """Root-relative breadcrumbs. There is no link above the root because
    there is nothing above it to see."""
    root = root_of(d)[0] or FILE_ROOT
    parts, cur = [], d
    while True:
        parts.append(cur)
        if cur == root:
            break
        cur = cur.parent
        if cur == cur.parent:              # walked out; should not happen
            break
    out = []
    for x in reversed(parts):
        name = str(x) if x == root else x.name
        out.append('<a href="file?path=%s">%s</a>' % (_url_q(str(x)),
                                                      _html.escape(name)))
    return '<span class="sep">/</span>'.join(out)


def _root_name(d):
    """What the picker calls a root: the session it belongs to, if any."""
    lab = ROOT_LABEL.get(str(d))
    short = str(d).replace(str(Path.home()), "~")
    return ("%s · %s" % (lab, short)) if lab else short

def _roots_options(sel):
    """The location picker: the roots this server will serve from."""
    return "".join(
        '<option value="%s"%s>%s</option>'
        % (_html.escape(str(r), quote=True),
           " selected" if str(r) == str(sel) else "",
           _html.escape(_root_name(r)))
        for r, _ in FILE_ROOTS)

def _page_common(page, here, heredir):
    return (page.replace("__ROOTS__", _roots_options(root_of(Path(heredir))[0]))
                .replace("__HOMEDIR__", _js(str(Path.home())))
                .replace("__HEREDIR__", _js(str(heredir)))
                .replace("__HERE__", _js(str(here))))

# When a folder is sorted by time, the time that matters is the newest thing
# inside it -- a folder whose name has not changed since it was created is
# still where today's work is. That is a recursive walk, so it is bounded and
# cached: a budget shared across one request, and a short-lived answer per
# folder. Over budget the walk stops and reports the newest it has seen,
# which is an approximation of the sort order and never of the content.
_MT_CACHE = {}
_MT_TTL = 60.0
_MT_REQ_BUDGET = 20000        # scandir entries one request may look at

def newest_mtime(d, budget=None):
    """The newest modification time anywhere under `d`, itself included."""
    key = str(d)
    now = time.time()
    hit = _MT_CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    if budget is None:
        budget = [_MT_REQ_BUDGET]
    try:
        best = d.stat().st_mtime
    except OSError:
        return 0.0
    stack = [key]
    while stack and budget[0] > 0:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    budget[0] -= 1
                    if budget[0] <= 0:
                        break
                    try:
                        # Never follow a symlink: a link back up the tree is
                        # an endless walk, and a link's own time is its own.
                        st = e.stat(follow_symlinks=False)
                        if st.st_mtime > best:
                            best = st.st_mtime
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                    except OSError:
                        continue
        except OSError:
            continue
    if len(_MT_CACHE) > 4000:
        _MT_CACHE.clear()
    _MT_CACHE[key] = (now + _MT_TTL, best)
    return best

def entry_times(entries):
    """(name -> mtime) for a listing, folders reported recursively.

    The budget is shared out rather than spent in order: one enormous folder
    at the top of the list would otherwise consume all of it and leave every
    folder below it reporting only its own timestamp."""
    dirs = 0
    for x in entries:
        try:
            dirs += 1 if x.is_dir() else 0
        except OSError:
            pass
    per, left = max(400, _MT_REQ_BUDGET // max(1, dirs)), _MT_REQ_BUDGET
    out = {}
    # Spend the budget on the folders that changed most recently first. A
    # folder's own timestamp moves whenever a file is added or removed in it,
    # so it is a decent guess at where today's work is -- and the whole point
    # of this sort is to get the top of the list right.
    def own(x):
        try:
            return x.stat().st_mtime
        except OSError:
            return 0.0
    for x in sorted(entries, key=own, reverse=True):
        try:
            if not x.is_dir():
                out[x.name] = x.stat().st_mtime
                continue
            b = [min(per, max(0, left))]
            had = b[0]
            out[x.name] = newest_mtime(x, b)
            left -= had - b[0]              # only a cache miss costs anything
        except OSError:
            out[x.name] = 0.0
    return out

# Which repository a path belongs to, for the hover box. Cached because a
# listing asks the same question about every row, and the answer is a walk
# up the tree.
_GITDIR_CACHE = {}
_GITDIR_TTL = 120.0

def git_root(d):
    """The nearest ancestor holding a .git, or None."""
    key = str(d)
    now = time.time()
    hit = _GITDIR_CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    cur, found = d, None
    while True:
        try:
            # A worktree's .git is a FILE pointing at the real gitdir, so both
            # shapes count -- but an empty .git directory does not. There is
            # one of those sitting in /tmp, left by something, and taking it
            # at face value made every worktree under /tmp report /tmp as its
            # repository.
            marker = cur / ".git"
            if marker.is_file() or (marker.is_dir() and (marker / "HEAD").exists()):
                found = cur
                break
        except OSError:
            break
        if cur.parent == cur:
            break
        cur = cur.parent
    if len(_GITDIR_CACHE) > 4000:
        _GITDIR_CACHE.clear()
    _GITDIR_CACHE[key] = (now + _GITDIR_TTL, found)
    return found

# Which branch is out, and whether this checkout is the repository's own
# working tree or one of its linked worktrees. Read off the files rather
# than by running git: a listing asks about every row, and a subprocess per
# row is a listing that takes seconds.
_GITINFO_CACHE = {}
_GITINFO_TTL = 20.0

def _head_branch(gitdir):
    t = _read_small(gitdir / "HEAD").strip()
    if not t:
        return ""
    if t.startswith("ref:"):
        ref = t[4:].strip()
        return ref.split("refs/heads/", 1)[-1] if "refs/heads/" in ref else ref
    return (t[:12] + " (detached)") if t else ""

def git_info(root):
    """{branch, wt, main} for a checkout, or {} when it cannot be read."""
    key = str(root)
    now = time.time()
    hit = _GITINFO_CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    out = {}
    marker = root / ".git"
    try:
        if marker.is_file():
            # A linked worktree: .git is a file naming the real gitdir, which
            # lives under the main repository as .git/worktrees/<name>.
            txt = _read_small(marker).strip()
            gd = txt.split("gitdir:", 1)[-1].strip() if txt.startswith("gitdir:") else ""
            if gd:
                g = Path(gd)
                if not g.is_absolute():
                    g = (root / g).resolve()
                main = str(g).split("/.git/worktrees/")[0] \
                    if "/.git/worktrees/" in str(g) else ""
                out = {"branch": _head_branch(g), "wt": "linked", "main": main}
        elif marker.is_dir():
            out = {"branch": _head_branch(marker), "wt": "main", "main": ""}
    except OSError:
        out = {}
    if len(_GITINFO_CACHE) > 2000:
        _GITINFO_CACHE.clear()
    _GITINFO_CACHE[key] = (now + _GITINFO_TTL, out)
    return out

# What state a path is in: still only on this disk, committed here, or on
# the remote. Answered on hover for one path at a time, never for a listing:
# it costs a few subprocesses and the answer is worth waiting 100ms for.
_STATE_CACHE = {}
_STATE_TTL = 15.0

def _git_rc(args, cwd, timeout=4):
    safe = _git_safe_args(cwd)
    if safe is None:
        return 124, ""
    return _run_git(["git"] + safe + args, cwd, timeout)

def git_state(path, isdir):
    """One line about where this path's content exists: nowhere but here,
    in a local commit, or on the remote."""
    key = (str(path), isdir)
    now = time.time()
    hit = _STATE_CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    out = _git_state(path, isdir)
    if len(_STATE_CACHE) > 2000:
        _STATE_CACHE.clear()
    _STATE_CACHE[key] = (now + _STATE_TTL, out)
    return out

def _git_state(path, isdir):
    g = git_root(path if isdir else path.parent)
    if not g:
        return "not in a repository"
    rel = _rel(path, g) or "."
    rc, out = _git_rc(["status", "--porcelain", "--ignore-submodules=all",
                       "--", rel], g)
    if rc == 124:
        return "too slow to check"
    if rc:
        return "unknown"
    lines = [x for x in out.splitlines() if x.strip()]
    if lines:
        if all(x.startswith("??") for x in lines):
            return ("untracked — only on this disk" if not isdir
                    else "%d untracked — only on this disk" % len(lines))
        n = len(lines)
        return ("uncommitted changes" if n == 1 and not isdir
                else "%d files changed or untracked" % n)
    # Nothing to commit. Either it is committed, or git is ignoring it --
    # which for a folder full of preserved work is the answer that matters.
    _, sha = _git_rc(["log", "-1", "--format=%H", "--", rel], g)
    if not sha:
        rc2, _ = _git_rc(["check-ignore", "-q", "--", rel], g)
        if rc2 == 0:
            return "ignored by git — only on this disk"
        return "empty, or nothing git tracks here"
    rc3, up = _git_rc(["rev-parse", "--abbrev-ref", "--symbolic-full-name",
                    "@{upstream}"], g)
    if rc3 or not up:
        return "committed · this branch has no upstream"
    rc4, _ = _git_rc(["merge-base", "--is-ancestor", sha, up], g)
    if rc4 == 124:
        return "committed · push state unknown"
    return ("pushed · on " + up) if rc4 == 0 \
        else "committed here · not pushed"

def _rel(path, base):
    try:
        return str(path.relative_to(base))
    except ValueError:
        return ""

def where(path, isdir):
    """Everything the hover box says about one path: which session's folder
    it is in, and which repository, with the path relative to each."""
    root, _ = root_of(path)
    g = git_root(path if isdir else path.parent)
    info = git_info(g) if g else {}
    return {
        "sess": ROOT_LABEL.get(str(root), "") if root else "",
        "sroot": str(root) if root else "",
        "srel": _rel(path, root) if root else "",
        "git": str(g) if g else "",
        "grel": _rel(path, g) if g else "",
        "branch": info.get("branch", ""),
        "wt": info.get("wt", ""),
        "main": info.get("main", ""),
    }

def _mine(x):
    try:
        return x.lstat().st_uid == _UID
    except OSError:
        return False

def listable(d):
    """The entries of `d` this viewer would actually serve.

    On sticky ground -- /tmp, where everyone writes -- that means ours only:
    listing another user's folder to refuse every file in it is worse than
    not listing it."""
    owner_only = root_of(d)[1]
    return [x for x in d.iterdir()
            if not denied(x) and (not owner_only or _mine(x))]

def dir_body(d):
    """The listing itself, without the page around it, so the viewer can
    swap it in without reloading."""
    try:
        entries = sorted(listable(d),
                         key=lambda x: (not x.is_dir(), x.name.lower()))
    except OSError as e:
        entries, err = [], str(e)
    else:
        err = ""
    rows, LIMIT = [], 2000
    if root_of(d)[0] != d:
        rows.append('<tr><td class="ic">↰</td><td><a href="file?path=%s">..</a>'
                    '</td><td></td><td></td></tr>' % _url_q(str(d.parent)))
    shown = entries[:LIMIT]
    times = entry_times(shown)
    for x in shown:
        try:
            st = x.stat()
            isdir = x.is_dir()
        except OSError:
            continue                       # a broken symlink: skip it
        # A folder shows the newest thing inside it, which is what the
        # by-time order sorts on -- the two must agree or the column
        # contradicts the order it is in.
        mt = times.get(x.name, st.st_mtime)
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(mt))
        rows.append(
            '<tr data-mt="%d" data-dir="%d"><td class="ic">%s</td>'
            '<td><a href="file?path=%s">%s</a></td>'
            '<td class="num">%s</td><td class="when">%s</td></tr>'
            % (int(mt), 1 if isdir else 0,
               "▸" if isdir else "·", _url_q(str(x)),
               _html.escape(x.name) + ("/" if isdir else ""),
               "" if isdir else _size_of(st.st_size), when))
    body = ('<table><tbody>%s</tbody></table>' % "".join(rows)) if rows \
        else '<p class="note">empty</p>'
    if err:
        body = '<p class="note">%s</p>' % _html.escape(err)
    if len(entries) > LIMIT:
        body += '<p class="note">%d more not shown</p>' % (len(entries) - LIMIT)
    return body, len(entries)

def dir_page(d):
    body, n = dir_body(d)
    return (_page_common(FILE_PAGE, d, d)
            .replace("__EXTRA__", _DIR_CSS)
            .replace("__BODY__", body)
            .replace("__NAME__", _html.escape(d.name or str(d)))
            .replace("__SIZE__", "%d items" % n)
            .replace("__PATH__", _crumbs(d))
            .replace("__RAWLABEL__", "browse")
            .replace("__RAW__", "file?path=" + _url_q(str(FILE_ROOT))))

_DIR_CSS = """
  table { width: 100%; }
  td { border: 0; border-bottom: 1px solid var(--c-line); padding: .4rem .5rem;
       white-space: nowrap; }
  td:nth-child(2) { width: 100%; white-space: normal; word-break: break-all; }
  .ic { color: var(--c-muted); width: 1rem; }
  .when { color: var(--c-muted); font-size: .8rem; }
  .num { color: var(--c-muted); font-size: .8rem; }
  header .p a { color: var(--c-muted); }
  header .p .sep { color: var(--c-muted); opacity: .5; margin: 0 .15rem; }
"""

# ------------------------------------------------------------------ markdown
# A small CommonMark subset, rendered here rather than in the browser: the
# transcript is already megabytes of HTML, and shipping a parser plus the raw
# text to a phone to do the same work is the wrong end. Blocks are handled by
# a single line walk, inline spans by one pass of regexes over already-escaped
# text, so nothing here backtracks over the whole document.
#
# Deliberately not supported: reference links, HTML passthrough, nested
# blockquotes, setext headings. Transcripts do not use them, and each one
# costs a pass.

_MD_CODE = _re.compile(r"`([^`]+)`")
_MD_BOLD = _re.compile(r"\*\*(.+?)\*\*|__(.+?)__", _re.S)
_MD_ITAL = _re.compile(r"(?<![\*\w])\*([^\*\n]+)\*(?!\*)|(?<![_\w])_([^_\n]+)_(?!_)")
_MD_STRIKE = _re.compile(r"~~(.+?)~~", _re.S)
# No brackets inside either part: with them, "[a](b" repeated makes every
# opening bracket scan to the end of the line, which is quadratic, and a
# regex holds the GIL -- one crafted README froze every request.
_MD_LINK = _re.compile(r"\[([^\]\[]*)\]\(([^)\s\[]+)(?:\s+\"[^\"]*\")?\)")
# Reference links: "[text][label]" or the collapsed "[label][]", against
# definitions gathered from lines like "[label]: https://…". A document that
# cites thirty commits keeps them at the bottom rather than inline, and until
# now every one of those rendered as literal brackets.
_MD_REF = _re.compile(r"\[([^\]\[]+)\]\[([^\]\[]*)\]")
_MD_REFDEF = _re.compile(
    r"^ {0,3}\[([^\]\[]+)\]:\s*<?([^>\s]+)>?(?:\s+[\"'(].*)?\s*$")
_MD_HEAD = _re.compile(r"^(#{1,6})\s+(.*)$")
_MD_ULI = _re.compile(r"^[ \t]*[-*+]\s+(.*)$")
_MD_OLI = _re.compile(r"^[ \t]*(\d+)[.)]\s+(.*)$")
_MD_HR = _re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_MD_ROW = _re.compile(r"^\s*\|?(.+\|.+?)\|?\s*$")
# \( \), $$ $$, and single $ $ -- the last is what everything from pandoc to
# GitHub emits, and roost did not read it, so a document that had gone to the
# trouble of writing real TeX rendered as literal dollars. It is the risky
# delimiter (prose has money in it), so it must look like TeX: a backslash or
# one of _ ^ { } inside, no blank line, no space just inside either dollar.
# Bounded: an unbounded lazy span rescans to the end of the text from every
# unclosed "\(" or "$$", which is quadratic and stalls the whole server.
_MD_IMATH = _re.compile(
    r"\\\([\s\S]{1,2000}?\\\)"
    r"|\$\$[\s\S]{1,2000}?\$\$"
    r"|(?<![\w$])\$(?!\s)((?:[^$\n]|\n(?!\n)){1,300}?)(?<!\s)\$(?![\w$])")
# Escaped text, so a <https://…> autolink arrives as &lt;…&gt;.
_MD_AUTO = _re.compile(r'(?<![">=\w])(&lt;)?(https?://[^\s<>"\']+)')
_MD_SEP = _re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")

class _RefMatch:
    """Just enough of a match object for link() to read: it wants group(1)
    for the text and group(2) for the target, and a reference link has both
    -- from two different places."""
    def __init__(self, text, target):
        self._g = (None, text, target)
    def group(self, i):
        return self._g[i]

def _md_inline(s, base="", refs=None):
    """Escape, then mark up. Code spans and finished anchors are held out of
    the way in a stash: code so its contents are not read as emphasis, and
    anchors so the bare-URL pass at the end cannot re-link what is already
    a link."""
    # NUL is the stash delimiter; a literal one in the text would name a
    # stash entry that does not exist.
    s = s.replace("\x00", "")
    # Past this size the inline patterns cost more than the markup is worth,
    # and some of them are not linear. Plain escaped text is still readable.
    if len(s) > 50000:
        return _html.escape(s)
    spans = []
    def hold(html):
        spans.append(html)
        return "\x00%d\x00" % (len(spans) - 1)

    def code(m):
        raw = m.group(1)
        if raw.startswith(("http://", "https://")) and " " not in raw:
            return hold(_anchor(raw, "", _html.escape(raw)))
        out = "<code>%s</code>" % _html.escape(raw)
        # Backticks are the usual way a document names a file. Same resolver
        # as bare prose, so a path written relative to the checkout it
        # describes is found there too, and the resolved path goes underneath.
        if base and len(raw) < 400 and " " not in raw and ("/" in raw or "." in raw):
            url, full = _ref_cached(raw, base)
            if url:
                out = _anchor(url, out, _html.escape(full), "pathref") + _gh_link(full)
        return hold(out)
    s = _MD_CODE.sub(code, s)
    # Inline math, for the same reason: nothing inside it is markdown.
    def _imath(m):
        # A single-dollar span only counts as maths if it looks like maths.
        if m.group(1) is not None and not _re.search(r"[\\\\_^{}]", m.group(1)):
            return m.group(0)
        return hold('<span class="imath">%s</span>' % _html.escape(m.group(0)))
    s = _MD_IMATH.sub(_imath, s)
    s = _html.escape(s)

    def link(m):
        target = m.group(2)
        text = _html.escape(m.group(1) or "")
        if target.startswith(("http://", "https://")):
            # Link text can say anything -- a local path, another site. When
            # it is not the address itself, the address's host goes next to it.
            from urllib.parse import urlsplit
            host = urlsplit(target).hostname or "?"
            if (m.group(1) or "").strip() not in (target, ""):
                text += ' <span class="linkhost">(%s)</span>' % _html.escape(host)
            return hold(_anchor(target, text, _html.escape(target)))
        ref = file_ref(target, base)
        if ref:                          # a real file, served in its own tab
            return hold(_anchor(ref, text, _html.escape(target), "fileref"))
        # Not something we can reach: keep the target visible as text.
        return "%s (%s)" % (text, _html.escape(target))
    s = _MD_LINK.sub(link, s)

    if refs:
        def ref_link(m):
            text = m.group(1)
            label = (m.group(2) or text).strip().lower()
            target = refs.get(label)
            if not target:
                return m.group(0)        # no such definition: leave it alone
            return link(_RefMatch(text, target))
        s = _MD_REF.sub(ref_link, s)

    s = _MD_BOLD.sub(lambda m: "<strong>%s</strong>" % (m.group(1) or m.group(2)), s)
    s = _MD_ITAL.sub(lambda m: "<em>%s</em>" % (m.group(1) or m.group(2)), s)
    s = _MD_STRIKE.sub(lambda m: "<del>%s</del>" % m.group(1), s)

    s = _link_pass(s, base, hold)
    return _re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], s)


def _auto_sub(m, hold):
    lead, cand = m.group(1) or "", m.group(2)
    # The text is escaped and paragraphs are joined with <br>, so a URL run
    # can contain &lt; / &gt;. Stop at the first of those.
    stop = len(cand)
    for cut in ("&lt;", "&gt;"):
        j = cand.find(cut)
        if j != -1:
            stop = min(stop, j)
    url = cand[:stop]
    while url and url[-1] in ".,;:!?":
        url = url[:-1]
    if url.endswith(")") and url.count("(") < url.count(")"):
        url = url[:-1]
    if not url:
        return m.group(0)
    rest = cand[len(url):]
    if lead and rest.startswith("&gt;"):
        lead, rest = "", rest[4:]            # the <…> wrapper, consumed
    return lead + hold(_anchor(_html.unescape(url), "", url)) + rest


# Three shapes worth testing: a multi-segment path, a bare filename with an
# extension, and a single name with a trailing slash. Deliberately narrow —
# every candidate costs a stat — and nothing becomes a link unless it
# resolves, which is what keeps "and/or", "km/h" and "3/4" out of it.
_PATH_TOK = _re.compile(
    r'(?<![\w/@.:-])('
    r'(?:~|\.\.?)?/?[\w.+-]+(?:/[\w.+-]+)+/?'      # a/b, ~/a/b, ./a/b/
    r'|[\w][\w.+-]*\.[A-Za-z0-9]{1,6}'              # README.md
    r'|[\w][\w.+-]*/'                                # reference/
    r')')

_REF_CACHE = {}

def _ref_cached(tok, base):
    k = (base, tok)
    if k not in _REF_CACHE:
        if len(_REF_CACHE) > 20000:
            _REF_CACHE.clear()
        _REF_CACHE[k] = ref_and_path(tok, base)
    return _REF_CACHE[k]

def _path_sub(m, base, hold):
    tok = m.group(1)
    if len(tok) < 4 or tok.count("/") > 24:
        return tok
    url, full = _ref_cached(tok, base)
    if not url:
        return tok
    return hold(_anchor(url, tok, _html.escape(full), "pathref") + _gh_link(full))

def _gh_link(full):
    gh, exact = github_url(full)
    if not gh:
        return ""
    # An inexact link is the last pushed revision, not what is on disk. Say
    # so rather than presenting it as the same thing.
    note = "" if exact else '<span class="ghn"> · last pushed</span>'
    return ('<a class="gh" href="%s" target="_blank" rel="noreferrer" '
            'title="%s">%s%s</a>'
            % (_html.escape(gh, quote=True),
               "this revision on GitHub" if exact
               else "the newest revision GitHub has; local HEAD is not pushed",
               _html.escape(gh), note))

def _link_pass(esc, base, hold):
    """Bare URLs, then bare paths. Order matters: a URL contains slashes and
    would otherwise be picked apart by the path pass."""
    esc = _MD_AUTO.sub(lambda m: _auto_sub(m, hold), esc)
    if base:
        esc = _PATH_TOK.sub(lambda m: _path_sub(m, base, hold), esc)
    return esc

def link_urls(esc, base=""):
    """Standalone version, for text that is not going through _md_inline —
    a code fence, where a list of artefact paths or URLs is often the whole
    point of the block."""
    spans = []
    def hold(html):
        spans.append(html)
        return "\x00%d\x00" % (len(spans) - 1)
    esc = esc.replace("\x00", "")      # NUL delimits the stash
    out = _link_pass(esc, base, hold)
    return _re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], out)

def _anchor(href, text, shown, cls=""):
    """One link.

    A URL is evidence, so it is never hidden -- but printing all 120
    characters of it after every citation turned a paragraph of prose into a
    paragraph of URLs. When a link has text of its own, the text is what
    shows and the address is on the hover and on the copy button beside it.
    When it has none -- a bare URL in the text, or a path found in prose --
    the address is all there is, and it stays visible."""
    url = '<span class="u">%s</span>' % shown
    # A path written in prose usually resolves to exactly itself with the
    # folder in front -- "work/app2-refactor" becoming
    # "/home/you/src/.../work/app2-refactor" -- and printing both is the
    # same string twice. The resolution earns its place only when it is not
    # what the text already says: a bare filename found somewhere else, a
    # partial path that turned out to live under a different branch.
    # The text may carry markup of its own -- a path in backticks arrives as
    # <code>…</code> -- so compare on the words, not the markup.
    tail = _html.unescape(_re.sub(r"<[^>]+>", "", text or ""))
    obvious = bool(tail) and (shown == tail or shown.endswith("/" + tail))
    named = bool(text) and not obvious and cls != "pathref"
    if named or (obvious and cls == "pathref"):
        inner = text                     # the words are enough; URL on hover
        named = True
    elif text and text != shown:
        inner = "%s %s" % (text, url)    # a resolution worth printing
    else:
        inner = url                      # the address is all there is
    # An external URL opens elsewhere; a file on this machine opens here,
    # because following a trail of them is the point and a tab per step is
    # not.
    local = cls in ("fileref", "pathref")
    a = ('<a href="%s"%s rel="noreferrer"%s%s>%s</a>'
         % (_html.escape(href, quote=True),
            "" if local else ' target="_blank"',
            ' class="%s"' % " ".join(x for x in (cls, "named" if named else "")
                                     if x) if (cls or named) else "",
            ' title="%s"' % shown if named else "",
            inner))
    # A long link is one you want to send someone, and selecting a wrapped
    # URL across three lines with a thumb is not a thing anybody manages. The
    # button carries the link itself, so the page needs no bookkeeping: one
    # delegated handler copies what is on it. Short links do without.
    if named or len(shown) > 48:
        a += ('<button class="cp" type="button" title="copy this link" '
              'data-u="%s" data-p="%s">\u29c9</button>'
              % (_html.escape(href, quote=True), _html.escape(shown, quote=True)))
    return a

# Borrowed from an earlier blockify, which feeds the same KaTeX: a document
# written for LaTeX proper carries \begin{equation} … \label{…}, none of
# which KaTeX knows. Unwrap the environment, map the multi-line ones to the
# inner forms KaTeX does support, and drop the labels.
_MATH_ENV = _re.compile(r"^\\begin\{(equation\*?|align\*?|aligned|gather\*?|"
                        r"gathered|multline\*?|eqnarray\*?)\}")
_MATH_LABEL = _re.compile(r"\\label\{[^}]*\}")

def katex_ready(tex):
    """Display maths as KaTeX can take it."""
    inner = _MATH_LABEL.sub("", tex).strip()
    m = _MATH_ENV.match(inner)
    if not m:
        return inner
    env = m.group(1)
    body = inner[m.end():]
    end = "\\end{%s}" % env
    if end in body:
        body = body.rsplit(end, 1)[0]
    body = body.strip()
    if env.startswith(("align", "eqnarray")):
        return "\\begin{aligned}%s\\end{aligned}" % body
    if env.startswith(("gather", "multline")):
        return "\\begin{gathered}%s\\end{gathered}" % body
    return body

# Markdown has no way to colour a table cell, and letting a document write
# raw <td style=…> would undo the reason file content is safe to render at
# all. So: a cell may open with a colour in braces -- "{green} passed",
# "{#1f6feb} 12" -- which is stripped from the text and turned into a
# background. Only a name from this palette or a plain hex triple is
# accepted; anything else stays as literal text, so a document that happens
# to start a cell with a brace is unharmed.
#
# The named tints are translucent, so they sit on either theme and leave the
# text legible; a hex colour is the author's own business.
_CELL_BG = {
    "red":    "rgba(217,87,87,.22)",   "green":  "rgba(137,209,133,.20)",
    "amber":  "rgba(226,192,141,.22)", "yellow": "rgba(226,192,141,.22)",
    "orange": "rgba(217,119,87,.22)",  "blue":   "rgba(99,168,238,.20)",
    "purple": "rgba(197,134,192,.20)", "grey":   "rgba(255,255,255,.08)",
    "gray":   "rgba(255,255,255,.08)", "none":   "",
}
_CELL_MARK = _re.compile(r"^\{([#A-Za-z0-9]{2,9})\}\s*")
_HEXCOL = _re.compile(r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")

def cell_colour(text):
    """(style attribute, remaining text) for a cell that opens with {colour}."""
    m = _CELL_MARK.match(text or "")
    if not m:
        return "", text
    want = m.group(1)
    if _HEXCOL.match(want):
        bg = want
    elif want.lower() in _CELL_BG:
        bg = _CELL_BG[want.lower()]
    else:
        return "", text                  # not a colour: leave it alone
    rest = text[m.end():]
    return (' style="background:%s"' % bg if bg else ""), rest

def _md_cells(line):
    line = line.strip()
    if line.startswith("|"): line = line[1:]
    if line.endswith("|"): line = line[:-1]
    return [c.strip() for c in line.split("|")]

# A newline inside a paragraph is a soft break in markdown: the same as a
# space. Rendering every one as <br> shredded hard-wrapped documents -- a
# file filled to 72 columns came out with a line break every 72 characters
# regardless of how wide the window was, which is the "too many newlines in
# the wrong places" this fixes.
#
# But not every newline in a paragraph came from a fill. Transcripts and
# covers are full of stanzas broken by meaning -- "Branch: x" over "Status:
# y" -- and joining those loses the shape the author gave them. The tell is
# the right margin: a filled paragraph's lines all end near the same column,
# a deliberate one's do not. So the lines are joined only when every line
# but the last runs close to the paragraph's own widest line.
_WRAP_MIN = 55       # narrower than this is not a fill, it is a stanza
# How much room a line must have left for the next word before the break
# counts as deliberate. Nobody fills to the exact column every time: a line
# ending one short of the margin is still a fill, and calling it a choice
# put a break in the middle of a sentence.
_WRAP_GAP = 4

def _para_join(buf):
    """One paragraph's lines, joined as spaces if they were wrapped by fill
    and kept as breaks if they were not."""
    if len(buf) < 2:
        return buf[0] if buf else ""
    body = [ln.rstrip() for ln in buf]
    # Two trailing spaces is markdown for "break here", and settles it.
    if any(ln.endswith("  ") for ln in buf[:-1]):
        return "<br>".join(buf)
    # The fill column, estimated from the middle of the distribution rather
    # than from the longest line: one line carrying a URL or a long flag
    # runs past the margin, and taking the maximum let that single line
    # declare a whole filled paragraph "deliberate".
    lens = sorted(len(ln) for ln in body)
    margin = lens[len(lens) // 2]
    if margin < _WRAP_MIN:
        return "<br>".join(buf)
    # The test is what a filling program would have done: it never leaves a
    # line with room for the next word. So a line that could have held the
    # word below it was ended on purpose, and the paragraph keeps its
    # breaks. A line that is short only because the next word did not fit is
    # a fill, and joins. This is stricter than "all the lines are long",
    # which called a paragraph deliberate whenever one line happened to be
    # followed by a long URL or identifier.
    # Per paragraph, by majority -- not "any break that looks deliberate".
    # A filled paragraph can contain one line the author ended early, and
    # treating that single break as intent kept every break in the paragraph:
    # six lines of prose stayed shredded because one of them was 21 columns
    # instead of 79. A stanza is a paragraph where MOST of the breaks are
    # deliberate; one odd line is an anomaly, not a shape.
    breaks = deliberate = 0
    for a, b in zip(body, body[1:]):
        breaks += 1
        nxt = b.strip().split(" ", 1)[0]
        if len(a) + 1 + len(nxt) <= margin - _WRAP_GAP:
            deliberate += 1
    if deliberate * 2 > breaks:
        return "<br>".join(buf)
    return " ".join(ln.strip() for ln in body)

def md_html(text, base=""):
    lines = (text or "").split("\n")
    # Link definitions first: they can sit anywhere, they are not content,
    # and a use may appear pages before the line that defines it.
    refs = {}
    kept = []
    fence = False
    for ln in lines:
        if ln.lstrip().startswith("```"):
            fence = not fence
        m = None if fence else _MD_REFDEF.match(ln)
        if m:
            refs[m.group(1).strip().lower()] = m.group(2)
        else:
            kept.append(ln)
    lines = kept
    out, i, n = [], 0, len(lines)
    while i < n:
        ln = lines[i]
        # Display math, taken whole. Markdown has no idea what a formula is:
        # "+" at the start of a line became a bullet, and the "_" pairs of
        # \underbrace{...}_{...} were eaten as italics across the joined
        # lines, which is how U-f^\star}_{\text{...}} arrived with its
        # subscript missing.
        lstr = ln.strip()
        if lstr.startswith("\\[") or lstr == "$$":
            close = "\\]" if lstr.startswith("\\[") else "$$"
            buf, i = [ln], i + 1
            while i < n and close not in lines[i]:
                buf.append(lines[i]); i += 1
            if i < n:
                buf.append(lines[i]); i += 1
            out.append('<div class="math">%s</div>'
                       % _html.escape("\n".join(buf)))
            continue
        if ln.lstrip().startswith("```"):                      # fenced code
            fence = ln.lstrip()[:3]
            info = ln.lstrip()[3:].strip().lower()
            i += 1
            buf = []
            while i < n and not lines[i].lstrip().startswith(fence):
                buf.append(lines[i]); i += 1
            i += 1
            # ```math is how pandoc and GitHub write display maths. Rendered
            # as a code block it came out as monospace TeX source.
            if info in ("math", "latex", "tex"):
                out.append('<div class="math">%s</div>'
                           % _html.escape(katex_ready("\n".join(buf))))
            else:
                out.append("<pre class=\"code\">%s</pre>"
                           % link_urls(_html.escape("\n".join(buf)), base))
            continue
        if not ln.strip():
            i += 1; continue
        if _MD_HR.match(ln):
            out.append("<hr>"); i += 1; continue
        m = _MD_HEAD.match(ln)
        if m:
            lvl = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (lvl, _md_inline(m.group(2), base, refs), lvl))
            i += 1; continue
        # table: a row of pipes followed by a --- separator row
        if i + 1 < n and "|" in ln and len(lines[i + 1]) < 2000 \
           and _MD_SEP.match(lines[i + 1]) \
                and _MD_ROW.match(ln):
            head = _md_cells(ln)
            i += 2
            body = []
            while i < n and "|" in lines[i] and lines[i].strip():
                body.append(_md_cells(lines[i])); i += 1
            out.append("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (
                "".join("<th%s>%s</th>" % (lambda sc: (sc[0], _md_inline(sc[1], base, refs)))(cell_colour(c))
                        for c in head),
                "".join("<tr>%s</tr>" % "".join(
                    "<td%s>%s</td>" % (lambda sc: (sc[0], _md_inline(sc[1], base, refs)))(cell_colour(c))
                    for c in r) for r in body)))
            continue
        if _MD_ULI.match(ln) or _MD_OLI.match(ln):
            ordered = bool(_MD_OLI.match(ln))
            items = []
            while i < n:
                mm = _MD_OLI.match(lines[i]) if ordered else _MD_ULI.match(lines[i])
                if not mm: break
                part = [mm.group(2) if ordered else mm.group(1)]
                i += 1
                # An item does not end at the end of its first line. A list
                # filled to 80 columns continues on indented lines below, and
                # taking only the first line left every item cut off with the
                # rest of it dumped after the list as loose prose -- which is
                # what the stray line breaks in these documents were.
                while i < n and lines[i].strip() and lines[i][:1] in (" ", "\t") \
                        and not _MD_ULI.match(lines[i]) \
                        and not _MD_OLI.match(lines[i]):
                    part.append(lines[i].strip())
                    i += 1
                items.append(_md_inline(_para_join(part), base, refs))
            tag = "ol" if ordered else "ul"
            out.append("<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % x for x in items), tag))
            continue
        if ln.lstrip().startswith(">"):
            buf = []
            while i < n and lines[i].lstrip().startswith(">"):
                buf.append(lines[i].lstrip()[1:].lstrip()); i += 1
            out.append("<blockquote>%s</blockquote>" % _md_inline(" ".join(buf), base, refs))
            continue
        buf = []                                               # paragraph
        while i < n and lines[i].strip() and not (
                lines[i].lstrip().startswith(("```", ">", "#"))
                or _MD_ULI.match(lines[i]) or _MD_OLI.match(lines[i])
                or _MD_HR.match(lines[i])):
            buf.append(lines[i]); i += 1
        if not buf:
            # Nothing above claimed this line and nothing below will: a "#"
            # with no space after it is not a heading, and the loop skips
            # lines starting with "#". So "#include <stdio.h>" consumed
            # nothing, the index never moved, and this walker appended empty
            # paragraphs until the process ran out of memory -- 9GB of them,
            # which is what took the server down. Always consume a line.
            buf.append(lines[i]); i += 1
        out.append("<p>%s</p>"
                   % _md_inline(_para_join(buf), base, refs)
                     .replace("&lt;br&gt;", "<br>"))
    return "".join(out)

# Entries never change once written, so the render is worth keeping. Keyed on
# the text itself: the same entry is re-rendered on every poll otherwise.
_MD_CACHE = {}

def md_cached(text, base=""):
    # The cwd is part of the key: the same text under a different session
    # resolves its relative paths to different files.
    k = hash((text, base))
    v = _MD_CACHE.get(k)
    if v is None:
        if len(_MD_CACHE) > 8000:
            _MD_CACHE.clear()
        v = _MD_CACHE[k] = md_html(text, base)
    return v

# A file opened from a transcript is usually evidence: a JSON receipt, a CSV
# of measurements, a generated .md. Served as text/plain those are a wall of
# characters on a phone, so these render them. The page carries no script of
# its own and says so in its CSP, which is what makes it safe to build HTML
# out of file content at all.

# Parsing costs memory, so there is a ceiling on what gets rendered; past it
# the file is previewed instead. Evidence files run to hundreds of megabytes
# — one linked from a real transcript is 950MB — and neither reading nor
# parsing that belongs in a page request.
# Where a pasted image lands. Under $HOME, not /tmp: systemd-tmpfiles is set
# to empty /tmp at boot and to sweep anything untouched for 30 days, and a
# screenshot you pasted into a conversation is part of that conversation. The
# folder is added to the roots below so the viewer can still open it.
# One-time move from the name this had before. A dot-directory that stays
# behind is worse than one that moves: the slots file holds messages in
# flight between sessions and the pasted images are part of conversations.
# Both are found by path, and nothing else knows where they went.
_OLD_STATE = Path.home() / ".ccrc"
_STATE = Path.home() / ".roost"
if _OLD_STATE.is_dir() and not _STATE.exists():
    try:
        _OLD_STATE.rename(_STATE)
    except OSError:
        pass                          # keep the old one; paths below just miss

PASTE_DIR = Path.home() / ".roost" / "pasted"
PASTE_MAX = 24 * 1024 * 1024         # a phone photo, with room to spare

FILE_VIEW_MAX = 24 * 1024 * 1024     # parse-and-render ceiling
FILE_HEAD = 512 * 1024               # how much of a bigger file to preview

# KaTeX and highlight.js, served from here rather than from a CDN.
#
# The CDN was a silent single point of failure: a browser that cannot reach
# cdnjs -- a phone on a network that blocks it, a tailnet with no route out,
# a content blocker -- got the page, the markup and the LaTeX source, and
# nothing to turn it into type. The failure looks exactly like "maths does
# not work": \[ ... \] sitting in the prose. Both libraries are small and
# neither changes, so roost carries them -- and a page that loads nothing
# from anywhere else is also a page that tells nobody else it was opened.
VENDOR = Path(__file__).resolve().parent / "vendor"
_VENDOR_TYPES = {".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".woff2": "font/woff2", ".woff": "font/woff",
                 ".ttf": "font/ttf", ".map": "application/json"}
KATEX_V, HLJS_V = "0.16.11", "11.9.0"

def _lib(rel, cdn, ver):
    """A URL for one library file under vendor/. Never a CDN: without the
    file the page renders without it (maths stays source)."""
    return "vendor/%s?v=%s" % (rel, ver)     # versioned: it is cached hard

# Inter, self-hosted and variable: one file covers every weight, so 350 for
# text and 600 for bold are both real cuts rather than the browser smearing
# a regular. Latin only -- anything else falls through to the system stack.
INTER_CSS = """
  @font-face { font-family: Inter; font-style: normal; font-weight: 100 900;
               font-display: swap; src: url(%s) format("woff2"); }
  @font-face { font-family: Inter; font-style: italic; font-weight: 100 900;
               font-display: swap; src: url(%s) format("woff2"); }
""" % (_lib("inter/inter-latin.woff2", "", "1"),
       _lib("inter/inter-latin-italic.woff2", "", "1"))
if not (VENDOR / "inter" / "inter-latin.woff2").exists():
    INTER_CSS = ""          # no copy on disk: the system stack is the answer

def roost_icon(px):
    """roost's own mark: the remote control from the app icon, at one size."""
    return ('<svg viewBox="0 0 24 24" width="{0}" height="{0}" aria-hidden="true"'
            ' style="display:block">'
            '<g fill="none" stroke="currentColor" stroke-width="1.6"'
            ' stroke-linecap="round">'
            '<path d="M14.4 3.6a5.2 5.2 0 0 1 3.7 3.7"/>'
            '<path d="M15.2 0.9a8 8 0 0 1 5.9 5.9"/></g>'
            '<rect x="4.6" y="7.4" width="8.2" height="15.2" rx="2.2"'
            ' fill="currentColor"/>'
            '<circle cx="8.7" cy="10.8" r="1.35" fill="#d8503c"/>'
            '<g fill="#14141a">'
            '<circle cx="7.1" cy="14.6" r="0.85"/><circle cx="10.3" cy="14.6" r="0.85"/>'
            '<circle cx="7.1" cy="17.2" r="0.85"/><circle cx="10.3" cy="17.2" r="0.85"/>'
            '<circle cx="7.1" cy="19.8" r="0.85"/><circle cx="10.3" cy="19.8" r="0.85"/>'
            '</g></svg>').format(px)


def home_link(px=20):
    """The way back to the sessions, first in every page's bar. An anchor, so
    it can be opened in a tab of its own like any other link, and the mark
    rather than the word "sessions": it is the same control on every page and
    should look like one. Styled inline because these pages each carry their
    own small stylesheet and this is one element in all of them."""
    return ('<a class="home" href="." title="roost — the sessions"'
            ' style="display:inline-flex;align-items:center;color:inherit;'
            'text-decoration:none;padding:.15rem .1rem;flex:none">%s</a>'
            % roost_icon(px))


def files_icon(px):
    """The files glyph at one size: a folder, and the tree it opens into."""
    return ('<svg viewBox="0 0 24 24" width="{0}" height="{0}" aria-hidden="true"'
            ' fill="none" stroke="currentColor" stroke-width="1.7"'
            ' stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M2.6 6.4a1.6 1.6 0 0 1 1.6-1.6h3.4l1.8 2h4.8a1.6 1.6 0 0'
            ' 1 1.6 1.6v2.2"/>'
            '<path d="M2.6 6.4v11.2a1.6 1.6 0 0 0 1.6 1.6h4.2"/>'
            '<path d="M12.6 9.2v9.2M12.6 12.4h3.2M12.6 18.4h3.2"/>'
            '<circle cx="18.4" cy="12.4" r="1.8"/>'
            '<circle cx="18.4" cy="18.4" r="1.8"/></svg>').format(px)

KATEX_TAGS = (
    '<link rel="stylesheet" href="%s">\n'
    '<script defer src="%s"></script>'
    % (_lib("katex/katex.min.css", "KaTeX/%s/katex.min.css" % KATEX_V, KATEX_V),
       _lib("katex/katex.min.js", "KaTeX/%s/katex.min.js" % KATEX_V, KATEX_V)))
HLJS_TAGS = (
    '<link rel="stylesheet" href="%s">\n'
    '<script defer src="%s"></script>'
    % (_lib("hljs/github-dark.min.css",
            "highlight.js/%s/styles/github-dark.min.css" % HLJS_V, HLJS_V),
       _lib("hljs/highlight.min.js",
            "highlight.js/%s/highlight.min.js" % HLJS_V, HLJS_V)))

FILE_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>__NAME__</title>
<link rel="icon" href="icon.png?s=64" type="image/png">
__HLJS__
__KATEX__
<style>
__INTER__
  /* Scrollbars the way an editor does them: a thin translucent thumb over
     the content, no track, no arrows, and a little more contrast while the
     pointer is on it. Firefox takes the two-property form; WebKit and
     Blink need the pseudo-elements, and the transparent border plus
     background-clip is what insets the thumb rather than letting it touch
     the edge. */
  * { scrollbar-width: thin; scrollbar-color: rgba(255,255,255,.16) transparent; }
  ::-webkit-scrollbar { width: 11px; height: 11px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-corner { background: transparent; }
  ::-webkit-scrollbar-thumb { background: rgba(255,255,255,.16);
      border-radius: 8px; border: 3px solid transparent;
      background-clip: content-box; }
  ::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,.30);
      border: 2px solid transparent; background-clip: content-box; }
  ::-webkit-scrollbar-thumb:active { background: rgba(255,255,255,.42);
      border: 2px solid transparent; background-clip: content-box; }
  :root {
    --c-bg: #151515; --c-raised: #20201f; --c-deep: #0b0b0b;
    --c-text: #f0efec; --c-muted: #898781;
    --c-line: rgba(255,255,255,.10); --c-line-strong: rgba(255,255,255,.20);
    --c-accent: #63a8ee; --c-clay: #d97757;
    --c-font: system-ui, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    --c-mono: ui-monospace, SFMono-Regular, Menlo, monospace;
    /* Inter, at a weight below regular. Computer Modern was the right idea
       for a paper and the wrong one for a backlit dark screen: at reading
       size its stems bloom and every bold run reads as a shout. A slim
       grotesque holds its colour at 350 and lets bold be 600 rather than
       700. Variable, so those weights are real rather than synthesised,
       and self-hosted -- two files, latin only. */
    --c-read: Inter, system-ui, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    --c-read-ink: #e0ddd7;
    --c-measure: 37rem;        /* about 70 characters at this size */
    --c-measure-wide: 58rem;   /* and about 110 */
  }
  /* The header stays, and the two panels scroll on their own: walking a
     long tree should not carry the document away, and reading to the end of
     a document should not lose your place in the tree. */
  html, body { height: 100%; margin: 0; background: var(--c-bg);
               color: var(--c-text); font: 1rem/1.62 var(--c-font);
               overflow: hidden; }
  body { display: flex; flex-direction: column; }
  header { flex: none; display: flex; gap: .6rem; align-items: center;
           flex-wrap: wrap; padding: .5rem .8rem;
           border-bottom: 1px solid var(--c-line); background: var(--c-bg); }
  /* The tree is a panel beside the document, not over it: on a phone it
     takes the whole width instead, because a 14rem sidebar next to a 24rem
     document leaves neither readable. */
  #body { flex: 1 1 auto; display: flex; align-items: stretch;
          min-height: 0; overflow: hidden; }
  #tree { display: none; flex: 0 0 var(--treew, 17rem); max-width: 85vw;
          overflow-y: auto; overscroll-behavior: contain;
          border-right: 1px solid var(--c-line); padding: .4rem 0 2rem; }
  body.tree #tree { display: block; }
  /* The column is draggable because paths outgrow any width picked for
     them. The handle is the border: a few pixels of nothing, widened by a
     transparent hit area so a finger can still find it. Double-click puts
     it back. */
  #grip { display: none; flex: 0 0 5px; margin: 0 -2px; z-index: 2;
          cursor: col-resize; background: transparent;
          border-left: 2px solid transparent; }
  body.tree #grip { display: block; }
  #grip:hover, #grip.drag { background: var(--c-accent); opacity: .55; }
  body.grabbing { cursor: col-resize; user-select: none; }
  #tree .n { display: flex; align-items: center; gap: .3rem; cursor: pointer;
             padding: .12rem .5rem; white-space: nowrap; font-size: .82rem;
             color: var(--c-text); }
  #tree .n:hover { background: rgba(255,255,255,.05); }
  #tree .n.here { background: rgba(255,255,255,.09); font-weight: 600; }
  #tree .tw { color: var(--c-muted); width: .8rem; flex: none;
              font-size: .7rem; text-align: center; }
  /* File icons, the way an editor does them: not pictures but a glyph and a
     colour, which is what actually distinguishes them at this size. Colours
     follow the families a person sorts by — prose, data, shell, code, media,
     secrets — rather than one per extension. */
  #tree .ic { flex: none; width: 1rem; text-align: center; font-size: .78rem;
              opacity: .95; }
  #tree .ic-dir  { color: #7aa7ff; }
  #tree .ic-doc  { color: #6cb6ff; }
  #tree .ic-data { color: #e2c08d; }
  #tree .ic-sh   { color: #89d185; }
  #tree .ic-code { color: #4ec9b0; }
  #tree .ic-img  { color: #c586c0; }
  #tree .ic-key  { color: #d7ba7d; }
  #tree .ic-arch { color: #b58a5a; }
  #tree .ic-none { color: var(--c-muted); opacity: .6; }
  #tree .n.hidden .nm { opacity: .55; }
  #tree .nm { overflow: hidden; text-overflow: ellipsis; }
  #tree .kids { margin-left: .7rem; border-left: 1px solid var(--c-line); }
  /* The two reading modes sit at the far right, away from the controls that
     act on the tree: they are about this pane, not about that one. */
  #modes { margin-left: auto; display: flex; gap: .3rem; flex: none; }
  #modes button { padding: .18rem .4rem; line-height: 0; }
  #modes svg { display: block; }
  /* The glyph is the whole button, so it sets the height rather than sitting
     on a text baseline with a descender's worth of space beneath it. */
  #treebtn { line-height: 0; padding: .3rem .42rem; }
  #treebtn svg { display: block; }
  #treebtn.on, #sorttime.on, #sortname.on, #readbtn.on, #midbtn.on,
  #widebtn.on {
      background: var(--c-raised); border-color: var(--c-accent);
      color: var(--c-text); }
  /* The one that is not in force reads as available, not as chosen. */
  #sorttime, #sortname, #readbtn, #midbtn, #widebtn { color: var(--c-muted); }
  /* What a row in the tree actually is: where it lives, when it last
     changed, whose session it belongs to and which repository. Positioned
     against the viewport, so it is not clipped by the tree's own scroll. */
  /* Wide, because these are paths: at 34rem a research path under /tmp
     wrapped onto three lines and the shape of it -- which is what you are
     reading it for -- was gone. The box still shrinks to its content, so a
     short row gets a short box. */
  #tip { position: fixed; z-index: 20; max-width: min(72rem, 94vw);
         width: max-content;
         background: var(--c-raised); color: var(--c-text);
         border: 1px solid var(--c-line-strong); border-radius: 8px;
         padding: .45rem .6rem; font-size: .76rem; line-height: 1.5;
         box-shadow: 0 .5rem 1.4rem #000a; pointer-events: none; }
  #tip b { font-weight: 600; overflow-wrap: anywhere; }
  #tip div { display: flex; gap: .5rem; }
  #tip i { flex: 0 0 3.6rem; font-style: normal; color: var(--c-muted); }
  #tip span { overflow-wrap: anywhere; }
  #tip em { font-style: normal; color: var(--c-muted); }
  /* A moment's highlight, for the row the reveal button just found. */
  @keyframes found { from { background: var(--c-accent); }
                     to   { background: rgba(255,255,255,.09); } }
  #tree .n.found { animation: found 1.1s ease-out; }
  select, button { font: inherit; font-size: .78rem; color: var(--c-text);
      background: var(--c-raised); border: 1px solid var(--c-line-strong);
      border-radius: 6px; padding: .2rem .4rem; cursor: pointer; }
  @media (max-width: 700px) {
    body.tree #tree { flex: 1 1 100%; max-width: none; border-right: 0; }
    body.tree main, body.tree #grip { display: none; }
  }
  header .n { font-weight: 600; word-break: break-all; }
  header .s, header .p { color: var(--c-muted); font-size: .78rem; }
  header .p { word-break: break-all; }
  a { color: var(--c-accent); }
  main { flex: 1 1 auto; min-width: 0; overflow-y: auto;
         overscroll-behavior: contain; padding: .7rem .8rem 2rem; }
  pre { margin: 0; white-space: pre-wrap; word-break: break-word;
        font: 13px/1.55 var(--c-mono); }
  table { border-collapse: collapse; font-size: .92rem; display: block;
          overflow-x: auto; max-width: 100%; width: max-content; }
  /* A prose column will take everything it is offered if nothing stops it.
     This is the width at which a cell starts wrapping instead of pushing
     the next column off the page. */
  td, th { max-width: 30rem; }
  /* Cells wrap. nowrap suits a column of numbers or hashes and is exactly
     wrong for a column of sentences: a table of prose became one line per
     row, several thousand pixels wide, scrolling sideways inside a box that
     was already as wide as the page allowed. Wrapping lets the table fit
     the width it is given; the box keeps its horizontal scroll for the
     tables that genuinely need one. Numbers are unaffected -- there is
     nothing in "1.0821e-6" to wrap at. */
  th, td { border: 1px solid var(--c-line-strong); padding: .3rem .55rem;
           text-align: left; vertical-align: top;
           overflow-wrap: break-word; }
  th { background: rgba(255,255,255,.06); font-weight: 600;
       position: sticky; top: 0; }
  tbody tr:nth-child(2n) { background: rgba(255,255,255,.025); }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  .k { color: #9ecbff; } .str { color: #b5e8a9; }
  .lit { color: var(--c-clay); } .n { color: #e8c07d; }
  .note { color: var(--c-muted); font-size: .8rem; margin-top: .8rem; }
  .muted { color: var(--c-muted); }
  th.key { font-weight: 600; white-space: nowrap; text-align: left;
           background: rgba(255,255,255,.04); position: static; }
  h2 { font-size: .95rem; font-weight: 600; margin: 1.1rem 0 .35rem;
       color: var(--c-muted); text-transform: uppercase;
       letter-spacing: .04em; }
  __EXTRA__
</style></head><body>
<header>
  __HOMELINK__
  <button id="treebtn" type="button" title="files">__FILESICON__</button>
  <button id="sorttime" type="button" title="newest first">&darr;&#9719;</button>
  <button id="sortname" type="button" title="A to Z">&darr;A-Z</button>
  <select id="root" title="location">__ROOTS__</select>
  <button id="revealbtn" type="button"
          title="find this file in the tree">⇄ find</button>
  <span class="n">__NAME__</span><span class="s">__SIZE__</span>
  <a href="__RAW__">__RAWLABEL__</a><span class="p">__PATH__</span>
  <span id="modes">
    <button id="readbtn" type="button" title="reader — one column, set for reading">
      <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
        <rect x="3.2" y="1.6" width="9.6" height="12.8" rx="1.4"
              fill="none" stroke="currentColor" stroke-width="1.2"/>
        <path d="M5.4 5h5.2M5.4 7.6h5.2M5.4 10.2h3.4" stroke="currentColor"
              stroke-width="1.2" stroke-linecap="round"/>
      </svg>
    </button>
    <button id="midbtn" type="button" title="wider column — a page and a half">
      <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
        <rect x="1.4" y="2.8" width="13.2" height="10.4" rx="1.4"
              fill="none" stroke="currentColor" stroke-width="1.2"/>
        <path d="M3.6 6h8.8M3.6 8.5h8.8M3.6 11h6" stroke="currentColor"
              stroke-width="1.2" stroke-linecap="round"/>
      </svg>
    </button>
    <button id="widebtn" type="button" title="full width — use the whole window">
      <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"
           fill="none" stroke="currentColor" stroke-width="1.3"
           stroke-linecap="round" stroke-linejoin="round">
        <path d="M6.2 2.2H2.2v4M9.8 2.2h4v4M6.2 13.8H2.2v-4M9.8 13.8h4v-4"/>
        <path d="M5.6 5.6 2.6 2.6M10.4 5.6l3-3M5.6 10.4l-3 3M10.4 10.4l3 3"/>
      </svg>
    </button>
  </span>
</header>
<div id="body"><nav id="tree"></nav><div id="grip"></div><main>__BODY__</main></div>
<div id="tip" hidden></div>
<script>
let HERE = __HERE__;
const HOMEDIR = __HOMEDIR__;
const HEREDIR = __HEREDIR__;
// A document opens at its beginning. The browser otherwise restores where
// the last one was left, which for a long file is somewhere in the middle of
// a document you have never seen.
try { history.scrollRestoration = "manual"; } catch (e) { /* older browser */ }
const mainEl = document.querySelector("main");
if (mainEl) mainEl.scrollTop = 0;
window.addEventListener("pageshow", () => { if (mainEl) mainEl.scrollTop = 0; });
const treeEl = document.getElementById("tree");
const treeBtn = document.getElementById("treebtn");
const rootSel = document.getElementById("root");

// Shown or hidden per browser, so it is a choice made once. Off by default:
// most of the time this page is opened to read one document.
function setTree(on) {
  document.body.classList.toggle("tree", on);
  treeBtn.classList.toggle("on", on);
  try { localStorage.setItem("roost.tree", on ? "1" : "0"); } catch (e) {}
  if (on && !treeEl.dataset.loaded) openTo(HEREDIR);
}
try { if (localStorage.getItem("roost.tree") === "1") setTree(true); }
catch (e) { /* private mode */ }
// Arriving from the dashboard's files icon: the tree is what was asked for.
if (new URL(window.location).searchParams.get("tree") === "1") setTree(true);
treeBtn.onclick = () => setTree(!document.body.classList.contains("tree"));

// Declared up here, not beside the sort helpers below: paintSort() runs at
// once and a `let` read before its declaration is a ReferenceError that
// kills the rest of the script.
let byTime = true;
try { byTime = localStorage.getItem("roost.sort") !== "name"; } catch (e) {}

// Two buttons rather than one that changes meaning: which order is in force
// is then visible without reading the label and working out whether it says
// what you have or what you would get.
const sortTime = document.getElementById("sorttime");
const sortName = document.getElementById("sortname");
let hereDir = HEREDIR;                    // the folder the tree is showing
function paintSort() {
  sortTime.classList.toggle("on", byTime);
  sortName.classList.toggle("on", !byTime);
}
paintSort();
// Find the open document in the tree again. After walking a long tree, or
// switching roots, the file you are reading is somewhere off-screen or in a
// branch that is no longer expanded; this puts the tree back around it.
const revealBtn = document.getElementById("revealbtn");
revealBtn.onclick = async () => {
  setTree(true);
  const root = await rootFor(HERE);
  if (root && rootSel.value !== root) rootSel.value = root;
  treeEl.dataset.loaded = "";
  await openTo(hereDir);
  const hit = treeEl.querySelector(".n.here");
  if (!hit) return;
  hit.classList.remove("found");
  void hit.offsetWidth;                  // restart the animation
  hit.classList.add("found");
};

// Which root the open document sits under, asked of the server so that the
// answer is the same innermost-root rule the rest of the viewer uses.
async function rootFor(path) {
  try {
    const d = await listDir(hereDir);
    return d.root || "";
  } catch (e) { return ""; }
}

function setSort(wantTime) {
  if (byTime === wantTime) return;
  byTime = wantTime;
  try { localStorage.setItem("roost.sort", byTime ? "time" : "name"); } catch (e) {}
  paintSort();
  sortRows();
  // The tree is rebuilt rather than shuffled: a row and the box holding its
  // children are siblings, so reordering one without the other is how a
  // folder ends up with somebody else's contents under it.
  if (treeEl.dataset.loaded) { treeEl.dataset.loaded = ""; openTo(hereDir); }
}
sortTime.onclick = () => setSort(true);
sortName.onclick = () => setSort(false);

// Reading width. A document opens in one column set for reading; the other
// button gives the window back, for a table that does not fit or a screen
// you would rather fill.
const widthBtn = { read: document.getElementById("readbtn"),
                   mid: document.getElementById("midbtn"),
                   wide: document.getElementById("widebtn") };
let width = "read";
try {
  const w = localStorage.getItem("roost.width");
  if (w === "read" || w === "mid" || w === "wide") width = w;
  else if (localStorage.getItem("roost.wide") === "1") width = "wide";  // older
} catch (e) {}
function paintWidth() {
  document.body.classList.toggle("mid", width === "mid");
  document.body.classList.toggle("wide", width === "wide");
  for (const k in widthBtn) widthBtn[k].classList.toggle("on", k === width);
}
paintWidth();
function setWidth(w) {
  if (width === w) return;
  width = w;
  try { localStorage.setItem("roost.width", w); } catch (e) {}
  paintWidth();
}
for (const k in widthBtn) widthBtn[k].onclick = () => setWidth(k);

// Dragging the divider. Pointer events rather than mouse events so a touch
// drag works the same, and pointer capture so a fast drag that leaves the
// 5px handle keeps resizing instead of stopping dead.
const grip = document.getElementById("grip");
const TREE_MIN = 140, TREE_DEF = 272;      // 17rem, the width it opens at
function treeWidth(px) {
  const max = Math.max(TREE_MIN, window.innerWidth - 220);   // leave a document
  const w = Math.round(Math.min(max, Math.max(TREE_MIN, px)));
  document.documentElement.style.setProperty("--treew", w + "px");
  return w;
}
try {
  const w = parseInt(localStorage.getItem("roost.treew"), 10);
  if (w > 0) treeWidth(w);
} catch (e) {}
grip.addEventListener("pointerdown", (e) => {
  e.preventDefault();
  const x0 = e.clientX, w0 = treeEl.getBoundingClientRect().width;
  grip.setPointerCapture(e.pointerId);
  grip.classList.add("drag");
  document.body.classList.add("grabbing");
  let w = w0;
  const move = (ev) => { w = treeWidth(w0 + ev.clientX - x0); };
  const done = () => {
    grip.removeEventListener("pointermove", move);
    grip.removeEventListener("pointerup", done);
    grip.removeEventListener("pointercancel", done);
    grip.classList.remove("drag");
    document.body.classList.remove("grabbing");
    try { localStorage.setItem("roost.treew", String(w)); } catch (e) {}
  };
  grip.addEventListener("pointermove", move);
  grip.addEventListener("pointerup", done);
  grip.addEventListener("pointercancel", done);
});
// Double-click fits the column to the longest name on screen, the way an
// editor does — one gesture for the case the drag exists to solve.
grip.addEventListener("dblclick", () => {
  let want = 0;
  for (const n of treeEl.querySelectorAll(".n")) {
    const nm = n.querySelector(".nm");
    if (nm) want = Math.max(want, n.offsetLeft + nm.scrollWidth + 28);
  }
  const w = treeWidth(want || TREE_DEF);
  try { localStorage.setItem("roost.treew", String(w)); } catch (e) {}
});

// --- ordering ----------------------------------------------------------
// Newest first, by default, with folders and files in one sequence: when
// you are looking for what you were last working on, "is this a folder" is
// not the question you are asking. A folder's time is the newest thing
// anywhere inside it, which the server works out. The other order is the
// usual one for a tree: folders first, then A to Z.
// Code-point order, not locale order: localeCompare ignores leading
// punctuation, which put __pycache__ above .claude and disagreed with the
// order the server sends.
function cmp(a, b) { return a < b ? -1 : a > b ? 1 : 0; }

function sortEntries(list) {
  return list.slice().sort(byTime
    ? (a, b) => (b.mt || 0) - (a.mt || 0) ||
                cmp(a.name.toLowerCase(), b.name.toLowerCase())
    : (a, b) => (a.dir ? 0 : 1) - (b.dir ? 0 : 1) ||
                cmp(a.name.toLowerCase(), b.name.toLowerCase()));
}

// The folder listing is a table the server already rendered, so it is
// reordered in place rather than fetched again. The ".." row carries no
// timestamp and is left where it is.
function sortRows() {
  const tb = mainPane.querySelector("table tbody");
  if (!tb) return;
  const rows = Array.from(tb.querySelectorAll("tr[data-mt]"));
  if (rows.length < 2) return;
  const nm = (r) => (r.cells[1].textContent || "").toLowerCase();
  rows.sort(byTime
    ? (a, b) => (+b.dataset.mt) - (+a.dataset.mt) || cmp(nm(a), nm(b))
    : (a, b) => (+b.dataset.dir) - (+a.dataset.dir) || cmp(nm(a), nm(b)));
  for (const r of rows) tb.appendChild(r);
}

async function listDir(path) {
  const r = await fetch("api/tree?path=" + encodeURIComponent(path));
  if (!r.ok) throw new Error(r.status);
  return r.json();
}

// Extension -> (family, glyph). Anything unlisted gets the quiet default,
// which is most of a tree and should look like it.
const ICONS = {
  md: ["doc", "≡"], markdown: ["doc", "≡"], txt: ["doc", "≡"],
  rst: ["doc", "≡"], adoc: ["doc", "≡"], tex: ["doc", "≡"], pdf: ["doc", "≡"],
  json: ["data", "{}"], yaml: ["data", "{}"], yml: ["data", "{}"],
  toml: ["data", "{}"], csv: ["data", "▦"], tsv: ["data", "▦"],
  jsonl: ["data", "{}"], lock: ["data", "{}"], ini: ["data", "{}"],
  sh: ["sh", "$"], bash: ["sh", "$"], zsh: ["sh", "$"], fish: ["sh", "$"],
  py: ["code", "◇"], js: ["code", "◇"], ts: ["code", "◇"], tsx: ["code", "◇"],
  jsx: ["code", "◇"], c: ["code", "◇"], h: ["code", "◇"], cpp: ["code", "◇"],
  rs: ["code", "◇"], go: ["code", "◇"], java: ["code", "◇"],
  html: ["code", "◇"], css: ["code", "◇"], sql: ["code", "◇"],
  v: ["code", "◇"], sv: ["code", "◇"], vhd: ["code", "◇"],
  png: ["img", "▣"], jpg: ["img", "▣"], jpeg: ["img", "▣"], gif: ["img", "▣"],
  webp: ["img", "▣"], svg: ["img", "▣"], mp4: ["img", "▣"],
  pem: ["key", "⚿"], key: ["key", "⚿"], crt: ["key", "⚿"], cert: ["key", "⚿"],
  gz: ["arch", "▤"], zip: ["arch", "▤"], tar: ["arch", "▤"], xz: ["arch", "▤"],
};

function iconFor(e) {
  if (e.dir) return ["dir", "▬"];
  const dot = e.name.lastIndexOf(".");
  const ext = dot > 0 ? e.name.slice(dot + 1).toLowerCase() : "";
  return ICONS[ext] || ["none", "·"];
}

// --- what a row is ------------------------------------------------------
// A tree shows names; a path in this dashboard belongs to a session and to a
// repository, and neither is visible from a name. The box says all of it, on
// hover, without a click that would navigate away from what you are reading.
const tip = document.getElementById("tip");
let tipTimer = null, tipFor = null;

function when(mt) {
  if (!mt) return "unknown";
  const d = new Date(mt * 1000), p = (n) => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate())
       + " " + p(d.getHours()) + ":" + p(d.getMinutes());
}

// "2026-09-11 04:33" answers when; "(21 minutes ago)" answers whether it is
// still warm, which is usually the actual question.
function ago(mt) {
  if (!mt) return "";
  let s = Math.round(Date.now() / 1000 - mt);
  const ahead = s < 0;
  s = Math.abs(s);
  const say = (n, u) => n + " " + u + (n === 1 ? "" : "s");
  let t;
  if (s < 45) t = "moments";
  else if (s < 5400) t = say(Math.round(s / 60), "minute");      // under 90m
  else if (s < 129600) t = say(Math.round(s / 3600), "hour");    // under 36h
  else if (s < 2592000) t = say(Math.round(s / 86400), "day");   // under 30d
  else if (s < 31536000) t = say(Math.round(s / 2592000), "month");
  else t = say(Math.round(s / 31536000), "year");
  return ahead ? "in " + t : t + " ago";
}

function short(p) { return p.startsWith(HOMEDIR) ? "~" + p.slice(HOMEDIR.length) : p; }

// The path of `p` inside `base`, or "" when that says nothing new.
function under(p, base) {
  if (!base) return "";
  const b = base.replace(/[/]+$/, "");
  if (p === b || !p.startsWith(b + "/")) return "";
  return p.slice(b.length + 1);
}

function tipRow(label, value, dim) {
  if (!value) return "";
  return '<div><i>' + label + '</i><span>'
       + (dim ? '<em>' + esc(dim) + '</em> ' : "") + esc(value) + '</span></div>';
}

function esc(t) {
  return String(t).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function showTip(node, e, w) {
  tip.innerHTML =
      "<b>" + esc(short(e.path)) + "</b>"
    + tipRow("modified", when(e.mt) + (e.mt ? " (" + ago(e.mt) + ")" : ""))
    + tipRow("size", e.dir ? "" : sizeOf(e.size))
    + tipRow("session", w.sess ? (w.sess + " \u00b7 " + short(w.sroot))
                               : "not a session folder")
    // The path within each, worked out here rather than sent: the row knows
    // its own absolute path, and the folder's answer covers every row in it.
    + (w.sess ? tipRow("", under(e.path, w.sroot)) : "")
    + tipRow("git", w.git ? short(w.git) : "not in a repository")
    + tipRow("", under(e.path, w.git))
    + tipRow("branch", w.branch)
    + tipRow("worktree", w.wt === "linked"
        ? ("linked \u00b7 of " + short(w.main || "?"))
        : (w.wt === "main" ? "the repository's own" : ""))
    // Last, and asked for only when the box opens, for this one row: it
    // costs the server a few git invocations, which is fine once and
    // hopeless for a listing. Filled in when it lands, if the pointer has
    // not moved on.
    + '<div><i>state</i><span id="tipstate">checking\u2026</span></div>';
  tip.hidden = false;
  askState(node, e.path);
  const r = node.getBoundingClientRect(), t = tip.getBoundingClientRect();
  // Beside the row, and inside the window: on a narrow screen the box is
  // wider than the tree, and off the right edge is no use to anybody.
  let x = Math.min(r.right + 8, window.innerWidth - t.width - 8);
  let y = Math.min(r.top, window.innerHeight - t.height - 8);
  tip.style.left = Math.max(8, x) + "px";
  tip.style.top = Math.max(8, y) + "px";
}

const stateCache = new Map();

function setState(node, text) {
  if (tipFor !== node) return;            // the pointer has moved on
  const el = document.getElementById("tipstate");
  if (el) el.textContent = text;
}

function askState(node, path) {
  if (stateCache.has(path)) { setState(node, stateCache.get(path)); return; }
  fetch("api/gitstate?path=" + encodeURIComponent(path))
    .then((r) => (r.ok ? r.json() : null))
    .then((j) => {
      const t = (j && j.state) || "unknown";
      stateCache.set(path, t);
      setState(node, t);
    }, () => setState(node, "unknown"));
}

function hideTip() {
  clearTimeout(tipTimer);
  tipTimer = null; tipFor = null;
  tip.hidden = true;
}

function sizeOf(n) {
  if (!n) return "";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

function armTip(node, e, folder) {
  node.addEventListener("pointerenter", (ev) => {
    if (ev.pointerType === "touch") return;   // a finger has no hover
    clearTimeout(tipTimer);
    tipFor = node;
    tipTimer = setTimeout(() => {
      if (tipFor === node) showTip(node, e, e.w || folder);
    }, 280);
  });
  node.addEventListener("pointerleave", hideTip);
}

function nodeRow(e, depth, folder) {
  const n = document.createElement("div");
  n.className = "n" + (e.path === HERE ? " here" : "")
              + (e.name.startsWith(".") ? " hidden" : "");
  n.dataset.path = e.path;
  const tw = document.createElement("span");
  tw.className = "tw";
  tw.textContent = e.dir ? "▸" : "";        // a twisty only where it turns
  const [fam, glyph] = iconFor(e);
  const ic = document.createElement("span");
  ic.className = "ic ic-" + fam;
  ic.textContent = glyph;
  const nm = document.createElement("span");
  nm.className = "nm";
  nm.textContent = e.name;
  n.append(tw, ic, nm);
  armTip(n, e, folder || {});
  n.onclick = async (ev) => {
    hideTip();
    ev.stopPropagation();
    if (!e.dir) { loadDoc(e.path, true); return; }
    const box = n.nextElementSibling;
    if (box && box.classList.contains("kids")) {   // already open: fold it
      box.remove(); tw.textContent = "▸"; return;
    }
    tw.textContent = "▾";
    const kids = document.createElement("div");
    kids.className = "kids";
    n.after(kids);
    try {
      const d = await listDir(e.path);
      for (const c of sortEntries(d.entries))
        kids.appendChild(nodeRow(c, depth + 1, d.where));
    } catch (err) { kids.textContent = " (unreadable)"; }
  };
  return n;
}

// --- keeping the tree current --------------------------------------------
// The tree is fetched when it is opened and never again, which is fine for
// names and wrong for times: sorted newest-first, a folder these agents are
// writing into is out of date within a minute, and the file that was just
// written sits wherever it happened to be when the branch was drawn.
//
// So it is rebuilt on a timer, and on coming back to the tab. Rebuilt rather
// than patched, because a row and the box holding its children are siblings
// and reordering one without the other puts somebody else's files under a
// folder -- but the branches you had open are reopened and the scroll is put
// back, so it should read as nothing having happened.
let treeTouched = 0;
["pointerdown", "wheel", "scroll", "touchstart"].forEach((ev) =>
  treeEl.addEventListener(ev, () => { treeTouched = Date.now(); },
                          { passive: true }));

async function refreshTree() {
  if (!document.body.classList.contains("tree")) return;
  if (!treeEl.dataset.loaded) return;
  if (Date.now() - treeTouched < 4000) return;   // not under a moving finger
  const open = [...treeEl.querySelectorAll(".n")]
      .filter((n) => n.querySelector(".tw") &&
                     n.querySelector(".tw").textContent === "\u25be")
      .map((n) => n.dataset.path);
  const top = treeEl.scrollTop;
  treeEl.dataset.loaded = "";
  await openTo(hereDir);
  // Parents before children, or a child's row does not exist yet.
  for (const p of open.sort((a, b) => a.length - b.length)) {
    const row = [...treeEl.querySelectorAll(".n")]
        .find((n) => n.dataset.path === p);
    const tw = row && row.querySelector(".tw");
    if (tw && tw.textContent === "\u25b8") row.click();
  }
  treeEl.scrollTop = top;
}
setInterval(refreshTree, 30000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshTree();
});

// Open straight to the folder the document is in, with its ancestors
// expanded, so the tree arrives showing where you already are.
async function openTo(dir) {
  treeEl.dataset.loaded = "1";
  treeEl.innerHTML = "";
  let d;
  try { d = await listDir(dir); }
  catch (e) { treeEl.textContent = " (cannot read that folder)"; return; }
  rootSel.value = d.root || rootSel.value;
  const chain = [];
  let cur = d;
  while (cur.parent) {                      // walk up to the root
    chain.unshift(cur);
    try { cur = await listDir(cur.parent); } catch (e) { break; }
  }
  chain.unshift(cur);
  let host = treeEl;
  for (let i = 0; i < chain.length; i++) {
    const lvl = chain[i], next = chain[i + 1];
    let branch = null;
    for (const e of sortEntries(lvl.entries)) {
      const row = nodeRow(e, 0, lvl.where);
      host.appendChild(row);
      if (next && e.path === next.path) branch = row;   // came down here
    }
    // Descend only once this level is fully drawn. Switching hosts inside
    // the loop put every sibling that came AFTER the branch inside the
    // branch's own box: in a folder of 730 entries whose second row is the
    // one you came down, the 728 below it were rendered as that folder's
    // children, and the folder's real contents ended up beneath them --
    // which is why a file written a minute ago appeared at the bottom of
    // the tree with the sort working perfectly.
    if (!branch) break;
    branch.querySelector(".tw").textContent = "\u25be";
    const kids = document.createElement("div");
    kids.className = "kids";
    branch.after(kids);
    host = kids;
  }
  // Move the tree only. scrollIntoView walks up and scrolls whatever else
  // it finds on the way, which is how landing on a file near the bottom of
  // the tree arrived with the document already scrolled down.
  const here = treeEl.querySelector(".n.here");
  if (here) treeEl.scrollTop = Math.max(0, here.offsetTop - treeEl.clientHeight / 2);
}

rootSel.onchange = () => { treeEl.dataset.loaded = ""; openTo(rootSel.value); };

// --- swapping the document without reloading the page --------------------
// Following a file link used to reload everything: the tree was rebuilt,
// every folder you had opened folded again, and the scroll position went
// back to the top of a tree you had walked down. Only the document actually
// changes, so only the document is replaced.
const mainPane = document.querySelector("main");
const hdrName = document.querySelector("header .n");
const hdrSize = document.querySelector("header .s");
const hdrPath = document.querySelector("header .p");
const hdrRaw = document.querySelector('header a[href^="file?"]');
const extraStyle = document.createElement("style");
extraStyle.nonce = (document.currentScript || {}).nonce || "";
document.head.appendChild(extraStyle);

function markHere(path) {
  HERE = path;
  for (const n of treeEl.querySelectorAll(".n.here")) n.classList.remove("here");
  const hit = treeEl.querySelector('.n[data-path="' + CSS.escape(path) + '"]');
  if (hit) hit.classList.add("here");
}

async function loadDoc(path, push, raw) {
  let d;
  try {
    const r = await fetch("api/doc?path=" + encodeURIComponent(path)
                          + (raw ? "&raw=1" : ""));
    d = await r.json();
  } catch (e) {
    window.location = "file?path=" + encodeURIComponent(path);   // let the
    return;                                                      // browser try
  }
  if (d.kind === "raw") { window.location = d.url; return; }     // an image
  if (d.error || !d.body) {
    window.location = "file?path=" + encodeURIComponent(path);
    return;
  }
  extraStyle.textContent = d.extra || "";
  mainPane.innerHTML = d.body;
  hdrName.textContent = d.name;
  hdrSize.textContent = d.size;
  hdrPath.innerHTML = d.crumbs;          // built and escaped by the server
  hdrRaw.href = d.raw;
  hdrRaw.textContent = d.rawlabel;
  document.title = d.name;
  mainPane.scrollTop = 0;                // a document opens at its beginning
  typeset(mainPane);
  paint(mainPane);
  sortRows();                            // a folder arrives in name order
  hereDir = d.dir || hereDir;
  markHere(d.path);
  if (push !== false) {
    history.pushState({ path: d.path, raw: !!raw }, "",
                      "file?path=" + encodeURIComponent(d.path)
                      + (raw ? "&view=source" : ""));
  }
}

// A link to something on this machine swaps the pane; raw, and anything
// else, is left to the browser.
function interceptLinks(scope) {
  scope.addEventListener("click", (e) => {
    const a = e.target.closest && e.target.closest("a");
    if (!a || e.metaKey || e.ctrlKey || e.shiftKey || a.target === "_blank") return;
    const href = a.getAttribute("href") || "";
    // "raw" swaps the source into this page rather than opening a bare
    // text/plain document somewhere else: same tree, same place in it, and
    // the link becomes the way back.
    const RAW = "file?raw=1&path=";
    if (href.startsWith(RAW)) {
      e.preventDefault();
      loadDoc(decodeURIComponent(href.slice(RAW.length)), true, true);
      return;
    }
    if (!href.startsWith("file?path=")) return;
    e.preventDefault();
    loadDoc(decodeURIComponent(href.slice("file?path=".length)), true);
  });
}
interceptLinks(mainPane);
interceptLinks(document.querySelector("header"));

if (new URL(window.location).searchParams.get("view") === "source") {
  loadDoc(HERE, false, true);
}

window.addEventListener("popstate", (e) => {
  const u = new URL(window.location);
  const path = (e.state && e.state.path) || u.searchParams.get("path");
  const raw = (e.state && e.state.raw) || u.searchParams.get("view") === "source";
  if (path) loadDoc(path, false, raw);
});


// One handler for every copy button on the page, wherever the markup came
// from: the button carries the link, so nothing has to be looked up. Plain
// click copies the address you would send someone; shift-click copies the
// path or URL as it is written, which is what you want for a local file.
document.addEventListener("click", async (e) => {
  const b = e.target.closest && e.target.closest("button.cp");
  if (!b) return;
  e.preventDefault();
  e.stopPropagation();
  let text = b.dataset.p || "";
  if (!e.shiftKey) {
    try { text = new URL(b.dataset.u, document.baseURI).href; }
    catch (err) { text = b.dataset.u || text; }
  }
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; }
  catch (err) {
    // No clipboard permission, or an insecure context: the old way.
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { ok = document.execCommand("copy"); } catch (e2) {}
    ta.remove();
  }
  b.classList.toggle("done", ok);
  b.textContent = ok ? "\u2713" : "\u2717";
  setTimeout(() => { b.classList.remove("done"); b.textContent = "\u29c9"; }, 1200);
}, true);

// Typeset the maths the renderer set aside. Deliberately after the fact and
// deliberately optional: the server sends the LaTeX source, KaTeX turns it
// into type if it loaded, and if the CDN is unreachable — no internet on the
// tailnet, say — the source stays on screen exactly as it was, readable.
function typeset(root) {
  if (!window.katex) {
    // Say so rather than leaving raw LaTeX in the prose looking like a
    // rendering bug: this used to fail silently whenever the library did
    // not arrive, and "maths does not work" was indistinguishable from
    // "maths was never attempted".
    for (const el of (root || document).querySelectorAll(".math, .imath")) {
      el.classList.add("noks");
      el.title = "KaTeX did not load — showing the LaTeX source";
    }
    return;
  }
  const els = (root || document).querySelectorAll(
    ".math:not([data-tex]), .imath:not([data-tex])");
  for (const el of els) {
    el.dataset.tex = "1";
    let tex = el.textContent.trim();
    const display = el.classList.contains("math");
    // B is one backslash, written as an escape on purpose. These templates
    // are Python strings: a backslash-bracket pair written literally here
    // loses a level on the way out, and JavaScript then reads what is left
    // as a plain bracket. So the delimiter test never matched, nothing was
    // stripped, and KaTeX was handed the delimiters as part of the formula.
    // It rendered that as a parse error -- in red, which on screen looks
    // exactly like maths that was never typeset at all.
    const B = "\\u005c";
    for (const [open, close] of [[B + "[", B + "]"], ["$$", "$$"],
                                 [B + "(", B + ")"]]) {
      if (tex.startsWith(open) && tex.endsWith(close)) {
        tex = tex.slice(open.length, -close.length).trim();
        break;
      }
    }
    try {
      katex.render(tex, el, { displayMode: display, throwOnError: false,
                              output: "html" });
    } catch (e) {
      el.dataset.tex = "err";        // leave the source visible, untouched
    }
  }
}

// Colour the code once the highlighter has arrived. Optional in the same
// way the maths is: without it the source is still there, in a monospace
// font, perfectly readable — just grey.
// highlight.js ships no Lean, and Lean is what many documents here are
// written around. A small grammar of its own: comments (nested, the way Lean
// nests them), strings, the words that open a declaration, and the tactics
// that fill a proof.
function registerLean() {
  if (!window.hljs || hljs.getLanguage("lean")) return;
  hljs.registerLanguage("lean", (hljs) => ({
    name: "Lean",
    keywords: {
      keyword:
        "import export open namespace section end variable variables universe "
        + "noncomputable private protected partial unsafe mutual macro macro_rules "
        + "syntax notation infix infixl infixr prefix postfix set_option attribute "
        + "def abbrev theorem lemma example instance class structure inductive "
        + "deriving extends where with fun let rec have show from suffices calc "
        + "match do if then else return try catch for in at by axiom opaque "
        + "local scoped",
      built_in:
        "Type Sort Prop Nat Int Rat Real Complex Bool Char String List Array "
        + "Option Except Id IO Unit Empty Sigma Subtype Set Finset Matrix Fin "
        + "Function Decidable",
      literal: "true false none some"
    },
    contains: [
      hljs.COMMENT("/-", "-/", { contains: ["self"] }),
      hljs.COMMENT("--", "$"),
      { className: "string", begin: /"/, end: /"/, contains: [{ begin: /\\./ }] },
      { className: "meta", begin: /@\\[/, end: /\\]/ },
      { className: "symbol", begin: /`+[A-Za-z_][A-Za-z0-9_.']*/ },
      { className: "built_in", begin: TACTICS },
      hljs.C_NUMBER_MODE
    ]
  }));
}
// The tactics, as one alternation: a proof is mostly these, and reading it
// is mostly telling them from the terms they act on.
const TACTICS = new RegExp("\\\\b(" + [
  "simp", "simpa", "simp_all", "rw", "rwa", "rfl", "exact", "apply", "intro",
  "intros", "refine", "constructor", "cases", "rcases", "rintro", "obtain",
  "induction", "omega", "decide", "native_decide", "linarith", "nlinarith",
  "positivity", "polyrith", "ring", "ring_nf", "field_simp", "norm_num",
  "norm_cast", "push_cast", "gcongr", "aesop", "tauto", "trivial",
  "contradiction", "assumption", "use", "ext", "funext", "congr", "subst",
  "unfold", "dsimp", "change", "specialize", "generalize", "conv", "sorry",
  "admit", "all_goals", "any_goals", "repeat", "first"
].join("|") + ")\\\\b");

function paint(root) {
  if (!window.hljs) return;
  registerLean();
  for (const el of (root || document).querySelectorAll(
         "pre.src code:not([data-hl]), pre.code:not([data-hl])")) {
    el.dataset.hl = "1";
    try { hljs.highlightElement(el); } catch (e) { /* leave it plain */ }
  }
}

window.addEventListener("load", () => {
  typeset(document); paint(document);
  sortRows();          // a listing the server rendered arrives in name order
});
</script>
</body></html>
""".replace("__HLJS__", HLJS_TAGS).replace("__KATEX__", KATEX_TAGS) \
     .replace("__INTER__", INTER_CSS).replace("__FILESICON__", files_icon(16)) \
     .replace("__HOMELINK__", home_link(18))

_JSON_TOK = _re.compile(
    r'("(?:[^"\\]|\\.)*")\s*:|("(?:[^"\\]|\\.)*")|\b(true|false|null)\b'
    r'|(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)')

def _json_cell(v):
    """One value in a table. Containers are shown as a size, not inlined —
    an evidence file has arrays with thousands of entries in it."""
    if isinstance(v, bool) or v is None:
        return '<span class="lit">%s</span>' % ("null" if v is None else str(v).lower()), False
    if isinstance(v, (int, float)):
        return _html.escape("{:,}".format(v) if isinstance(v, int) else repr(v)), True
    if isinstance(v, list):
        return '<span class="muted">[%d items]</span>' % len(v), False
    if isinstance(v, dict):
        return '<span class="muted">{%d keys}</span>' % len(v), False
    t = str(v)
    return _html.escape(t if len(t) <= 300 else t[:300] + " …"), False

def _kv_table(d):
    rows = []
    for k, v in list(d.items())[:400]:
        cell, num = _json_cell(v)
        rows.append('<tr><th class="key">%s</th><td%s>%s</td></tr>'
                    % (_html.escape(str(k)), ' class="num"' if num else "", cell))
    return "<table><tbody>%s</tbody></table>" % "".join(rows)

def _rows_table(items):
    cols, seen = [], set()
    for r in items[:500]:                       # column order as first seen
        for k in r:
            if k not in seen and len(cols) < 40:
                seen.add(k); cols.append(k)
    LIMIT = 2000
    body = []
    for r in items[:LIMIT]:
        tds = []
        for c in cols:
            cell, num = _json_cell(r.get(c)) if c in r else ("", False)
            tds.append("<td%s>%s</td>" % (' class="num"' if num else "", cell))
        body.append("<tr>%s</tr>" % "".join(tds))
    extra = ("<p class=\"note\">%d more rows not shown</p>" % (len(items) - LIMIT)
             if len(items) > LIMIT else "")
    return ("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>%s"
            % ("".join("<th>%s</th>" % _html.escape(str(c)) for c in cols),
               "".join(body), extra))

def _json_tables(obj):
    """Tables when the shape is tabular, else None.

    Evidence JSON is almost always one of two shapes: a record — scalars
    with a nested block or two — or a list of records. Both are a table.
    Anything else falls through to the pretty-printed form, which is still
    better than a wall of text."""
    if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj[:2000]):
        return _rows_table(obj)
    if not isinstance(obj, dict) or not obj:
        return None
    parts, flat = [], {k: v for k, v in obj.items()
                       if not isinstance(v, (dict, list))}
    if flat:
        parts.append(_kv_table(flat))
    for k, v in obj.items():
        if isinstance(v, dict) and v:
            parts.append("<h2>%s</h2>%s" % (_html.escape(str(k)), _kv_table(v)))
        elif isinstance(v, list) and v:
            if all(isinstance(x, dict) for x in v[:200]):
                parts.append("<h2>%s <span class=\"muted\">%d</span></h2>%s"
                             % (_html.escape(str(k)), len(v), _rows_table(v)))
            else:
                head = ", ".join(_html.escape(str(x))[:40] for x in v[:8])
                parts.append('<h2>%s <span class="muted">%d items</span></h2>'
                             '<pre>%s%s</pre>'
                             % (_html.escape(str(k)), len(v), head,
                                " …" if len(v) > 8 else ""))
    return "".join(parts) if parts else None

def _json_view(text):
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        return None                      # not JSON after all: fall through
    try:
        tables = _json_tables(obj)
    except RecursionError:
        return None
    if tables:
        return tables
    # Not a shape that tabulates: pretty-print and colour it instead.
    esc = _html.escape(json.dumps(obj, indent=2, ensure_ascii=False), quote=False)
    def paint(m):
        if m.group(1): return '<span class="k">%s</span>:' % m.group(1)
        if m.group(2): return '<span class="str">%s</span>' % m.group(2)
        if m.group(3): return '<span class="lit">%s</span>' % m.group(3)
        return '<span class="n">%s</span>' % m.group(4)
    return "<pre>%s</pre>" % _JSON_TOK.sub(paint, esc)

def _csv_view(text, sep=None):
    import csv, io
    sample = text[:8192]
    if sep is None:
        try:
            sep = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            sep = "\t" if "\t" in sample.split("\n")[0] else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=sep))
    if not rows:
        return None
    LIMIT = 2000
    shown, extra = rows[:LIMIT], max(0, len(rows) - LIMIT)
    head, body = shown[0], shown[1:]
    def cell(v, tag):
        num = ""
        try:
            float(v.replace(",", ""))
            num = ' class="num"'
        except ValueError:
            pass
        return "<%s%s>%s</%s>" % (tag, num if tag == "td" else "",
                                  _html.escape(v), tag)
    out = ["<table><thead><tr>",
           "".join(cell(c, "th") for c in head), "</tr></thead><tbody>"]
    for r in body:
        out.append("<tr>%s</tr>" % "".join(cell(c, "td") for c in r))
    out.append("</tbody></table>")
    if extra:
        out.append('<p class="note">%d more rows not shown</p>' % extra)
    return "".join(out)


# Extension -> highlight.js language. Only where the name differs from the
# extension or the guess is unreliable; anything unlisted is left to
# highlight.js to detect, and anything it cannot detect stays plain.
_HL_LANG = {
    "py": "python", "js": "javascript", "mjs": "javascript", "ts": "typescript",
    "tsx": "typescript", "jsx": "javascript", "sh": "bash", "bash": "bash",
    "zsh": "bash", "rs": "rust", "go": "go", "c": "c", "h": "c",
    "cpp": "cpp", "hpp": "cpp", "cc": "cpp", "java": "java", "rb": "ruby",
    "php": "php", "pl": "perl", "lua": "lua", "sql": "sql", "html": "xml",
    "xml": "xml", "svg": "xml", "css": "css", "scss": "scss",
    "yaml": "yaml", "yml": "yaml", "toml": "ini", "ini": "ini", "cfg": "ini",
    "make": "makefile", "mk": "makefile", "dockerfile": "dockerfile",
    "tex": "latex", "v": "verilog", "sv": "verilog", "vhd": "vhdl",
    "diff": "diff", "patch": "diff", "log": "accesslog",
    "json": "json", "jsonl": "json", "ndjson": "json", "geojson": "json",
    "lean": "lean", "md": "markdown", "txt": "", "text": "",
}

_SRC_CSS = """
  pre.src { margin: 0; white-space: pre; overflow-x: auto;
            font: 13px/1.5 var(--c-mono); tab-size: 4; }
  pre.src code { display: block; padding: .2rem 0; background: none; }
  pre.src code.hljs { background: none; padding: .2rem 0; }
"""

def file_view(f, data, size=None, partial=False):
    """(html, extra_css) for a file we can render, or None to serve as text."""
    ext = f.suffix.lower()
    if partial:
        # Only the head was read. Parsing half a document is not possible, so
        # show it as text and say plainly what is missing.
        return ('<p class="note">Showing the first %s of %s. '
                'The whole file is too large to render.</p><pre>%s</pre>'
                % (_size_of(len(data)), _size_of(size or 0),
                   _html.escape(data.decode("utf-8", "replace")))), ""
    if len(data) > FILE_VIEW_MAX:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if ext in (".html", ".htm"):
        # A page, shown as a page. It renders inside a frame rather than in
        # this document: the frame's own response disowns it (CSP sandbox,
        # opaque origin, nothing but inline script and a webfont), so a
        # generated report reads as itself without any of it running here.
        return ('<iframe class="page" src="file?raw=1&amp;path=%s" '
                'title="%s"></iframe>'
                % (_url_q(str(f)), _html.escape(f.name, quote=True))), _HTML_VIEW_CSS
    if ext in (".md", ".markdown"):
        return md_html(text, str(f.parent)), _MD_VIEW_CSS
    if ext == ".json":
        return _json_view(text), ""
    if ext in (".csv", ".tsv"):
        return _csv_view(text, "\t" if ext == ".tsv" else None), ""
    # Everything else that is text at all: a source file was being served as
    # text/plain, which the browser shows bare -- no page, and so no tree
    # beside it. It is a document like any other; it just wants a monospace
    # font and colours.
    lang = _HL_LANG.get(ext.lstrip("."), "")
    return ('<pre class="src"><code class="%s">%s</code></pre>'
            % ("language-" + lang if lang else "nohighlight",
               _html.escape(text))), _SRC_CSS

# Matched without the closing ">" because some carry attributes:
# <codex_internal_context source="goal">.
_CODEX_NOISE = ("<environment_context", "<user_instructions",
                "<skills_instructions", "<mcp_instructions",
                "<codex_internal_context")
# And the family in general, so the next wrapper codex invents is machinery
# from the start rather than after someone spots it in a transcript.
_CODEX_TAG = _re.compile(r"^<([a-z0-9_]+)[\s>]")
# Resuming a session re-injects the folder's AGENTS.md as a user turn, under a
# markdown heading rather than a tag -- so none of the tests above saw it, and
# a wall of workspace policy appeared in the conversation as though somebody
# had typed it. The heading is the tell, and so is the INSTRUCTIONS block it
# opens with.
_CODEX_ENV_HEAD = _re.compile(
    r"^#+\s*AGENTS\.md\b|^#+\s*instructions for\b|^<INSTRUCTIONS>", _re.I)

# Claude Code's own injections arrive as user turns: a task finishing in the
# background, a reminder the harness adds. Nobody typed them, and a wall of
# <task-notification> XML in a transcript is exactly the machinery the
# reading view exists to keep out.
_INJECTED_NAMES = ("system-reminder", "task-notification", "local-command-stdout",
                   "local-command-stderr", "command-name", "command-message",
                   "command-args", "user-prompt-submit-hook")
_INJECTED_START = _re.compile(r"^<(%s)\b" % "|".join(_INJECTED_NAMES), _re.I)
# The same blocks also turn up appended to something a person did write, and
# there they are noise inside the message rather than a message of their own.
_INJECTED_BLOCK = _re.compile(
    r"<(%s)\b[\s\S]*?</\1>\s*" % "|".join(_INJECTED_NAMES), _re.I)

def strip_injected(text):
    """Remove harness blocks from a message, leaving what was actually said."""
    return _INJECTED_BLOCK.sub("", text or "").strip()

_CODEX_INTENT = ("i'll", "i\u2019ll", "i will", "let me", "next,", "now i",
                 "going to", "starting", "i'm going", "i am going", "first,")

def _codex_kind(role, body):
    """ask = a real question · say = assistant · env/tool/out = machinery."""
    if role == "user":
        b = (body or "").lstrip()
        if _INJECTED_START.match(b):
            return "env"                 # a whole turn that nobody typed
        if b.startswith(_CODEX_NOISE):
            return "env"
        m = _CODEX_TAG.match(b)
        if m and (m.group(1).startswith("codex_")
                  or m.group(1).endswith(("_context", "_instructions"))):
            return "env"
        if _CODEX_ENV_HEAD.match(b) or b[:400].find("<INSTRUCTIONS>") >= 0:
            return "env"
        return "ask"
    if role == "assistant":
        return "say"
    if role == "thinking":
        return "think"
    if role == "compacted":
        return "mark"                    # where the thread was cut, not content
    return "out" if role == "output" else "tool"

def _codex_essential(kinds, rows, i):
    """The thread of the conversation: what was asked, and what was concluded.

    A question always counts. For an assistant message the useful signal is
    structural: the one that ends a turn — nothing but tool traffic between it
    and the next question — is where codex says what it did and what came of
    it. The narration in the middle ("I'll read the handoff first") only
    counts when it is long and structured enough to be a summary in its own
    right, which is how a phase write-up mid-turn gets through."""
    if kinds[i] == "mark":
        return True                      # a cut in the thread is the thread
    if kinds[i] == "ask":
        return True
    if kinds[i] != "say":
        return False
    for j in range(i + 1, len(kinds)):
        if kinds[j] == "ask":
            return True                    # ends the turn
        if kinds[j] == "say":
            body = (rows[i][1] or "").strip()
            # Narration announces an intention; a conclusion reports a fact.
            # That distinction separates "I'll read the handoff first" from
            # "The integration suite passed: 1,204 checks with zero failures"
            # better than length alone does.
            if body[:40].lower().startswith(_CODEX_INTENT):
                return False
            return (len(body) >= 400
                    or any(m in body for m in ("\n- ", "\n## ", "\n**",
                                               "\n1. ")))
    return True                            # the newest thing said



# --------------------------------------------------- Claude Code transcripts
# Same shape as a codex rollout -- append-only JSONL -- so the cache and the
# renderer are shared. The entries are not the same, though: Claude keeps
# thinking blocks and pasted images alongside the conversation, and a great
# deal of bookkeeping (mode, ai-title, bridge-session, file-history-*) that
# is nobody's transcript. A live session is found by its record rather than
# by scanning: ~/.claude/sessions/<pid>.json gives the id and the cwd, and
# the file is <munged cwd>/<id>.jsonl.

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CLAUDE_SESSIONS = Path.home() / ".claude" / "sessions"

def claude_path(sid):
    """The transcript for a session id, or None. The id is from a query
    string, so it is constrained before it touches the filesystem."""
    if not sid or not all(c.isalnum() or c in "-_" for c in sid):
        return None
    try:
        for f in CLAUDE_PROJECTS.glob("*/" + sid + ".jsonl"):
            return f
    except OSError:
        pass
    return None

def claude_files(limit=40):
    """Live Claude sessions, newest transcript first, in codex_files' shape."""
    out = []
    try:
        recs = list(CLAUDE_SESSIONS.glob("*.json"))
    except OSError:
        return []
    for r in recs:
        d = session_record(r)
        if d is None:
            continue
        sid, cwd = d.get("sessionId"), d.get("cwd")
        f = claude_path(sid) if sid else None
        if not f:
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        out.append({"file": "claude:" + sid, "cwd": cwd or "",
                    "mb": round(st.st_size / 1e6, 1), "mtime": st.st_mtime,
                    "name": (d.get("tmux") or "").split(":")[0],
                    "when": time.strftime("%m-%d %H:%M",
                                          time.localtime(st.st_mtime))})
    # One record per PID, and a session outlives its PIDs, so the same
    # transcript arrives several times. Keep the one that knows its tmux name.
    best = {}
    for e in out:
        cur = best.get(e["file"])
        if cur is None or (not cur["name"] and e["name"]):
            best[e["file"]] = e
    out = sorted(best.values(), key=lambda x: x["mtime"], reverse=True)
    return out[:limit]




# ------------------------------------------------------------------- slots
# A clipboard with named slots, rather than an address book.
#
# A session dumps its last answer into a slot addressed to another session --
# "for @app2 at 12:55" -- and that slot sits in the source's top bar until
# someone clicks it. Nothing is sent by dumping. This is deliberately not the
# earlier design: there, any bot that found ccmsg could enumerate the whole
# fleet and message any of it. Here a session cannot discover anybody; it can
# only fill a slot that a person addressed, and a person sends it.
#
# A route can be made sticky, and then dumps to that target go straight out.
# That is the only automatic path, it is per source-and-target pair, and it
# is off until switched on.

SLOTS_FILE = Path.home() / ".roost" / "slots.json"

def _slots_read():
    try:
        d = _jobj(SLOTS_FILE.read_text()) or {}
    except OSError:
        d = {}
    if not isinstance(d.get("slots"), list):
        d["slots"] = []
    d["slots"] = [x for x in d["slots"] if isinstance(x, dict)
                  and all(isinstance(x.get(k), str) for k in ("id", "to", "from", "text"))]
    if not isinstance(d.get("auto"), dict):
        d["auto"] = {}             # "@from>@to" -> True
    if not isinstance(d.get("reply"), dict):
        d.pop("reply", None)
    return d

def _slots_write(d):
    try:
        SLOTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_private(SLOTS_FILE, json.dumps(d, indent=2))
        return True
    except OSError:
        return False

# A slot has two halves. The BINDING -- owner, name, target -- is set by a
# person and is the only place a destination appears. The CONTENT is what a
# session dumped into it. A bot names the slot; it never names, and never
# learns, the session on the other end.

def slot_defs(owner=None):
    d = _slots_read()
    defs = d.setdefault("defs", [])
    o = owner.lstrip("@") if owner else None
    return [x for x in defs if o is None or x["owner"] == o]

@_locked(_SLOTS_LOCK)
def slot_bind(owner, name, to):
    """Point a named slot at a session. A person does this, not a bot."""
    hit, err = ccmsg.resolve(to.lstrip("@"))
    if not hit:
        return False, "target: " + err
    d = _slots_read()
    defs = d.setdefault("defs", [])
    o = owner.lstrip("@")
    for x in defs:
        if x["owner"] == o and x["name"] == name:
            x["to"] = hit[1]
            break
    else:
        defs.append({"owner": o, "name": name, "to": hit[1]})
    _slots_write(d)
    return True, "%s: slot %r -> @%s" % (owner, name, hit[1])

@_locked(_SLOTS_LOCK)
def _reply_set(owner, to):
    """Owe one answer back to `to`. One, not a standing arrangement."""
    d = _slots_read()
    d.setdefault("reply", {})[owner.lstrip("@")] = {"to": to.lstrip("@"),
                                                    "ts": time.time()}
    _slots_write(d)

def reply_target(owner):
    """Who this session owes an answer to, if it owes one."""
    r = _slots_read().get("reply", {}).get(owner.lstrip("@"))
    if isinstance(r, dict):
        return r.get("to", "")
    return r or ""                      # tolerate the older shape

@_locked(_SLOTS_LOCK)
def _reply_clear(owner):
    d = _slots_read()
    if d.get("reply", {}).pop(owner.lstrip("@"), None) is not None:
        _slots_write(d)

def slot_target(owner, name):
    for x in slot_defs(owner):
        if x["name"] == name:
            return x["to"]
    return ""

def slot_route(frm, to):
    return "%s>%s" % (frm.lstrip("@"), to.lstrip("@"))

def slots_for(owner):
    """Slots this session has filled and not yet sent."""
    o = owner.lstrip("@")
    d = _slots_read()
    return [x for x in d["slots"] if x["from"] == o and not x.get("sent")]

_AUTO_LAST = {}                 # route -> when it last sent by itself

@_locked(_SLOTS_LOCK)
def slot_dump(frm, slot, to=""):
    """Capture the source's last answer into one of its named slots.

    `slot` is what a bot supplies -- a name it was given, like "for-review".
    `to` is only ever passed by a person, from the dashboard, and binds the
    slot as a side effect. Sends nothing by itself, unless the route is
    sticky: the one case a person agreed to in advance."""
    hit, err = ccmsg.resolve(frm.lstrip("@"))
    if not hit:
        return None, "source: " + err
    owed = reply_target(hit[1])
    dest = to or slot_target(hit[1], slot) or owed
    used_reply = not to and not slot_target(hit[1], slot) and bool(owed)
    if not dest:
        return None, ("slot %r is not pointed anywhere yet — bind it from the "
                      "dashboard" % slot)
    dst, derr = ccmsg.resolve(dest.lstrip("@"))
    if not dst:
        return None, "target: " + derr
    if to:
        slot_bind(hit[1], slot, dst[1])
    if used_reply or (to and owed and dst[1] == owed):
        _reply_clear(hit[1])            # answered; the debt is discharged
    text = ccmsg.last_said(hit[1], hit[0])
    if not text:
        return None, "@%s has not said anything to hand over" % hit[1]
    d = _slots_read()
    rec = {"id": "%d" % (time.time() * 1000), "from": hit[1], "to": dst[1],
           "slot": slot, "at": time.strftime("%H:%M"), "ts": time.time(),
           "chars": len(text), "text": text, "sent": False}
    d["slots"] = (d["slots"] + [rec])[-200:]       # a clipboard, not an archive
    _slots_write(d)
    route = slot_route(hit[1], dst[1])
    if d["auto"].get(route):
        # Automatic sending is for answers to a person. An answer to
        # something that was itself forwarded is held for a click -- two
        # sticky routes pointing at each other would otherwise loop for as
        # long as both sessions keep answering -- and no route sends more
        # than once a minute on its own.
        if ccmsg.arrived_by_forward(hit[1], hit[0]):
            return rec, "held for @%s (an answer to a forward is not auto-sent)" % dst[1]
        now = time.time()
        if now - _AUTO_LAST.get(route, 0) < 60:
            return rec, "held for @%s (auto-send is limited to once a minute)" % dst[1]
        _AUTO_LAST[route] = now
        ok, detail = slot_send(rec["id"])
        return rec, ("auto-sent to @%s" % dst[1]) if ok else detail
    return rec, "held for @%s" % dst[1]

@_locked(_SLOTS_LOCK)
def slot_send(slot_id):
    d = _slots_read()
    for x in d["slots"]:
        if x["id"] != slot_id:
            continue
        if x.get("sent"):
            return False, "already sent"
        ok, detail = ccmsg.send(x["to"], ccmsg.FORWARD_MARK + "%s] %s"
                                % (x["from"], x["text"]))
        if ok:
            x["sent"] = True
            _slots_write(d)
            # The answer goes back where the work came from -- for THIS
            # answer, and then the arrangement is over. A standing binding
            # would quietly turn one handover into a permanent pipe out of
            # that session; making a route permanent is a separate act, and a
            # visible one (the purple chip).
            _reply_set(x["to"], x["from"])
        return ok, detail
    return False, "no such slot"

@_locked(_SLOTS_LOCK)
def slot_drop(slot_id):
    d = _slots_read()
    n = len(d["slots"])
    d["slots"] = [x for x in d["slots"] if x["id"] != slot_id]
    _slots_write(d)
    return len(d["slots"]) < n

@_locked(_SLOTS_LOCK)
def slot_auto(frm, to, on):
    d = _slots_read()
    k = slot_route(frm, to)
    if on:
        d["auto"][k] = True
    else:
        d["auto"].pop(k, None)
    _slots_write(d)
    return bool(d["auto"].get(k))

def claude_term_name(tmux_name):
    """The terminal name for a Claude session: its card's label. Codex
    terminals already own the bare names -- there is a codex terminal called
    fw and a Claude session called fw -- and the label is the name you
    see on the card anyway."""
    return tmux_name + SUFFIX

def ensure_claude_term(tmux_name):
    """Define a ttyd terminal for a Claude session, once.

    It attaches to a GROUPED session rather than the session itself, with
    window-size largest, so a phone joining cannot shrink the window under a
    desktop client already watching it. tmux stays the supervisor: dtach has
    no way to read a screen -- literally no capture code in it -- and the
    dashboard's snippets and status come from capture-pane.
    """
    name = claude_term_name(tmux_name)
    if not all(c.isalnum() or c in "-_" for c in name):
        return ""
    if is_dtach(tmux_name):
        return name            # its .cmd is the session itself, not an attach
    d = Path.home() / ".dtach"
    try:
        d.mkdir(exist_ok=True)
        f = d / (name + ".cmd")
        # mouse on, and only on this grouped session: tmux is always on the
        # alternate screen, where xterm turns every wheel tick into a cursor
        # key -- which is what sprayed ^[[B^[[B across the pane instead of
        # scrolling. With mouse reporting on, tmux takes the wheel itself and
        # scrolls its own history. Scoped to the -web session so a local
        # client's selection behaviour is untouched.
        cmd = ("export ROOST_NAME={t}; "
               "tmux new-session -A -s {n}-web -t {n} \\; "
               "set-option window-size largest \\; "
               "set-option mouse on").format(n=tmux_name, t=name)
        if not f.exists() or f.read_text().strip() != cmd:
            write_private(f, cmd)
    except OSError:
        return ""
    return name

def claude_sid_by_tmux():
    """tmux session name -> transcript id, for the live Claude sessions.
    The two halves of a session -- its pane and its transcript -- are found
    by different keys, and this is the join."""
    out = {}
    for f in claude_files(60):
        if f["name"]:
            out.setdefault(f["name"], f["file"])
    return out

def _claude_full(o, rows):
    """Every entry, tool calls and thinking included, for the view that asks
    for them. The conversation-only reader above drops these."""
    t = o.get("type")
    if t not in ("user", "assistant"):
        return
    msg = o.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        rows.append(("user", strip_injected(content) or content))
        return
    for b in content or []:
        bt = b.get("type")
        if bt == "text":
            raw = b.get("text") or ""
            rows.append((t, strip_injected(raw) if t == "user" else raw))
        elif bt == "thinking":
            rows.append(("thinking", b.get("thinking") or ""))
        elif bt == "image":
            rows.append((t, "[image]"))
        elif bt == "tool_use":
            rows.append(("tool " + (b.get("name") or ""),
                         json.dumps(b.get("input") or {}, indent=2)[:20000]))
        elif bt == "tool_result":
            rows.append(("output", _text_of(b.get("content"))))

def _claude_rows(o, rows, hidden):
    """One transcript line -> conversation rows. Returns the hidden count."""
    t = o.get("type")
    if t not in ("user", "assistant"):
        return hidden                     # bookkeeping, not conversation
    msg = o.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        rows.append(("user", strip_injected(content) or content))
        return hidden
    for b in content or []:
        bt = b.get("type")
        if bt == "text":
            raw = b.get("text") or ""
            rows.append((t, strip_injected(raw) if t == "user" else raw))
        elif bt == "thinking":
            rows.append(("thinking", b.get("thinking") or ""))
        elif bt == "image":
            rows.append((t, "[image]"))   # base64; not worth carrying here
        else:
            hidden += 1                   # tool_use / tool_result
    return hidden

# --------------------------------------------------------- rollout cache
# A rollout is append-only JSONL, and the machinery in it dwarfs the
# conversation: t42 is 200MB of which 127 entries are things anyone said.
# Re-reading that on every 5s poll cost ~200ms of CPU per open page, so the
# conversation is kept in memory and only the newly appended bytes are
# parsed. Tool bodies are NOT kept — they are the 200MB — so asking for them
# still re-reads the file, which is the rare case.

_ROLL = {}
_ROLL_CHUNK = 4 << 20
FULL_READ_MAX = 48 << 20     # past this the everything view reads the tail
# One parse at a time per file. Without this, the count poll and the page
# fetch both start a full read of the same transcript, and a second viewer
# starts two more -- four passes over a 71MB file at once, each holding every
# message it has parsed so far. That is how the server reached 3.5GB and
# stopped answering. The lock makes the others wait for the first one's
# result instead of duplicating it.
import threading as _threading
_ROLL_LOCKS = {}
_ROLL_LOCKS_GUARD = _threading.Lock()

def _roll_lock(key):
    with _ROLL_LOCKS_GUARD:
        lk = _ROLL_LOCKS.get(key)
        if lk is None:
            lk = _ROLL_LOCKS[key] = _threading.Lock()
        return lk

def rollout_conv(path):
    """Cached conversation rows for a rollout, brought up to date.

    Returns {"rows", "hidden", "kinds", "ess"}. Rows are (role, text) for
    user and assistant entries only."""
    key = str(path)
    with _roll_lock(key):
        try:
            return _rollout_conv_locked(path, key)
        except (TypeError, AttributeError, KeyError, IndexError):
            # A line of the right JSON but the wrong shape. The file is the
            # agent's, so say nothing rather than fail every poll.
            _ROLL.pop(key, None)
            return {"rows": [], "hidden": 0, "kinds": [], "ess": []}

def _rollout_conv_locked(path, key):
    try:
        st = path.stat()
    except OSError:
        return {"rows": [], "hidden": 0, "kinds": [], "ess": []}
    c = _ROLL.get(key)
    # A shrunken file, or a different inode, is a different file.
    if not c or c["ino"] != st.st_ino or st.st_size < c["off"]:
        c = {"rows": [], "hidden": 0, "off": 0, "ino": st.st_ino,
             "kinds": [], "ess": [], "size": -1, "mtime": 0}
    elif c["size"] == st.st_size and c["mtime"] == st.st_mtime:
        return c                                   # nothing appended

    rows, hidden = c["rows"], c["hidden"]
    # Which transcript this is, decided by where it lives.
    is_claude = CLAUDE_PROJECTS in path.parents
    try:
        with path.open("rb") as fh:
            fh.seek(c["off"])
            carry = b""
            while True:
                chunk = fh.read(_ROLL_CHUNK)       # bounded memory, not 200MB
                if not chunk:
                    break
                carry += chunk
                cut = carry.rfind(b"\n") + 1      # whole lines only: the tail
                if not cut:                        # may still be being written
                    continue
                for line in carry[:cut].split(b"\n"):
                    if not line:
                        continue
                    o = _jobj(line)
                    if o is None:
                        continue
                    if is_claude:
                        hidden = _claude_rows(o, rows, hidden)
                        continue
                    if o.get("type") == "compacted":
                        # Codex compacts a long thread and prints a
                        # "Conversation recap" in its TUI. That recap is
                        # rendered, not recorded -- the rollout keeps the
                        # event and an empty message -- so the transcript
                        # cannot show the words. It can at least say where
                        # the thread was cut, which is what makes a jump in
                        # it explicable.
                        w = (o.get("payload") or {}).get("window_number")
                        rows.append(("compacted",
                                     "conversation compacted"
                                     + (" \u00b7 window %s" % w if w else "")))
                        continue
                    if o.get("type") != "response_item":
                        continue
                    pl = o.get("payload") or {}
                    t = pl.get("type")
                    if t == "message":
                        role = pl.get("role", "")
                        if role == "developer":
                            continue
                        rows.append((role, _text_of(pl.get("content"))))
                    elif t in ("custom_tool_call", "function_call",
                               "custom_tool_call_output", "function_call_output"):
                        hidden += 1
                c["off"] += cut
                carry = carry[cut:]
    except OSError:
        pass

    c["hidden"] = hidden
    c["size"], c["mtime"] = st.st_size, st.st_mtime
    # Classification is cheap over the conversation alone, and correct there:
    # the rule asks what the next non-machinery entry is, and this list is
    # nothing but non-machinery.
    c["kinds"] = kinds = [_codex_kind(r, b) for r, b in rows]
    c["ess"] = [_codex_essential(kinds, rows, i) for i in range(len(rows))]
    _ROLL[key] = c
    return c

def _codex_render(rows, kinds, ess, keep, items, skip, frm, md, base):
    """The window, and the HTML for it. Shared by the cached
    conversation path and the full re-read that includes tool calls."""
    CAP = 20000     # generous now that it is hidden until asked for
    parts = []
    # Two ways to slice. `frm` is an absolute index from the START of the
    # transcript and is what the terminal page uses: anchoring there means new
    # entries extend the view instead of pushing old ones off the top. `skip`
    # counts back from the newest, for callers that just want the tail.
    if frm is not None:
        lo = max(0, min(frm, len(keep)))
        hi = min(len(keep), lo + items)
    else:
        hi = len(keep) - skip
        lo = max(0, hi - items)
    for i in keep[max(0, lo):max(0, hi)]:
        role, body = rows[i]
        conversation = role in ("user", "assistant")
        if role == "compacted":
            parts.append('<div class="e mark" data-k="mark" data-ess="1">'
                         '<span>%s</span></div>' % _html.escape(body or ""))
            continue
        cls = ("user" if role == "user" else
               "asst" if role == "assistant" else
               "out" if role == "output" else "tool")
        body = (body or "").strip()
        head, size = _summary(role, body)
        if len(body) > CAP:
            body = body[:CAP] + f"\n… [{len(body) - CAP} more characters]"
        # Only what a person wrote or the model said. A tool call is JSON and
        # its output is a command dump; markdown would mangle both.
        if md and conversation:
            shown = '<div class="md">%s</div>' % md_cached(body, base)
        else:
            shown = "<pre>%s</pre>" % _html.escape(body)
        parts.append(
            '<details class="e {cls}" data-k="{k}" data-ess="{e}"{op}>'
            '<summary><span class="who">{who}</span>'
            '<span class="head">{head}</span><span class="size">{size}</span></summary>'
            '{body}</details>'.format(
                cls=cls, k=kinds[i], e="1" if ess[i] else "0",
                op=" open" if conversation else "",
                who=_html.escape(role), head=_html.escape(head),
                size=size, body=shown))
    return ("".join(parts) or "<em>nothing to show</em>"), len(keep)

def codex_html(path, items, skip=0, frm=None, md=True, base="", conv=False):
    """Render the transcript, newest last.

    Everything is a <details>. The conversation (user prompts, assistant
    replies) opens by default because that is what you came to read; tool calls
    and their output stay shut, because a single run of diffs and command dumps
    buries the thread. Each closed entry shows its first line and size, which is
    usually enough to decide whether to open it.

    Skips the developer preamble (a large instruction blob repeated every
    session) and reasoning entries (encrypted, so unreadable anyway)."""
    if conv:
        c = rollout_conv(path)
        rows, kinds, ess = c["rows"], c["kinds"], c["ess"]
        # env is machinery too, and it arrives with role "user" -- which is
        # why it survived the message filter and read as something typed.
        keep = [i for i in range(len(rows)) if kinds[i] in ("ask", "say", "mark")]
        return _codex_render(rows, kinds, ess, keep, items, skip, frm, md, base)

    rows = []
    # The machinery-included path reads the file whole, and it has to know
    # both formats too -- the cached path is not the only reader.
    claude = CLAUDE_PROJECTS in path.parents
    # It also keeps every tool call and every tool output, which for a large
    # transcript is the file itself in memory: work is 276MB. Two viewers
    # asking at once is how the server reached 3.5GB and stopped answering.
    # This view is a window onto the end anyway, so past a limit read only
    # the tail -- and say so rather than quietly showing less.
    head_skipped = 0
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    try:
        with open(path, "r", errors="replace") as fh:
            if size > FULL_READ_MAX:
                fh.seek(size - FULL_READ_MAX)
                fh.readline()               # drop the half line at the seam
                head_skipped = 1
            for line in fh:
                o = _jobj(line)
                if o is None:
                    continue
                if claude:
                    _claude_full(o, rows)
                    continue
                if o.get("type") == "compacted":
                    w = (o.get("payload") or {}).get("window_number")
                    rows.append(("compacted", "conversation compacted"
                                 + (" \u00b7 window %s" % w if w else "")))
                    continue
                if o.get("type") != "response_item":
                    continue
                pl = o.get("payload") or {}
                t = pl.get("type")
                if t == "message":
                    role = pl.get("role", "")
                    if role == "developer":
                        continue
                    rows.append((role, _text_of(pl.get("content"))))
                elif t == "custom_tool_call":
                    rows.append(("tool " + (pl.get("name") or ""), pl.get("input") or ""))
                elif t == "function_call":
                    rows.append(("call " + (pl.get("name") or ""), pl.get("arguments") or ""))
                elif t in ("custom_tool_call_output", "function_call_output"):
                    rows.append(("output", _text_of(pl.get("output"))))
    except OSError:
        return "<em>cannot read that session</em>", 0

    # What each entry is, and whether it carries the thread. The transcript
    # is mostly machinery: in one real session, 1148 tool calls and their
    # 1148 outputs against 45 user turns — and a third of those turns are the
    # environment blob codex injects as a user message, not something anyone
    # asked. Hiding that by default is what makes the rest readable.
    kinds = [_codex_kind(r, b) for r, b in rows]
    ess = [_codex_essential(kinds, rows, i) for i in range(len(rows))]
    # `conv` drops the machinery before the window is taken, not after. With
    # it taken after, a page of 120 entries was ~110 tool calls that the
    # browser then hid, and a long conversation looked like six messages.
    # Classification still runs over the whole sequence, so a turn boundary
    # is judged against what actually happened.
    keep = [i for i in range(len(rows))
            if not conv or kinds[i] in ("ask", "say", "mark")]

    body, total = _codex_render(rows, kinds, ess, keep, items, skip, frm, md, base)
    if head_skipped:
        body = ('<div class="e out"><pre>Showing the end of a large transcript. Untick &quot;tool calls&quot; for the whole conversation.</pre></div>') + body
    return body, total


# What an open terminal tab has told us about its own screen. In memory
# only, and only for as long as somebody is looking: a page reports when the
# screen settles on a question and again when it stops. Nothing polls, and
# the terminal's own data path is not touched -- a reader that cannot break
# the terminal is worth more than one that sees every session.
SCREEN_SAYS = {}
SCREEN_FRESH = 180      # a report older than this tells us nothing now

def screen_says(name):
    r = SCREEN_SAYS.get(name)
    if not r or time.time() - r.get("t", 0) > SCREEN_FRESH:
        return None
    return r

CODEX_QUIET_S = 40      # no new transcript for this long: it is waiting

def codex_quiet_for(cmd):
    """True when the codex session started by `cmd` has stopped writing."""
    m = _re.search(r"cd\s+(\S+)", cmd or "")
    if not m:
        return False
    try:
        want = str(Path(m.group(1)).expanduser().resolve())
    except (OSError, ValueError, RuntimeError):
        return False
    # codex_files reports the file's NAME, not its path -- stat-ing that was
    # a FileNotFoundError swallowed by the except, so every session looked
    # busy. The listing carries the mtime itself now.
    newest = max((x.get("mt", 0) for x in codex_files(40)
                  if x.get("cwd") == want), default=0.0)
    # No transcript at all means it has not started a conversation yet -- a
    # fresh codex sitting on its trust prompt or its composer. That is
    # waiting, not working.
    if not newest:
        return True
    return (time.time() - newest) > CODEX_QUIET_S

def term_entries():
    """Codex terminals as list entries, alongside the Claude sessions.

    They are a different kind of thing — a dtach session behind ttyd, with no
    /rc bridge — so they carry kind="codex" and are identified as "term:<name>"
    to avoid colliding with a folder button of the same name (there is both a
    Claude session and a codex terminal called fw)."""
    d = Path.home() / ".dtach"
    out = []
    for f in sorted(d.glob("*.cmd")):
        n = f.stem
        try:
            cmd = f.read_text().strip()
        except OSError:
            cmd = ""
        # A migrated Claude session has a .cmd here too, and it already has a
        # card of its own -- without this every one of them was listed twice,
        # the second time as a codex terminal.
        if _re.search(r"(^|&&|;)\s*claude\b", cmd) or "tmux new-session" in cmd:
            continue        # a Claude session, migrated or still under tmux
        # Codex keeps no status record the way Claude Code does, so "is it
        # working or waiting for me" comes from its rollout: a session that
        # is thinking appends to that file constantly, and one waiting at its
        # prompt has not touched it for a while.
        quiet = codex_quiet_for(cmd)
        # An open tab may have seen the screen stop on a question. That beats
        # the transcript's silence, which cannot tell "finished" from "asked".
        said = screen_says(n)
        asking = bool(said and said.get("asking"))
        out.append({
            "name": "term:" + n, "label": n, "kind": "codex",
            "state": "running" if (d / n).is_socket() else "stopped",
            "cc": "waiting" if asking else ("idle" if quiet else "busy"),
            "rc": False, "count": 0, "model": "", "link": "",
            "note": "",
            "snip": (said.get("text") or cmd[:160]) if asking else cmd[:160],
            "href": "t?name=" + n,
            "fw": len(slots_for(n)), "owes": reply_target(n),
        })
    return out


def saved_order():
    """The card order the last drag saved, or [] if nobody has dragged yet."""
    try:
        return list(json.loads(
            (ROOT / "config.json").read_text()).get("card_order") or [])
    except (OSError, ValueError):
        return []

def favorites():
    try:
        return list(json.loads((ROOT / "config.json").read_text()).get("favorites", []))
    except (OSError, ValueError, AttributeError, TypeError):
        return []                      # a config being rewritten under us


@_locked(_CFG_LOCK)
def set_favorite(name, on):
    """Pin or unpin, persisted in config.json so it follows you between
    devices rather than living in one browser.

    Pinning moves the card to the front of the saved order as well. The two
    have to agree: the pinned cards are drawn first, and within that group
    the drag order still decides -- so a star that only set a flag left the
    card exactly where it was, which is not what a star means. Unpinning
    leaves the position alone; the card simply rejoins the others where it
    now sits, rather than springing back somewhere you have to go and find.
    """
    cfg = json.loads((ROOT / "config.json").read_text())
    favs = [f for f in cfg.get("favorites", []) if f != name]
    if on:
        favs.insert(0, name)
        order = [n for n in (cfg.get("card_order") or []) if n != name]
        if cfg.get("card_order"):
            cfg["card_order"] = [name] + order
    cfg["favorites"] = favs
    write_private(ROOT / "config.json", json.dumps(cfg, indent=2) + "\n")
    return favs


def status_all():
    out = []
    for n, path in FOLDERS.items():
        st = state_of(n)
        cc, rc, count, model, link, href = "", False, 0, "", "", ""
        if st == "claude":
            infos = claude_infos(n, path)
            count = len(infos)
            if infos:
                cc = infos[0].get("status", "")
                bridge = infos[0].get("bridgeSessionId") or ""
                rc = bool(bridge)
                # Built from the session record rather than scraped out of the
                # pane, so it survives the transcript scrolling past the /rc reply.
                link = f"https://claude.ai/code/{bridge}" if bridge else ""
                model = model_of(infos[0])
        # The terminal link belongs to the session, not to its state. It used
        # to be built only for a session already reporting "claude", so a
        # session sitting on its very first prompt -- the trust question a new
        # folder asks -- reads as "shell", offers no link, and the card does
        # nothing when tapped. The one thing that would let you answer the
        # prompt was the one thing withheld until the prompt was answered.
        # Anything alive has something to attach to; only "dead" has not.
        if st != "dead":
            tn = ensure_claude_term(n)
            if tn:
                href = "t?name=" + tn
        out.append({"name": n, "label": n + SUFFIX, "state": st, "cc": cc, "rc": rc,
                    "count": count, "model": model, "link": link,
                    "kind": "claude", "href": href,
                    "note": note_for(n, st),
                    "snip": "" if st == "dead" else snippet(n)})
    for e in out:                      # what mail this session is holding
        e["fw"] = len(slots_for(e["label"]))
        e["owes"] = reply_target(e["label"])
    out.extend(term_entries())
    favs = favorites()
    for e in out:
        e["fav"] = e["name"] in favs
    # Dragging decides the order once it has been used: card_order lists every
    # card, of either kind, and anything new sorts to the end keeping the
    # order it arrived in. Until then, pinned first in the order they were
    # pinned -- and because the first drag saves exactly what is on screen,
    # the changeover moves nothing.
    # Pinned first, and inside each group the order dragging saved. A star
    # that did not move the card was the complaint: card_order alone decided
    # everything once anyone had dragged, so pinning set a flag and changed
    # nothing you could see.
    order = {n: i for i, n in enumerate(saved_order())}
    if order:
        out.sort(key=lambda e: (0 if e["fav"] else 1,
                                order.get(e["name"], len(order))))
    else:
        rank = {n: i for i, n in enumerate(favs)}
        out.sort(key=lambda e: (0, rank[e["name"]]) if e["name"] in rank else (1, 0))
    return out


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<link rel="icon" href="icon.png?s=64" type="image/png">
<link rel="apple-touch-icon" href="icon.png?s=180">
<link rel="manifest" href="manifest.webmanifest">
<meta name="theme-color" content="#0e0e11">
<title>roost</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; padding: 1rem; background: #111214; color: #e8e8ea;
         font: 17px/1.4 system-ui, sans-serif; }
  h1 { font-size: 1.1rem; font-weight: 600; color: #9a9aa0; margin: .2rem 0 1rem;
       display: flex; align-items: center; gap: .55rem; }
  /* Both controls are the same size and the same colour as the words around
     them: a tap target on a phone, not a decoration. */
  #hdr #home, #hdr #files {
    display: inline-flex; align-items: center; gap: .4rem;
    background: none; border: 0; padding: .3rem .4rem; margin: 0;
    color: inherit; font: inherit; border-radius: 8px; cursor: pointer;
    text-decoration: none; -webkit-tap-highlight-color: transparent; }
  #hdr #files { margin-left: auto; color: #7aa7ff; }
  #hdr #home:hover, #hdr #files:hover { background: rgba(255,255,255,.07); }
  #hdr #home:active, #hdr #files:active { background: rgba(255,255,255,.12); }
  #hdr #home svg, #hdr #files svg { display: block; }
  #hdr .hint { font-weight: 400; color: #6f6f76; }
  #hdr #new { color: #7aa7ff; margin-left: auto; }
  /* The files link used to be the one thing pushed to the end; now the two
     controls travel together. */
  #hdr #files { margin-left: 0; }

  /* The picker sits over the list rather than replacing it: it is a choice
     about the list, and closing it should leave everything as it was. */
  #picker { position: fixed; inset: 0; background: rgba(0,0,0,.55);
            display: flex; align-items: flex-end; justify-content: center;
            z-index: 20; }
  #picker[hidden] { display: none; }
  #picker .box { background: #16161a; border: 1px solid #2a2a31;
                 border-radius: 14px 14px 0 0; width: min(46rem, 100%);
                 max-height: 86vh; display: flex; flex-direction: column;
                 padding: .8rem .9rem 1rem; gap: .55rem; }
  @media (min-width: 700px) {
    #picker { align-items: center; }
    #picker .box { border-radius: 14px; }
  }
  #picker .ptop { display: flex; align-items: center; justify-content: space-between;
                  color: #d8d8de; font-size: 1.05rem; }
  #picker .ptop button { background: none; border: 0; color: #9a9aa0;
                         font-size: 1.5rem; line-height: 1; cursor: pointer;
                         padding: 0 .3rem; }
  #picker input { background: #0e0e11; border: 1px solid #2a2a31; color: #f0efec;
                  border-radius: 9px; padding: .55rem .6rem; font: inherit; }
  #picker input:focus { outline: none; border-color: #3a5a9a; }
  #pwhere { color: #8a8a93; font-size: .85rem; overflow-wrap: anywhere; }
  #plist { overflow-y: auto; -webkit-overflow-scrolling: touch;
           border: 1px solid #23232a; border-radius: 10px; min-height: 6rem; }
  #plist .prow { display: flex; align-items: center; gap: .45rem; width: 100%;
                 color: #f0efec; font: inherit; text-align: left;
                 padding: .42rem .6rem; cursor: pointer; }
  #plist .prow:hover { background: rgba(255,255,255,.05); }
  #plist .prow.sel { background: rgba(122,167,255,.14); }
  /* The twisty is its own tap target: opening a folder and choosing one are
     different intentions, exactly as in the file view's tree. */
  #plist .tw { color: #6f6f76; width: 1rem; flex: none; text-align: center; }
  #plist .prow .nm { overflow-wrap: anywhere; }
  #plist .prow .tag { font-size: .72rem; border-radius: 6px; padding: .05rem .35rem;
                      border: 1px solid #2f4d2f; color: #8fc98f; }
  #plist .prow .tag.on { border-color: #4a4a55; color: #8a8a93; }
  #plist .empty { color: #6f6f76; padding: .7rem; }
  .pfoot { display: flex; gap: .5rem; flex-wrap: wrap; }
  /* Which agent, as two words you can hit rather than a dropdown: there are
     two, and the one in force should be readable without opening anything. */
  #pagent { display: inline-flex; flex: none; border: 1px solid #2a2a31;
            border-radius: 9px; overflow: hidden; }
  #pagent button { background: none; border: 0; color: #8a8a93; font: inherit;
                   padding: .55rem .7rem; cursor: pointer; }
  #pagent button.on { background: #23304a; color: #cfe0ff; }
  .pfoot #pname { flex: 1 1 auto; min-width: 0; }
  .pfoot button { background: #23304a; border: 1px solid #3a5a9a; color: #cfe0ff;
                  border-radius: 9px; padding: .55rem .8rem; font: inherit;
                  cursor: pointer; }
  .pfoot button[disabled] { opacity: .5; cursor: default; }
  #pmsg { margin: 0; color: #c9a227; min-height: 1.2rem; font-size: .9rem; }
  /* On a phone the hint is the first thing worth losing: the two icons and
     the name have to stay reachable with a thumb. */
  @media (max-width: 460px) { #hdr .hint { display: none; } }
  /* A div, not a <button>: the card holds a link and a copy control, and
     nesting interactive elements inside a button is invalid and stops the
     link from being tappable. role/tabindex keep the keyboard behaviour. */
  /* Card and copy sit side by side; the row owns the vertical rhythm so the
     two stay exactly the same height (align-items: stretch). */
  /* Deliberately not the card's colour: copy is a different action from
     "press this session", so it must not read as part of the same surface. */
  /* One card per row, full width. The controls used to be full-height columns
     beside it, which on a phone left ~100px for the card and wrapped every
     label to one word per line. They are now a compact action bar inside it. */
  /* Card, restart, copy always in one row, buttons on the right. They shrink
     on narrow screens rather than wrapping underneath: at 390px that leaves
     ~212px for the card, against ~100px for the original fixed widths which
     wrapped every label to one word per line. */
  .row { display: flex; flex-wrap: nowrap; align-items: stretch;
         gap: .45rem; margin: .55rem 0; }
  .sess { display: block; flex: 1 1 auto; min-width: 0;
          padding: .75rem .85rem; border: 0;
         border-radius: 14px; font: inherit; color: #fff; text-align: left;
         cursor: pointer; -webkit-tap-highlight-color: transparent; }
  /* Status colours. These were lost when the card layout was rewritten: the
     classes were still being applied in render(), but their definitions had
     gone with the old CSS block, so every card came out unstyled. */
  .gray   { background: #4a4a50; }
  .yellow { background: #b8860b; }
  .green  { background: #2e7d32; }
  /* Waiting for you, rather than working. */
  .blue   { background: #1f4e79; }
  /* Stopped on a question and going nowhere until it is answered. */
  .purple { background: #5b3a8e; }
  .orange { background: #cc5a1e; }
  .top { display: flex; align-items: center; gap: .5rem; }
  /* Star sits at the right end of the card's title line. */
  .star { margin-left: auto; flex: none; font-size: 1.15rem; line-height: 1;
          color: rgba(255,255,255,.35); cursor: pointer; user-select: none;
          padding: 0 .1rem; -webkit-tap-highlight-color: transparent; }
  .star.on { color: #ffd257; }
  /* Codex terminals: same list, visibly not a Claude session. */
  .codex { background: #1e2a3a; border: 1px solid #35506e; }

  .codex.stopped { background: #191c22; border-color: #2c313b; }
  .kind { font-size: .68rem; letter-spacing: .02em;
          color: rgba(255,255,255,.6); border: 1px solid rgba(255,255,255,.22);
          border-radius: 999px; padding: .08rem .45rem; flex: none;
          white-space: nowrap; }
  .grip { flex: none; width: 1.5rem; text-align: center; cursor: grab;
         color: rgba(255,255,255,.45); font-size: 1rem; line-height: 1;
         user-select: none; touch-action: none; }
  .grip:active { cursor: grabbing; }
  /* A finger needs something to aim at. The column widens where there is no
     mouse; a press held anywhere on the card picks it up as well. */
  @media (pointer: coarse) {
    .grip { width: 2.6rem; font-size: 1.35rem; }
  }
  /* Lifted while it is in the air, and no transition on the card itself:
     it should sit under the pointer exactly, not chase it. */
  .row.dragging .sess { box-shadow: 0 .5rem 1.4rem #000a;
                        outline: 1px solid rgba(255,255,255,.18); }
  .name { font-size: 1.15rem; font-weight: 650; }
  .sess small { display: block; font-size: .8rem; font-weight: 400;
         opacity: .9; margin-top: .2rem; }
  .sess small.snip { font-style: italic; opacity: .6; font-size: .72rem;
         margin-top: .3rem; overflow: hidden; display: -webkit-box;
         -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
  .row.dragging { opacity: .85; }
  .row.dragging .sess { outline: 2px dashed rgba(255,255,255,.5); outline-offset: -3px; }
  /* Pills, not slabs: readable but clearly secondary to the card itself. */
  /* Siblings of the card again, so they sit beside it and match its height. */
  .row > button { flex: 0 0 auto; font: inherit; font-size: .78rem;
         font-weight: 700; padding: 0 1.1rem; border-radius: 14px;
         cursor: pointer; -webkit-tap-highlight-color: transparent; }
  .row > .restart { min-width: 6.5rem; color: #f3d9c4; background: #4a2e1c;
         border: 1px solid #7a4a2c; }
  /* Narrow screens keep the buttons on the right rather than wrapping them
     underneath; they shrink instead. At 390px this leaves roughly 260px for
     the card — enough to read — where the old fixed widths left about 100px
     and wrapped every label to one word per line. */
  .row > .restart:active { background: #5c3a24; }
  .row > .restart.arm { background: #8a2f22; border-color: #c05a45; color: #fff; }
  .row > .start { min-width: 6.5rem; color: #f3d9c4; background: #4a2e1c;
         border: 1px solid #43434c; }
  .row > .copy:active { background: #34343c; }
  #msg { min-height: 1.4rem; color: #9a9aa0; font-size: .85rem; }
  /* On a phone the two labels ate ~150px of a 390px screen laid out
     horizontally, squeezing the card until its caption wrapped to three
     lines. Turned on their side they read the same but cost ~30px each,
     which gives the card back ~115px. Words, not glyphs: "restart" is
     unambiguous in a way that a symbol is not. */
  @media (max-width: 560px) {
    .row > button {
      writing-mode: vertical-rl; text-orientation: mixed;
      display: flex; align-items: center; justify-content: center;
      min-width: 0; width: 1.9rem; padding: .4rem 0;
      font-size: .72rem; letter-spacing: .01em;
      /* Sideways, a wrap would start a second column and overflow the
         fixed width, so keep each label on one line. */
      white-space: nowrap; overflow: hidden;
    }
    /* The base rules set these wide for the desktop layout. */
    .row > .start { min-width: 0; }
  }
</style>
</head>
<body>
<!-- Two controls, not five links. "codex" and "terminals" were separate
     ways into things a card already opens; the words "files" and "logs"
     were a menu where two icons say the same in the space of one. -->
<h1 id="hdr">
  <button id="home" type="button" title="roost — back to the sessions">
    <svg viewBox="0 0 24 24" width="24" height="24" aria-hidden="true">
      <!-- the app icon, drawn the same way: a remote control, sending -->
      <g fill="none" stroke="currentColor" stroke-width="1.6"
         stroke-linecap="round">
        <path d="M14.4 3.6a5.2 5.2 0 0 1 3.7 3.7"/>
        <path d="M15.2 0.9a8 8 0 0 1 5.9 5.9"/>
      </g>
      <rect x="4.6" y="7.4" width="8.2" height="15.2" rx="2.2"
            fill="currentColor"/>
      <circle cx="8.7" cy="10.8" r="1.35" fill="#d8503c"/>
      <g fill="#14141a">
        <circle cx="7.1" cy="14.6" r="0.85"/><circle cx="10.3" cy="14.6" r="0.85"/>
        <circle cx="7.1" cy="17.2" r="0.85"/><circle cx="10.3" cy="17.2" r="0.85"/>
        <circle cx="7.1" cy="19.8" r="0.85"/><circle cx="10.3" cy="19.8" r="0.85"/>
      </g>
    </svg>
    <span>roost</span>
  </button>
  <span class="hint">tap a session to open it</span>
  <button id="new" type="button" title="new session — pick a repository">
    <svg viewBox="0 0 24 24" width="24" height="24" aria-hidden="true"
         fill="none" stroke="currentColor" stroke-width="1.9"
         stroke-linecap="round">
      <path d="M12 5.5v13M5.5 12h13"/>
    </svg>
  </button>
  <a id="files" href="file?tree=1" target="_blank"
     title="files — the tree, and every session's folder (opens a tab)">
    __FILESICON__
  </a>
</h1>
<div id="list"></div>
<p id="msg"></p>

<!-- The picker. A session is a folder on this machine, so choosing one is
     browsing to it: the tree starts at src_dir (what onboarding.sh wrote)
     and cannot leave it. Search is there because src_dir is somebody's whole
     working life and clicking down to one repository is slow on a phone. -->
<div id="picker" hidden>
  <div class="box">
    <div class="ptop"><strong>new session</strong>
      <button id="pclose" type="button" title="close">&times;</button></div>
    <input id="pq" type="search" placeholder="search repositories under the root"
           autocomplete="off" spellcheck="false">
    <div id="pwhere"></div>
    <div id="plist"></div>
    <div class="pfoot">
      <span id="pagent" role="group" aria-label="which agent">
        <button id="pclaude" type="button" class="on">Claude Code</button>
        <button id="pcodex" type="button">codex</button>
      </span>
      <input id="pname" placeholder="card name" autocomplete="off" spellcheck="false">
      <button id="pgo" type="button">choose a repository</button>
    </div>
    <p id="pmsg"></p>
  </div>
</div>
<script>
// Initial status is baked into the page: first paint costs a single request.
// No polling — refresh only on tap of the header or after a press.
const INIT = __INIT__;
// Tapping a card opens its terminal, whatever state it is in; "start" is
// the button that launches things. These two said the opposite, and named
// tmux for sessions that dtach has run for months.
const HINT = { dead:  "not running — start launches it",
               shell: "session up but not running yet — "
                      + "tap for the terminal, start relaunches it",
               // A codex terminal has no /rc bridge and no model to report;
               // its states are the dtach socket's, not tmux's. Without
               // these two the lookup missed and the card said "undefined".
               running: "tap for the terminal and its history",
               stopped: "not started — tap to start it" };

function describe(s) {
  if (s.state != "claude") return HINT[s.state] || s.state;
  // "waiting" is Claude Code's word for a modal it cannot pass; say what it
  // means, since the card is purple and the reason should be readable.
  if (s.cc === "waiting")
    return "stopped on a question — answer it in the terminal"
         + (s.model ? " · " + s.model : "");
  return (s.cc || "running")
       + (s.model ? " · " + s.model : "")
       + (s.count > 1 ? " · " + s.count + " parallel sessions" : "")
       + (s.rc ? " · remote control ON — tap to re-send link"
               : " · remote control off — tap to /rc");
}


// Dragging a card, the way a board does it.
//
// The old version re-inserted the row into the DOM on every pointer move, so
// the thing under your finger kept changing size and position as you moved —
// which is what made it feel like it was fighting you. Nothing moves in the
// DOM until you let go. While dragging:
//
//   the card follows the pointer, on a transform, lifted and unanimated
//   the others slide out of its way, on transforms, with a transition
//   the target index comes from the geometry captured when the drag began,
//     so it does not drift as things move
//
// One order is committed at the end, and the server is told once.
let dragging = null;
let lastDragEnd = 0;

function beginDrag(row, ev) {
  const list = row.parentNode;
  const rows = Array.from(list.children);
  const rects = rows.map((r) => r.getBoundingClientRect());
  const from = rows.indexOf(row);
  dragging = {
    row, list, rows, rects, from, to: from,
    startY: ev.clientY, h: rects[from].height + 10,   // 10 = the row gap
    pointerId: ev.pointerId, moved: false,
  };
  row.classList.add("dragging");
  row.style.transition = "none";
  row.style.zIndex = "5";
  row.style.position = "relative";
  for (const r of rows) if (r !== row) r.style.transition = "transform .16s ease";
  try { row.setPointerCapture(ev.pointerId); } catch (e) {}
  // On the window, not on the grip. Capturing the pointer retargets every
  // later event to the row, and the grip is a CHILD of the row -- so the
  // grip's own move handler stopped firing the moment the drag began, and
  // what kept it working on a desktop was only that the mouse happened to
  // stay over a 24px column. A finger does not.
  window.addEventListener("pointermove", onDragMove, { passive: false });
  window.addEventListener("pointerup", endDrag);
  window.addEventListener("pointercancel", endDrag);
  // A touch that has not yet scrolled can still be stopped from scrolling.
  // This is what lets a card be picked up from the middle of a scrollable
  // list without the list running away underneath it.
  window.addEventListener("touchmove", blockScroll, { passive: false });
}

function onDragMove(ev) {
  if (!dragging || ev.pointerId !== dragging.pointerId) return;
  ev.preventDefault();
  moveDrag(ev);
}

function blockScroll(ev) { if (dragging) ev.preventDefault(); }

function moveDrag(ev) {
  const d = dragging;
  if (!d) return;
  const dy = ev.clientY - d.startY;
  if (Math.abs(dy) > 3) d.moved = true;
  d.row.style.transform = "translateY(" + dy + "px)";

  // Where would it land? Compare the dragged card's centre against the
  // centres the other rows had before anything moved.
  // Against the neighbour's near edge, not its centre. Against centres you
  // had to drag a whole row before anything gave way, which reads as the
  // card refusing to move; against edges it steps aside at about 60%.
  const centre = d.rects[d.from].top + d.rects[d.from].height / 2 + dy;
  let to = d.from;
  for (let i = 0; i < d.rows.length; i++) {
    if (i === d.from) continue;
    const r = d.rects[i];
    if (i < d.from && centre < r.top + r.height) to = Math.min(to, i);
    if (i > d.from && centre > r.top) to = Math.max(to, i);
  }
  d.to = to;

  // Everything between here and there steps aside by one row.
  for (let i = 0; i < d.rows.length; i++) {
    if (i === d.from) continue;
    let shift = 0;
    if (to < d.from && i >= to && i < d.from) shift = d.h;
    else if (to > d.from && i > d.from && i <= to) shift = -d.h;
    d.rows[i].style.transform = shift ? "translateY(" + shift + "px)" : "";
  }
}

async function endDrag(ev) {
  const d = dragging;
  if (!d) return;
  dragging = null;
  window.removeEventListener("pointermove", onDragMove);
  window.removeEventListener("pointerup", endDrag);
  window.removeEventListener("pointercancel", endDrag);
  window.removeEventListener("touchmove", blockScroll);
  // A card opens its session on click, and a click follows every pointerup.
  // Without this, letting go of a dragged card opened a terminal.
  lastDragEnd = Date.now();
  try { d.row.releasePointerCapture(d.pointerId); } catch (e) {}
  for (const r of d.rows) { r.style.transition = ""; r.style.transform = ""; }
  d.row.classList.remove("dragging");
  d.row.style.zIndex = ""; d.row.style.position = "";
  if (d.to === d.from || !d.moved) return;          // a tap, or put back

  const ref = d.rows[d.to];
  d.list.insertBefore(d.row, d.to > d.from ? ref.nextSibling : ref);
  const names = Array.from(d.list.children).map((r) => r.dataset.name);
  try {
    const res = await fetch("api/reorder", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ names }),
    });
    const j = await res.json();
    document.getElementById("msg").textContent = j.result || j.error;
    if (j.error) refresh();          // server refused — resync with the truth
  } catch (err) {
    document.getElementById("msg").textContent = "reorder failed: " + err;
    refresh();
  }
}

// Picking a card up with a finger. The grip is a 24px column on a card that
// is about to move; on a phone that is not a target, it is a dare. So a
// press held on the card itself picks it up, the way a board app does: hold
// still for a third of a second and the card comes with you, move before
// that and it was a scroll, let go before that and it was a tap.
const HOLD_MS = 320, HOLD_SLOP = 10;

function armHold(row, card) {
  let timer = null, sx = 0, sy = 0, id = null;
  const cancel = () => { clearTimeout(timer); timer = null; };
  card.addEventListener("pointerdown", (e) => {
    // The mouse has the grip, and a mouse held still on a card is not asking
    // for anything. The star is a target of its own.
    if (e.button || e.pointerType === "mouse") return;
    if (e.target.closest && e.target.closest(".star")) return;
    sx = e.clientX; sy = e.clientY; id = e.pointerId;
    cancel();
    timer = setTimeout(() => {
      timer = null;
      if (dragging) return;
      beginDrag(row, { clientY: sy, pointerId: id });
      // Say it has been picked up. Without the buzz there is nothing to tell
      // you the hold worked except the card moving, which is too late.
      try { navigator.vibrate && navigator.vibrate(12); } catch (err) {}
    }, HOLD_MS);
  });
  card.addEventListener("pointermove", (e) => {
    if (timer && (Math.abs(e.clientX - sx) > HOLD_SLOP ||
                  Math.abs(e.clientY - sy) > HOLD_SLOP)) cancel();
  });
  card.addEventListener("pointerup", cancel);
  card.addEventListener("pointercancel", cancel);
  card.addEventListener("pointerleave", cancel);
}

function gripFor(row) {
  const g = document.createElement("div");
  g.className = "grip";
  g.textContent = "⣿";
  g.title = "drag to reorder";
  g.onclick = (e) => e.stopPropagation();   // never open the terminal
  g.onpointerdown = (e) => {
    e.preventDefault(); e.stopPropagation();
    beginDrag(row, e);
  };
  return g;
}

// Pinning. Stored server-side rather than in localStorage, so the order is
// the same on the phone and the desktop.
function starFor(s) {
  const st = document.createElement("span");
  st.className = "star" + (s.fav ? " on" : "");
  st.textContent = s.fav ? "★" : "☆";
  st.title = s.fav ? "unpin" : "pin to top";
  st.onclick = async (e) => {
    e.stopPropagation();          // never trigger the card's own action
    const on = !s.fav;
    st.className = "star" + (on ? " on" : "");
    st.textContent = on ? "★" : "☆";
    try {
      await fetch("api/favorite?name=" + encodeURIComponent(s.name)
                  + "&on=" + (on ? 1 : 0), { method: "POST" });
    } catch (err) { /* the refresh below will resync */ }
    refresh();
  };
  return st;
}


function render(sessions) {
  const list = document.getElementById("list");
  list.innerHTML = "";
  for (const s of sessions) {
    const row = document.createElement("div");
    row.className = "row";
    row.dataset.name = s.name;

    // Two colours and a dead one. The old scheme spent four on distinctions
    // the caption already makes in words; these two say the thing the
    // caption cannot say at a glance across fifteen cards -- green is
    // working, blue is waiting for you. For Claude that is its own status
    // record; for codex it is whether the transcript has stopped growing.
    const down = s.kind === "codex" ? s.state !== "running" : s.state === "dead";
    // Three live states, and the server already knows all three -- this costs
    // nothing beyond reading a field that was in the payload anyway.
    //   green  working
    //   blue   idle: finished, waiting for the next instruction
    //   purple stopped ON something: a prompt, a permission, a trust dialog
    const asking = !down && s.cc === "waiting";
    const idle = !down && s.cc === "idle";
    const card = document.createElement("div");
    card.className = "sess " + (down ? "gray"
                                : asking ? "purple" : idle ? "blue" : "green");
    card.setAttribute("role", "button");
    card.tabIndex = 0;

    const top = document.createElement("div");
    top.className = "top";
    top.appendChild(gripFor(row));
    const nm = document.createElement("div");
    nm.className = "name";
    // The roost name, with its @: this is what one agent calls another, so it
    // should be the thing you read off the card, not a decoration.
    nm.textContent = "@" + (s.label || s.name);
    top.appendChild(nm);
    const k = document.createElement("span");
    k.className = "kind";
    k.textContent = s.kind === "codex" ? "codex" : "Claude Code";
    top.appendChild(k);
    // Mail state, in the same hand as the conversation view: ↪ waiting to be
    // forwarded, ↩ an answer owed to whoever forwarded work here.
    for (const [cls, text, title] of [
          ["fw", s.fw ? "↪" + (s.fw > 1 ? s.fw : "") : "",
           s.fw + " waiting to be forwarded"],
          ["owes", s.owes ? "↩" : "", "owes an answer to @" + s.owes]]) {
      if (!text) continue;
      const b = document.createElement("span");
      b.className = "mail " + cls;
      b.textContent = text;
      b.title = title;
      top.appendChild(b);
    }
    top.appendChild(starFor(s));
    card.appendChild(top);

    const cap = document.createElement("small");
    cap.textContent = describe(s) + (s.note ? " · ⚠ " + s.note : "");
    card.appendChild(cap);

    if (s.snip) {
      const sn = document.createElement("small");
      sn.className = "snip";
      sn.textContent = s.snip;   // textContent: pane text can't inject HTML
      card.appendChild(sn);
    }

    // Tapping a card opens its terminal — for both kinds now. The history is
    // a tab in there, so it needs no button of its own out here.
    // A card opens its session in a window of its own: the dashboard is the
    // place you come back to, and losing it to every glance at a terminal is
    // what made it feel like a dead end.
    const go = () => {
      if (!s.href) return;
      // Not "noopener": window.open returns null when that is passed, by
      // specification and not because anything failed — so the "it was
      // blocked" fallback fired every time and opened the session twice, once
      // in a new window and once in this one. The target is this same origin,
      // so an opener reference is not a hazard here.
      const w = window.open(s.href, "_blank");
      if (!w) window.location = s.href;      // actually blocked: go there
    };
    card.onclick = () => {
      // Letting go of a card you just dragged is a pointerup, and a click
      // follows it. Opening a terminal there is not what the drag meant.
      if (Date.now() - lastDragEnd < 400) return;
      go();
    };
    card.onkeydown = (e) => {
      if (e.key == "Enter" || e.key == " ") { e.preventDefault(); go(); }
    };
    armHold(row, card);
    row.appendChild(card);
    row.appendChild(startButton(s, card));
    list.appendChild(row);
  }
}

// One button beside the card, doing what the card used to do: bring the
// session up, or send /rc to one already running.
//
// Pressing it again while nothing has changed is how you say "it is not
// answering" — so the second press arms a restart and says so, and the third
// carries it out. The confirmation is the arming: a restart takes a session
// off the air, and it should not be one tap away from a button you press
// when things are slow.
function startButton(s, card) {
  const b = document.createElement("button");
  b.className = "start";
  b.type = "button";
  b.textContent = "start";
  let armed = false, lastState = "", disarm = null;
  const reset = () => {
    armed = false; b.classList.remove("arm"); b.textContent = "start";
  };
  b.onclick = async (e) => {
    e.stopPropagation();
    const msg = document.getElementById("msg");
    const label = s.label || s.name;
    if (armed) {
      clearTimeout(disarm); reset();
      b.textContent = "…"; card.classList.add("busy");
      msg.textContent = label + ": restarting…";
      try {
        const r = await fetch("api/restart?name=" + encodeURIComponent(s.name),
                              { method: "POST" });
        const j = await r.json();
        msg.textContent = label + ": " + (j.result || j.error);
      } catch (err) { msg.textContent = label + ": " + err; }
      b.textContent = "start";
      setTimeout(refresh, 2500);
      return;
    }
    const before = s.state + "/" + s.cc + "/" + s.rc;
    b.textContent = "…";
    msg.textContent = label + ": …";
    try {
      const r = await fetch("api/press?name=" + encodeURIComponent(s.name),
                            { method: "POST" });
      const j = await r.json();
      msg.textContent = label + ": " + (j.result || j.error);
    } catch (err) { msg.textContent = label + ": " + err; }
    b.textContent = "start";
    // Nothing moved since the last press: offer the bigger hammer.
    if (lastState && lastState === before) {
      armed = true;
      b.classList.add("arm");
      b.textContent = "restart?";
      clearTimeout(disarm);
      disarm = setTimeout(reset, 6000);
    }
    lastState = before;
    setTimeout(refresh, 1500);
  };
  return b;
}

async function refresh() {
  try {
    const r = await fetch("api/status");
    render(await r.json());
  } catch (e) {
    document.getElementById("msg").textContent = "status fetch failed: " + e;
  }
}

async function pressBtn(btn, name, label) {
  btn.classList.add("busy");
  document.getElementById("msg").textContent = label + ": …";
  try {
    const r = await fetch("api/press?name=" + encodeURIComponent(name), { method: "POST" });
    const j = await r.json();
    document.getElementById("msg").textContent = label + ": " + (j.result || j.error);
  } catch (e) {
    document.getElementById("msg").textContent = label + ": " + e;
  }
  setTimeout(refresh, 1500);
}

// ---------------------------------------------------------------- picker
// Pick a folder, get a card. Everything on screen is built with textContent:
// these are names off somebody's disk, and the page is the one place where a
// folder called <img onerror> would matter.
const picker = document.getElementById("picker");
const plist = document.getElementById("plist");
const pwhere = document.getElementById("pwhere");
const pname = document.getElementById("pname");
const pgo = document.getElementById("pgo");
const pmsg = document.getElementById("pmsg");
const pq = document.getElementById("pq");
let pTree = null;                // every repository under the root
let pOpen = new Set();           // the folders showing their contents
let pSel = "";                   // the folder the footer acts on
let pSelNode = null;
let pRoot = "";
let pAgent = "claude";           // which agent the button will start

function shortPath(path) {
  if (!pRoot || path === pRoot) return path;
  return path.startsWith(pRoot + "/") ? "." + path.slice(pRoot.length) : path;
}

// The tree the picker shows is the file view's tree with one job: a twisty
// opens a folder, the name chooses it, and every branch leads to a
// repository because the server left out the ones that do not.
function ptree(nodes, depth, into, term) {
  for (const n of nodes) {
    const hit = !term || n.path.toLowerCase().includes(term);
    const kids = document.createElement("div");
    const shown = ptree(n.kids, depth + 1, kids, term);
    if (!hit && !shown) continue;          // nothing here matches
    const row = document.createElement("div");
    row.className = "prow" + (n.path === pSel ? " sel" : "");
    row.style.paddingLeft = (0.45 + depth * 0.85) + "rem";

    const tw = document.createElement("span");
    tw.className = "tw";
    const open = term ? true : pOpen.has(n.path);
    tw.textContent = n.kids.length ? (open ? "\u25be" : "\u25b8") : "\u00b7";
    tw.onclick = (e) => {
      e.stopPropagation();
      if (!n.kids.length) return;
      if (pOpen.has(n.path)) pOpen.delete(n.path); else pOpen.add(n.path);
      pdraw();
    };
    row.appendChild(tw);

    const name = document.createElement("span");
    name.className = "nm";
    name.textContent = n.name;
    row.appendChild(name);

    if (n.repo) {
      const t = document.createElement("span");
      t.className = "tag" + (n.added ? " on" : "");
      t.textContent = n.added ? "has a card" : "repo";
      row.appendChild(t);
    }
    row.onclick = () => { psel(n); };
    into.appendChild(row);
    if (open && n.kids.length) into.appendChild(kids);
  }
  return into.childElementCount > 0;
}

function pact() {
  const what = pAgent === "codex" ? "codex" : "Claude Code";
  if (!pSelNode) { pgo.disabled = true; pgo.textContent = "choose a repository"; return; }
  // "has a card" is about a Claude session in that folder; a codex terminal
  // beside it is an ordinary thing to want, so only Claude Code is refused.
  const taken = pSelNode.added && pAgent === "claude";
  pgo.disabled = taken;
  pgo.textContent = taken ? "already has a Claude Code card"
                          : "start " + what + " in " + pSelNode.name;
}

function psel(n) {
  pSel = n.path;
  pSelNode = n;
  pname.value = n.name;
  pact();
  pdraw();
}

function pagent(which) {
  pAgent = which;
  document.getElementById("pclaude").classList.toggle("on", which === "claude");
  document.getElementById("pcodex").classList.toggle("on", which === "codex");
  pact();
}
document.getElementById("pclaude").onclick = () => pagent("claude");
document.getElementById("pcodex").onclick = () => pagent("codex");

function pdraw() {
  if (!pTree) return;
  const term = pq.value.trim().toLowerCase();
  plist.textContent = "";
  const box = document.createElement("div");
  const any = ptree(pTree.nodes, 0, box, term);
  plist.appendChild(box);
  if (!any) {
    const e = document.createElement("div");
    e.className = "empty";
    e.textContent = term ? "nothing matches" : "no repositories under the root";
    plist.appendChild(e);
  }
  pwhere.textContent = shortPath(pTree.root) + " \u00b7 "
    + pTree.repos + " repositor" + (pTree.repos === 1 ? "y" : "ies")
    + (pTree.cut ? " (as far as it looked)" : "");
  const sel = plist.querySelector(".prow.sel");
  if (sel && !pq.value) sel.scrollIntoView({ block: "nearest" });
}

async function pload() {
  pmsg.textContent = "looking for repositories\u2026";
  try {
    const r = await fetch("api/repos");
    pTree = await r.json();
    pRoot = pTree.root;
    pOpen = new Set();
    // Open enough to see something: the whole tree when it is small, the
    // first level when it is not.
    const count = (ns) => ns.reduce((n, x) => n + 1 + count(x.kids), 0);
    const all = count(pTree.nodes) <= 40;
    const open = (ns) => ns.forEach((x) => {
      if (x.kids.length) { pOpen.add(x.path); if (all) open(x.kids); }
    });
    open(pTree.nodes);
    pmsg.textContent = "";
    pdraw();
  } catch (e) { pmsg.textContent = "could not read the tree: " + e; }
}

let pTimer = 0;
pq.oninput = () => { clearTimeout(pTimer); pTimer = setTimeout(pdraw, 120); };

pgo.onclick = async () => {
  if (!pSelNode) return;
  pgo.disabled = true;
  pmsg.textContent = "starting\u2026";
  try {
    const r = await fetch("api/session?path=" + encodeURIComponent(pSel)
                          + "&name=" + encodeURIComponent(pname.value.trim())
                          + "&agent=" + pAgent,
                          { method: "POST" });
    const j = await r.json();
    if (j.error) { pmsg.textContent = j.error; pgo.disabled = false; return; }
    picker.hidden = true;
    document.getElementById("msg").textContent = j.name + ": " + j.result;
    refresh();
    setTimeout(refresh, 2500);     // the card turns green a moment later
  } catch (e) { pmsg.textContent = "" + e; pgo.disabled = false; }
};

document.getElementById("new").onclick = () => {
  picker.hidden = false;
  pq.value = "";
  pSel = ""; pSelNode = null;
  pagent(pAgent);
  pload();
};
document.getElementById("pclose").onclick = () => { picker.hidden = true; };
picker.onclick = (e) => { if (e.target === picker) picker.hidden = true; };
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !picker.hidden) picker.hidden = true;
});

render(INIT);
// The icon is the refresh: the whole header used to take the tap, which
// swallowed the links inside it and left no way to tell a tap from a miss.
document.getElementById("home").onclick = () => {
  // Back to the default view: the session list, at the top, up to date.
  document.getElementById("msg").textContent = "";
  window.scrollTo(0, 0);
  refresh();
};
</script>
</body>
</html>
""".replace("__FILESICON__", files_icon(24))


LOGS_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<link rel="icon" href="icon.png?s=64" type="image/png">
<link rel="apple-touch-icon" href="icon.png?s=180">
<link rel="manifest" href="manifest.webmanifest">
<meta name="theme-color" content="#0e0e11">
<title>__TITLE__</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; padding: .6rem; background: #0e0e11; color: #d8d8de;
         font: 13px/1.35 ui-monospace, SFMono-Regular, Menlo, monospace; }
  header { display: flex; gap: .5rem; align-items: center; flex-wrap: wrap;
           margin-bottom: .5rem; font-family: system-ui, sans-serif; }
  a, select, button { font: inherit; font-family: system-ui, sans-serif; }
  a { color: #7aa7ff; text-decoration: none; }
  select, button, input { font: inherit; background: #1b1b1f; color: #e8e8ea;
                   border: 1px solid #33333b; border-radius: 999px;
                   padding: .4rem .8rem; -webkit-tap-highlight-color: transparent; }
  button { cursor: pointer; font-weight: 600; font-size: .78rem; }
  button:active { background: #2b2b33; }
  #live { margin-left: auto; font-size: .8rem; color: #7a7a83; }
  pre { margin: 0; white-space: pre-wrap; word-break: break-word;
        background: #000; padding: .6rem; border-radius: 10px;
        border: 1px solid #23232a; min-height: 55vh; }
  /* Sticks to the bottom so it stays reachable while the pane scrolls, and
     sits above the home indicator on a phone. */
  #cmdbar { position: sticky; bottom: 0; display: flex; align-items: center;
            gap: .5rem; margin-top: .5rem;
            padding: .5rem 0 calc(.5rem + env(safe-area-inset-bottom));
            background: #0e0e11; }
  #cmd { flex: 1; min-width: 0; font: inherit;
         font-family: system-ui, sans-serif; background: #1b1b1f;
         color: #e8e8ea; border: 1px solid #33333b; border-radius: 8px;
         padding: .5rem .6rem; }
  #cmd:disabled { opacity: .5; }
  #sent { font-family: system-ui, sans-serif; font-size: .8rem;
          color: #7a7a83; min-width: 3.5rem; }
</style>
</head>
<body>
<header>
  __HOMELINK__
  <select id="sess">__OPTIONS__</select>
  <select id="lines">
    <option value="60">60 lines</option>
    <option value="200" selected>200 lines</option>
    <option value="1000">1000 lines</option>
  </select>
  <button id="pause" type="button">pause</button>
  <a id="hist" href="">history</a>
  <span id="live">live</span>
</header>
<pre id="out">loading…</pre>
<form id="cmdbar" autocomplete="off">
  <input id="cmd" type="text" placeholder="type a command, Enter to send"
         autocapitalize="off" autocorrect="off" spellcheck="false">
  <span id="sent"></span>
</form>
<script>
// Mostly read-only: this polls capture-pane and renders it. The one exception
// is the command bar below, which types into the selected session — so this
// page CAN disturb a run, unlike every earlier version of it.
const out = document.getElementById("out");
const HIST = __HIST__;
const sess = document.getElementById("sess");
const lines = document.getElementById("lines");
const pause = document.getElementById("pause");
const live = document.getElementById("live");
let timer = null, paused = false, atBottom = true;

window.addEventListener("scroll", () => {
  atBottom = (window.innerHeight + window.scrollY) >= document.body.offsetHeight - 40;
});

async function tick() {
  if (paused) return;
  try {
    const r = await fetch("api/pane?name=" + encodeURIComponent(sess.value)
                          + "&lines=" + lines.value);
    const j = await r.json();
    out.innerHTML = j.html;
    live.textContent = "live · " + new Date().toLocaleTimeString();
    // Follow the tail unless the reader has scrolled up to look at something.
    if (atBottom) window.scrollTo(0, document.body.scrollHeight);
  } catch (e) {
    live.textContent = "disconnected";
  }
}
function restart() {
  if (timer) clearInterval(timer);
  tick();
  timer = setInterval(tick, 2000);
  const u = new URL(window.location);
  u.searchParams.set("name", sess.value);
  history.replaceState(null, "", u);
}
const cmdbar = document.getElementById("cmdbar");
const cmd = document.getElementById("cmd");
const sentmsg = document.getElementById("sent");
cmdbar.onsubmit = async (e) => {
  e.preventDefault();                       // Enter submits; never navigate
  const text = cmd.value;
  if (!text.trim()) return;
  cmd.disabled = true;
  try {
    const r = await fetch("api/type?name=" + encodeURIComponent(sess.value), {
      method: "POST",
      body: JSON.stringify({text: text}),
    });
    const j = await r.json();
    // Clear only on success, so a rejected line is not silently lost from
    // the box with nothing to retry from.
    if (j.result) { cmd.value = ""; sentmsg.textContent = "sent"; }
    else { sentmsg.textContent = j.error || "failed"; }
  } catch (err) {
    sentmsg.textContent = "failed";
  }
  cmd.disabled = false;
  cmd.focus();
  setTimeout(() => { sentmsg.textContent = ""; }, 2500);
  tick();                                   // show the echo without waiting
};

// The transcript for whichever session is showing. A codex pane has none
// here — it keeps its own, reachable from the terminals list — so the link
// disappears rather than going nowhere.
const histLink = document.getElementById("hist");
function syncHist() {
  const f = HIST[sess.value];
  histLink.style.display = f ? "" : "none";
  if (f) histLink.href = "codex?file=" + encodeURIComponent(f);
}
sess.onchange = () => { syncHist(); restart(); };
lines.onchange = restart;

syncHist();
restart();
</script>
</body>
</html>
""".replace("__HOMELINK__", home_link(18))

CODEX_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<link rel="icon" href="icon.png?s=64" type="image/png">
<link rel="apple-touch-icon" href="icon.png?s=180">
<link rel="manifest" href="manifest.webmanifest">
<meta name="theme-color" content="#0e0e11">
<title>codex — session</title>
__KATEX__
<style>
__INTER__
  /* Scrollbars the way an editor does them: a thin translucent thumb over
     the content, no track, no arrows, and a little more contrast while the
     pointer is on it. Firefox takes the two-property form; WebKit and
     Blink need the pseudo-elements, and the transparent border plus
     background-clip is what insets the thumb rather than letting it touch
     the edge. */
  * { scrollbar-width: thin; scrollbar-color: rgba(255,255,255,.16) transparent; }
  ::-webkit-scrollbar { width: 11px; height: 11px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-corner { background: transparent; }
  ::-webkit-scrollbar-thumb { background: rgba(255,255,255,.16);
      border-radius: 8px; border: 3px solid transparent;
      background-clip: content-box; }
  ::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,.30);
      border: 2px solid transparent; background-clip: content-box; }
  ::-webkit-scrollbar-thumb:active { background: rgba(255,255,255,.42);
      border: 2px solid transparent; background-clip: content-box; }
  /* Claude's own dark palette, read off the app's stylesheet rather than
     eyeballed: gray-850 is the page, gray-800 the raised surface a person's
     turn sits on, gray-50 the text. The web font is theirs and not ours to
     serve, so this is the fallback chain their token names. */
  :root {
    --c-bg: #151515; --c-raised: #20201f; --c-deep: #0b0b0b;
    --c-text: #f0efec; --c-muted: #898781;
    --c-line: rgba(255,255,255,.10); --c-line-strong: rgba(255,255,255,.20);
    --c-accent: #63a8ee; --c-clay: #d97757;
    --c-font: system-ui, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    --c-mono: ui-monospace, SFMono-Regular, Menlo, monospace;
    /* The same face the document viewer reads in: Inter below regular
       weight, which holds its colour on a dark screen where the UI sans at
       UI weight shouts. A conversation is the part you sit and read. */
    --c-read: Inter, system-ui, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    --c-read-ink: #e0ddd7;
  }
  :root { color-scheme: dark; }
  body { margin: 0; padding: .6rem; background: var(--c-bg); color: var(--c-text);
         font: 14px/1.45 system-ui, sans-serif; }
  header { display: flex; gap: .5rem; align-items: center; flex-wrap: wrap;
           margin-bottom: .6rem; position: sticky; top: 0; background: var(--c-bg);
           padding: .3rem 0; z-index: 5; }
  a { color: #7aa7ff; text-decoration: none; }
  select, button, input { font: inherit; background: #1b1b1f; color: #e8e8ea;
                   border: 1px solid #33333b; border-radius: 999px;
                   padding: .4rem .8rem; max-width: 62vw;
                   -webkit-tap-highlight-color: transparent; }
  button { cursor: pointer; font-weight: 600; font-size: .78rem; }
  button:active { background: #2b2b33; }
  #meta { margin-left: auto; font-size: .78rem; color: #7a7a83; }
  .e { border-radius: 10px; margin: .4rem 0; border: 1px solid #23232a;
       background: #141419; overflow: hidden; }
  .e > summary { list-style: none; cursor: pointer; padding: .45rem .6rem;
       display: flex; gap: .5rem; align-items: baseline; }
  .e > summary::-webkit-details-marker { display: none; }
  .e > summary::before { content: "▸"; color: #6a6a75; flex: none; }
  .e[open] > summary::before { content: "▾"; }
  .who { font-size: .66rem; text-transform: uppercase; letter-spacing: .06em;
         color: #8a8a95; flex: none; }
  .head { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;
          white-space: nowrap; color: #b8b8c2;
          font: 12px/1.3 ui-monospace, SFMono-Regular, Menlo, monospace; }
  .size { flex: none; font-size: .66rem; color: #6a6a75; }
  .e[open] .head { display: none; }
  .e pre { margin: 0; padding: 0 .65rem .55rem; white-space: pre-wrap;
           word-break: break-word;
           font: 12.5px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; }
  #alllab { font-family: system-ui, sans-serif; font-size: .8rem;
            color: #9a9aa4; display: flex; align-items: center; gap: .3rem; }
  .filehdr { margin: .9rem 0 .3rem; padding: .25rem .5rem; border-radius: 6px;
             background: #1b1b1f; border: 1px solid #33333b; color: #9a9aa4;
             font: 11.5px/1.3 ui-monospace, SFMono-Regular, Menlo, monospace; }
  /* Machinery is hidden until asked for. In a real session that is 2452 of
     2823 entries -- the transcript is unreadable with them in. */
  #out:not(.noise) .e[data-k="tool"],
  #out:not(.noise) .e[data-k="out"],
  #out:not(.noise) .e[data-k="env"],
  #out:not(.noise) .e[data-k="think"] { display: none; }
  /* Reading view, the default: no summary line, no disclosure arrow, no
     border and no rounding -- plain blocks of text butted together, each
     keeping a background colour so you can still see where a turn starts.
     Assistant text sits on the page's own black. "detailed" restores the
     transcript chrome. */
  #out:not(.detailed) .e > summary { display: none; }
  #out:not(.detailed) .e { border: 0; border-radius: 0; margin: 0; }
  #out:not(.detailed) .e .md { padding: .1rem .6rem .5rem; }
  #out:not(.detailed) .e pre { padding: .5rem .7rem;
      /* Prose, not console: the monospace grid is for the terminal, and
         this is the part you sit and read. */
      font: .95rem/1.68 var(--c-read); font-weight: 350;
      color: var(--c-read-ink); }
  #out:not(.detailed) .asst { background: var(--c-bg); }
  #out:not(.detailed) .asst pre, #out:not(.detailed) .asst .md { color: var(--c-text); }
  /* A person's turn as a bubble against the right edge, the way the web
     conversation reads: the band across the full width made every question
     look like a section heading. Text inside stays left-aligned — only the
     box moves. */
  /* Clear air on both sides: in a wall of assistant prose the bubble has
     to announce itself as the moment someone spoke. */
  #out:not(.detailed) .user { background: none; margin: 1.45rem 0; }
  #out:not(.detailed) .user .md,
  #out:not(.detailed) .user pre { background: var(--c-raised);
      color: var(--c-text); border-radius: 16px; padding: .5rem .85rem;
      width: fit-content; max-width: 85%; margin-left: auto; }
  #out:not(.detailed) .asst { margin-bottom: .3rem; }
  .hidden { display: none !important; }
  #noiselab, #noiselab2, #noiselab3 { font-size: .72rem; color: #9a9aa6; display: flex;
              align-items: center; gap: .3rem; }
  #noiselab input, #noiselab2 input, #noiselab3 input { margin: 0; }
  .e[data-ess="1"] > summary .who { color: #d8b45a; }
  /* Rendered markdown. The table scrolls inside its own box: a wide table
     must not make the whole page scroll sideways on a phone. */
  .e .md { padding: .1rem .7rem .55rem; color: var(--c-read-ink);
           font: .95rem/1.68 var(--c-read); font-weight: 350;
           -webkit-font-smoothing: antialiased;
           -moz-osx-font-smoothing: grayscale; }
  /* One step up from the text, not three. */
  .e .md strong, .e .md b { font-weight: 600; color: #f0eeea; }
  .e .md h1, .e .md h2, .e .md h3, .e .md h4 {
      font-weight: 600; color: #f0eeea; }
  .e .md > :first-child { margin-top: 0; }
  .e .md > :last-child { margin-bottom: 0; }
  .e .md p, .e .md ul, .e .md ol, .e .md blockquote { margin: .45rem 0; }
  .e .md h1, .e .md h2, .e .md h3, .e .md h4, .e .md h5, .e .md h6 {
      margin: .6rem 0 .3rem; font-size: 1.02em; font-weight: 700; }
  .e .md ul, .e .md ol { padding-left: 1.2rem; }
  .e .md li { margin: .15rem 0; }
  .e .md table { border-collapse: collapse; margin: .5rem 0; font-size: .93em;
                 display: block; max-width: 100%; overflow-x: auto; }
  .e .md th, .e .md td { border: 1px solid var(--c-line-strong); padding: .3rem .55rem;
                         text-align: left; vertical-align: top;
                         overflow-wrap: break-word; }
  .e .md th { background: rgba(255,255,255,.06); font-weight: 600; }
  .e .md code { background: rgba(255,255,255,.08); border-radius: 4px;
                padding: .05rem .3rem; font: .855em/1.4 var(--c-mono); }
  .e .md pre.code { background: var(--c-deep); border: 1px solid var(--c-line);
                    border-radius: 8px; margin: .55rem 0; padding: .5rem .6rem;
                    overflow-x: auto; white-space: pre;
                    font: 13px/1.5 var(--c-mono); }
  .e .md blockquote { border-left: 2px solid var(--c-line-strong);
                      padding-left: .7rem; color: var(--c-muted); }
  .math, .imath { font: 13px/1.5 var(--c-mono); }
  /* Only as wide as the formula. A full-width band for a six-character
     inequality reads as a section break rather than as one line of maths;
     fit-content sizes the box to what is in it, and the cap plus the scroll
     handle a formula wider than the column. */
  .math { display: block; width: fit-content; max-width: 100%;
          white-space: pre-wrap; overflow-x: auto;
          background: var(--c-deep); border: 1px solid var(--c-line);
          border-radius: 8px; margin: .55rem 0; padding: .5rem .6rem; }
  /* KaTeX centres display maths. In a document of left-aligned prose that
     leaves each formula stranded in the middle of a wide line, disconnected
     from the sentence that introduces it, so it is set flush left like
     everything else. The outer .math box keeps the horizontal scroll for a
     formula wider than the column. */
  .math .katex-display,
  .math .katex-display > .katex { text-align: left; }
  .math .katex-display { margin: 0; }
  /* Source shown because the typesetter never arrived, not because the
     document meant it that way. */
  .noks { border-color: var(--c-clay) !important; opacity: .85; }
  .e .md a { color: var(--c-accent); }
  .e .md a.fileref::after { content: " \\2197"; font-size: .85em; opacity: .7; }
  .e .md, .e .md p, .e .md li, .e .md td { overflow-wrap: anywhere; }
  .e .md a .u { color: var(--c-muted); font-size: .84em; overflow-wrap: anywhere; }
  /* Where codex cut the thread. Its own "Conversation recap" is drawn by
     the TUI and never written to the rollout, so the words cannot be shown
     -- but the seam can, and a jump in the conversation then has a reason. */
  .e.mark { display: flex; align-items: center; gap: .6rem; border: 0;
            margin: .9rem 0; padding: 0; background: none;
            color: var(--c-muted); font-size: .72rem; letter-spacing: .04em;
            text-transform: uppercase; }
  .e.mark::before, .e.mark::after { content: ""; flex: 1 1 auto;
            border-top: 1px solid var(--c-line); }

  /* A link whose address is on the hover rather than on the page says so
     with a dotted underline; the button beside it copies the address. */
  a.named { text-decoration: none; border-bottom: 1px dotted currentColor; }
  .linkhost { opacity: .6; font-size: .85em; }

  /* Copy the link, because selecting a URL that wraps over three lines with a
     thumb is not a thing anybody manages. Click copies the address; hold
     shift and it copies the path or URL as written instead. */
  .cp { font: inherit; font-size: .8em; line-height: 1; cursor: pointer;
        vertical-align: baseline; margin-left: .25em; padding: .1em .3em;
        color: var(--c-muted); background: none;
        border: 1px solid var(--c-line); border-radius: 5px;
        -webkit-tap-highlight-color: transparent; }
  .cp:hover { color: var(--c-text); border-color: var(--c-line-strong); }
  .cp.done { color: #89d185; border-color: #89d185; }

  /* A partial path keeps the shape it was written in, with what it resolves
     to on the line below, so the link is checkable at a glance. */
  .e .md a.pathref .u { display: block; }
  .e .md a.gh { display: block; color: var(--c-accent); font-size: .84em;
             word-break: break-all; opacity: .85; }
  .ghn { color: var(--c-muted); }
  /* A fence full of artefact URLs should wrap and be tappable, not scroll
     off the side of a phone. */
  .e .md pre.code:has(a) { white-space: pre-wrap; word-break: break-all; }
  .e .md pre.code a .u { color: var(--c-accent); font-size: 1em; }
  .e .md a:hover .u { color: var(--c-accent); }
  .e .md hr { border: 0; border-top: 1px solid var(--c-line); margin: .7rem 0; }
  .user { background: #16233a; border-color: #2c4670; }
  .asst { background: #14211a; border-color: #2a4a35; }
  .tool { background: #201a12; border-color: #4a3a22; }
  .out  { background: #101014; border-color: #26262e; color: #b6b6c0; }
  /* Sticky, so it stays reachable while the transcript scrolls under it. */
  #cbar { position: sticky; bottom: 0; display: flex; align-items: center;
          gap: .45rem; padding: .5rem 0; background: var(--c-bg); }
  #cbar input { flex: 1; min-width: 0; max-width: none; border-radius: 10px;
                font: 13px ui-monospace, SFMono-Regular, Menlo, monospace; }
  #cbar input:disabled { opacity: .45; }
  #csent { font-size: .72rem; color: #7a7a83; flex: none; }
</style>
</head>
<body>
<header>
  __HOMELINK__
  <select id="file">__OPTIONS__</select>
  <select id="items">
    <option value="60">last 60</option>
    <option value="200">last 200</option>
    <option value="100000" selected>everything</option>
  </select>
  <button id="pause" type="button">pause</button>
  <button id="expand" type="button">expand all</button>
  <button id="essential" type="button">essential</button>
  <button id="collapse" type="button">collapse all</button>
  <label id="noiselab"><input type="checkbox" id="detailed"> detailed</label>
  <label id="noiselab2"><input type="checkbox" id="noise"> tool calls</label>
  <label id="noiselab3"><input type="checkbox" id="mdbox" checked> render as .md</label>
  <a id="term" href="">terminal</a>
  <label id="alllab"><input type="checkbox" id="all"> all .json in folder</label>
  <span id="meta"></span>
</header>
<div id="out">loading…</div>
<form id="cbar" autocomplete="off">
  <input id="cin" type="text" placeholder="loading…" disabled
         autocapitalize="off" autocorrect="off" spellcheck="false">
  <span id="csent"></span>
</form>
<script>
// Reads codex's own rollout file, not a terminal. The whole session is there,
// so "everything" really is everything — no scrollback limit to run past.


// One handler for every copy button on the page, wherever the markup came
// from: the button carries the link, so nothing has to be looked up. Plain
// click copies the address you would send someone; shift-click copies the
// path or URL as it is written, which is what you want for a local file.
document.addEventListener("click", async (e) => {
  const b = e.target.closest && e.target.closest("button.cp");
  if (!b) return;
  e.preventDefault();
  e.stopPropagation();
  let text = b.dataset.p || "";
  if (!e.shiftKey) {
    try { text = new URL(b.dataset.u, document.baseURI).href; }
    catch (err) { text = b.dataset.u || text; }
  }
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; }
  catch (err) {
    // No clipboard permission, or an insecure context: the old way.
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { ok = document.execCommand("copy"); } catch (e2) {}
    ta.remove();
  }
  b.classList.toggle("done", ok);
  b.textContent = ok ? "\u2713" : "\u2717";
  setTimeout(() => { b.classList.remove("done"); b.textContent = "\u29c9"; }, 1200);
}, true);

// Typeset the maths the renderer set aside. Deliberately after the fact and
// deliberately optional: the server sends the LaTeX source, KaTeX turns it
// into type if it loaded, and if the CDN is unreachable — no internet on the
// tailnet, say — the source stays on screen exactly as it was, readable.
function typeset(root) {
  if (!window.katex) {
    // Say so rather than leaving raw LaTeX in the prose looking like a
    // rendering bug: this used to fail silently whenever the library did
    // not arrive, and "maths does not work" was indistinguishable from
    // "maths was never attempted".
    for (const el of (root || document).querySelectorAll(".math, .imath")) {
      el.classList.add("noks");
      el.title = "KaTeX did not load — showing the LaTeX source";
    }
    return;
  }
  const els = (root || document).querySelectorAll(
    ".math:not([data-tex]), .imath:not([data-tex])");
  for (const el of els) {
    el.dataset.tex = "1";
    let tex = el.textContent.trim();
    const display = el.classList.contains("math");
    // B is one backslash, written as an escape on purpose. These templates
    // are Python strings: a backslash-bracket pair written literally here
    // loses a level on the way out, and JavaScript then reads what is left
    // as a plain bracket. So the delimiter test never matched, nothing was
    // stripped, and KaTeX was handed the delimiters as part of the formula.
    // It rendered that as a parse error -- in red, which on screen looks
    // exactly like maths that was never typeset at all.
    const B = "\\u005c";
    for (const [open, close] of [[B + "[", B + "]"], ["$$", "$$"],
                                 [B + "(", B + ")"]]) {
      if (tex.startsWith(open) && tex.endsWith(close)) {
        tex = tex.slice(open.length, -close.length).trim();
        break;
      }
    }
    try {
      katex.render(tex, el, { displayMode: display, throwOnError: false,
                              output: "html" });
    } catch (e) {
      el.dataset.tex = "err";        // leave the source visible, untouched
    }
  }
}

const out = document.getElementById("out");
const TERMOF = __TERMOF__;
const file = document.getElementById("file");
const items = document.getElementById("items");
const pause = document.getElementById("pause");
const meta = document.getElementById("meta");
const all = document.getElementById("all");
let timer = null, paused = false, atBottom = true;

window.addEventListener("scroll", () => {
  atBottom = (window.innerHeight + window.scrollY) >= document.body.offsetHeight - 60;
});

async function tick() {
  if (paused) return;
  try {
    const r = await fetch("api/codex?file=" + encodeURIComponent(file.value)
                          + "&items=" + items.value
                          + (all.checked ? "&all=1" : "")
                          + "&md=" + (mdBox.checked ? "1" : "0"));
    const j = await r.json();
    if (j.error) { meta.textContent = j.error; return; }
    // Remember which entries the reader had opened, so a refresh doesn't
    // close them; entries are keyed by position in the list.
    const wasOpen = new Set();
    out.querySelectorAll("details.e").forEach((d, i) => { if (d.open) wasOpen.add(i); });
    const hadContent = out.children.length > 0;
    out.innerHTML = j.html;
    if (hadContent) {
      out.querySelectorAll("details.e").forEach((d, i) => {
        if (wasOpen.has(i)) d.open = true;
      });
    }
    typeset(out);
    applyDetailed();
    if (expandAll) applyExpand();
    setTarget(j.target || "");
    meta.textContent = j.total + " items"
                     + (j.files > 1 ? " · " + j.files + " files" : "")
                     + " · " + new Date().toLocaleTimeString();
    if (atBottom) window.scrollTo(0, document.body.scrollHeight);
  } catch (e) {
    meta.textContent = "disconnected";
  }
}
function restart() {
  if (timer) clearInterval(timer);
  tick();
  timer = setInterval(tick, 5000);
  const u = new URL(window.location);
  u.searchParams.set("file", file.value);
  history.replaceState(null, "", u);
}
// The live pane behind this transcript, when it has one.
const termLink = document.getElementById("term");
function syncTerm() {
  const n = TERMOF[file.value];
  termLink.style.display = n ? "" : "none";
  if (n) termLink.href = "logs?name=" + encodeURIComponent(n);
}
file.onchange = () => { syncTerm(); restart(); };
syncTerm();
items.onchange = restart;

// Typing needs a live session behind the file. The server finds one by
// matching this rollout's cwd against a pane running codex; when the run has
// ended the box stays disabled, because the transcript is then just history.
// Sends through /api/type — the same endpoint the /logs command bar uses.
const cin = document.getElementById("cin");
const csent = document.getElementById("csent");
// null, not "": the first update is usually "" (no live session), and an
// early return on an unchanged value would then never replace the initial
// "loading…" placeholder — the box would sit there looking broken.
let target = null;
function setTarget(t) {
  if (t === target) return;
  target = t;
  cin.disabled = !t;
  cin.placeholder = t ? ("send to " + t + " · Enter") : "no live codex session for this transcript";
  if (!t) csent.textContent = "";
}
document.getElementById("cbar").onsubmit = async (e) => {
  e.preventDefault();
  const text = cin.value;
  if (!text.trim() || !target) return;
  cin.disabled = true;
  try {
    const r = await fetch("api/type?name=" + encodeURIComponent(target), {
      method: "POST",
      body: JSON.stringify({text: text}),
    });
    const j = await r.json();
    // Clear only on success, so a rejected line isn't lost from the box.
    if (j.result) { cin.value = ""; csent.textContent = "sent"; }
    else { csent.textContent = j.error || "failed"; }
  } catch (err) {
    csent.textContent = "failed";
  }
  cin.disabled = false;
  cin.focus();
  setTimeout(tick, 900);   // codex takes a moment to append to the rollout
};
all.onchange = restart;
pause.onclick = () => {
  paused = !paused;
  pause.textContent = paused ? "resume" : "pause";
  if (!paused) tick();
};

// Expand-all is sticky across refreshes: a poll every 5s would otherwise slam
// shut whatever you had just opened to read.
const expand = document.getElementById("expand");
const collapse = document.getElementById("collapse");
// "all" is sticky across the 5s refresh; without it the poll would undo
// whatever you just did.
// null = as rendered · true/false = everything · "ess" = only the thread:
// the questions, and the message that ends each turn.
let expandAll = null;
function applyExpand() {
  if (expandAll === null) return;
  out.querySelectorAll("details.e").forEach((d) => {
    d.open = expandAll === "ess" ? d.dataset.ess === "1" : expandAll;
  });
}
expand.onclick = () => { expandAll = true; applyExpand(); };
document.getElementById("essential").onclick = () => {
  expandAll = "ess"; applyExpand();
};
collapse.onclick = () => {
  expandAll = false;
  // Collapse everything, conversation included — that is what "all" means.
  applyExpand();
};

// Tool calls, their output and codex's environment blob: off unless asked
// for, remembered per browser. Shared with the terminal page's history tab,
// which is the same transcript under different chrome.
const noiseBox = document.getElementById("noise");
try { noiseBox.checked = localStorage.getItem("roost.noise") === "1"; }
catch (e) { /* private mode */ }
function applyNoise() {
  out.classList.toggle("noise", noiseBox.checked);
  try { localStorage.setItem("roost.noise", noiseBox.checked ? "1" : "0"); }
  catch (e) { /* not fatal */ }
}
noiseBox.onchange = applyNoise;
applyNoise();

// Reading view by default: entries opened so the text shows, summary hidden
// by CSS. Re-run after each refresh, which replaces the elements.
const detBox = document.getElementById("detailed");
try { detBox.checked = localStorage.getItem("roost.detailed") === "1"; }
catch (e) { /* private mode */ }
function applyDetailed() {
  out.classList.toggle("detailed", detBox.checked);
  if (!detBox.checked)
    out.querySelectorAll("details.e").forEach((d) => { d.open = true; });
  for (const id of ["expand", "essential", "collapse"])
    document.getElementById(id).classList.toggle("hidden", !detBox.checked);
  try { localStorage.setItem("roost.detailed", detBox.checked ? "1" : "0"); }
  catch (e) { /* not fatal */ }
}
detBox.onchange = applyDetailed;

const mdBox = document.getElementById("mdbox");
try {
  const v = localStorage.getItem("roost.md");
  if (v !== null) mdBox.checked = v === "1";
} catch (e) { /* private mode */ }
mdBox.onchange = () => {                 // rendered server-side: refetch
  try { localStorage.setItem("roost.md", mdBox.checked ? "1" : "0"); }
  catch (e) { /* not fatal */ }
  restart();
};
applyDetailed();
restart();
</script>
</body>
</html>
""".replace("__KATEX__", KATEX_TAGS).replace("__INTER__", INTER_CSS).replace("__HOMELINK__", home_link(18))

TERMS_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<link rel="icon" href="icon.png?s=64" type="image/png">
<link rel="apple-touch-icon" href="icon.png?s=180">
<link rel="manifest" href="manifest.webmanifest">
<meta name="theme-color" content="#0e0e11">
<title>terminals</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; padding: .8rem; background: #0e0e11; color: #d8d8de;
         font: 15px/1.45 system-ui, sans-serif; }
  a { color: #7aa7ff; text-decoration: none; font-weight: 650; font-size: 1.05rem; }
  ul { list-style: none; padding: 0; margin: .6rem 0 0; }
  li { border: 1px solid #23232a; background: #141419; border-radius: 12px;
       padding: .7rem .8rem; margin: .5rem 0; }
  .s { font-size: .72rem; color: #8a8a95; margin-left: .4rem; }
  .c { font: 11.5px/1.35 ui-monospace, SFMono-Regular, Menlo, monospace;
       color: #7a7a83; margin-top: .35rem; word-break: break-all; }
  p { color: #7a7a83; font-size: .8rem; }
</style></head><body>
__HOMELINK__
<ul>__ROWS__</ul>
<p>Writable terminals, tailnet-only. Define one with
<code>./term.sh &lt;name&gt; '&lt;command&gt;'</code>.</p>
</body></html>
""".replace("__HOMELINK__", home_link(18))

# The terminal itself is ttyd's page and has no chrome — once you are in it
# there is no way back to the dashboard. Wrapping it in an iframe under a slim
# header keeps a "back" link on screen; same origin and port, so the websocket
# is unaffected.
# The /rc experience for codex: connect and the conversation is already
# there. ttyd alone gives a bare terminal — dtach keeps no scrollback and
# codex does not replay on reconnect — so the transcript is rendered above the
# terminal from codex's own rollout file, and the terminal sits underneath for
# typing. Same origin and port, so the websocket is unaffected.
TERMWRAP_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<link rel="icon" href="icon.png?s=64" type="image/png">
<link rel="apple-touch-icon" href="icon.png?s=180">
<link rel="manifest" href="manifest.webmanifest">
<meta name="theme-color" content="#0e0e11">
<title>__NAME__</title>
__KATEX__
<style>
__INTER__
  /* Scrollbars the way an editor does them: a thin translucent thumb over
     the content, no track, no arrows, and a little more contrast while the
     pointer is on it. Firefox takes the two-property form; WebKit and
     Blink need the pseudo-elements, and the transparent border plus
     background-clip is what insets the thumb rather than letting it touch
     the edge. */
  * { scrollbar-width: thin; scrollbar-color: rgba(255,255,255,.16) transparent; }
  ::-webkit-scrollbar { width: 11px; height: 11px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-corner { background: transparent; }
  ::-webkit-scrollbar-thumb { background: rgba(255,255,255,.16);
      border-radius: 8px; border: 3px solid transparent;
      background-clip: content-box; }
  ::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,.30);
      border: 2px solid transparent; background-clip: content-box; }
  ::-webkit-scrollbar-thumb:active { background: rgba(255,255,255,.42);
      border: 2px solid transparent; background-clip: content-box; }
  /* Claude's own dark palette, read off the app's stylesheet rather than
     eyeballed: gray-850 is the page, gray-800 the raised surface a person's
     turn sits on, gray-50 the text. The web font is theirs and not ours to
     serve, so this is the fallback chain their token names. */
  :root {
    --c-bg: #151515; --c-raised: #20201f; --c-deep: #0b0b0b;
    --c-text: #f0efec; --c-muted: #898781;
    --c-line: rgba(255,255,255,.10); --c-line-strong: rgba(255,255,255,.20);
    --c-accent: #63a8ee; --c-clay: #d97757;
    --c-font: system-ui, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    --c-mono: ui-monospace, SFMono-Regular, Menlo, monospace;
    /* The same face the document viewer reads in: Inter below regular
       weight, which holds its colour on a dark screen where the UI sans at
       UI weight shouts. A conversation is the part you sit and read. */
    --c-read: Inter, system-ui, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    --c-read-ink: #e0ddd7;
  }
  :root { color-scheme: dark; }
  html, body { height: 100%; margin: 0; background: var(--c-bg); color: var(--c-text);
               font: 14px/1.45 system-ui, sans-serif; }
  body { display: flex; flex-direction: column; }
  header { flex: none; display: flex; align-items: center; gap: .5rem .7rem;
           flex-wrap: wrap; padding: .45rem .7rem;
           border-bottom: 1px solid #23232a; }
  a { color: #7aa7ff; text-decoration: none; font-weight: 600; }
  .n { font-weight: 700; }
  .r { margin-left: auto; font-size: .72rem; color: #7a7a83; }
  /* Shown only while the link to the server is down, so a restart reads as
     a blip rather than a dead terminal. */
  #link { font-size: .72rem; font-weight: 600; color: #f0d99a;
          background: #3a3116; border: 1px solid #7a6320;
          border-radius: 999px; padding: .1rem .5rem; }
  button { font: inherit; font-size: .72rem; font-weight: 600; cursor: pointer;
           background: #1b1b1f; color: #e8e8ea; border: 1px solid #33333b;
           border-radius: 999px; padding: .3rem .7rem; }
  /* Tabs, not a split: on a phone two panes at once left neither one usable.
     Both fill the page and only one is shown at a time. */
  #hist { flex: 1 1 auto; overflow-y: auto; padding: .5rem .6rem; }
  #term { flex: 1 1 auto; border: 0; width: 100%; display: block; }
  .gone { display: none !important; }
  /* The reading widths, as in the document viewer: a conversation is prose
     too, and a line that runs the whole of a wide window is as hard to
     follow here as it is there. Only the transcript is affected -- the
     terminal is a grid and takes the window it is given. */
  /* What is left in the bar belongs to the session; the meta text takes the
     space the width buttons used to sit in. */
  #meta { margin-left: auto; }
  #hist > * { max-width: 46rem; margin-inline: auto; }
  body.mid #hist > * { max-width: 64rem; }
  body.wide #hist > * { max-width: none; margin-inline: 0; }

  /* What this session has put on the clipboard, above the conversation.
     Yellow while it is waiting for you; purple once that route sends by
     itself, which is the difference worth seeing at a glance. */
  #replybtn { font: inherit; font-size: .72rem; font-weight: 600;
              background: #14331f; color: #a9e5bd; border: 1px solid #2f6b45;
              border-radius: 999px; padding: .22rem .6rem; cursor: pointer; }
  #toslot { font: inherit; font-size: .72rem; font-weight: 600;
            background: #3a3116; color: #f0d99a; border: 1px solid #7a6320;
            border-radius: 999px; padding: .22rem .5rem; cursor: pointer; }
  #slots { flex: none; display: flex; flex-wrap: wrap; gap: .35rem;
           padding: .35rem .7rem; }
  #slots:empty { display: none; }
  #slots .chip { display: flex; align-items: center; gap: .4rem;
                 border-radius: 999px; padding: .18rem .6rem; cursor: pointer;
                 font-size: .74rem; font-weight: 600;
                 background: #3a3116; color: #f0d99a;
                 border: 1px solid #7a6320; }
  #slots .chip.auto { background: #2e1f3d; color: #d9bcf5;
                      border-color: #6b46a0; }
  #slots .chip .x { opacity: .6; font-weight: 400; }
  #slots .chip:active { filter: brightness(1.25); }
  /* Same hint at the other end, where the gesture is a hold rather than a
     pull: keep the terminal stretched past its last line and it reattaches. */
  #pull.down { top: auto; bottom: 1.4rem; }
  /* Rides above the terminal while you pull down at the top of it. */
  #pull { position: fixed; left: 0; right: 0; top: 2.9rem; z-index: 5;
          display: flex; justify-content: center; pointer-events: none;
          opacity: 0; transition: opacity .12s linear; }
  /* Appears only when the terminal is parked away from the newest output —
     which otherwise just looks like a terminal that stopped. */
  #toend { position: fixed; right: .8rem; bottom: .9rem; z-index: 6;
           background: #2e7d32; color: #fff; border: 1px solid #3f9e45;
           border-radius: 999px; padding: .45rem .9rem; font-weight: 600;
           font-size: .78rem; box-shadow: 0 2px 10px #0008; }
  /* Stood down because another page took the terminal. Over the frame
     rather than instead of it: what was on screen stays readable. */
  #paused { position: absolute; inset: 0; z-index: 7; display: grid;
            place-items: center; background: #0e0e11d8; text-align: center; }
  #paused b { display: block; font-size: 1rem; margin-bottom: .2rem; }
  #paused p { margin: 0 0 .9rem; color: #a0a0aa; font-size: .82rem; }
  #takeover { background: #2e7d32; color: #fff; border: 1px solid #3f9e45;
              border-radius: 999px; padding: .45rem 1.1rem; font-weight: 600;
              font-size: .8rem; }
  #pull span { background: #1b1b1f; border: 1px solid #33333b;
               border-radius: 999px; padding: .25rem .8rem;
               font-size: .72rem; color: #c8c8d2; }
  .tab { border-radius: 999px 999px 999px 999px; }
  .tab.on { background: #2e7d32; border-color: #3f9e45; color: #fff; }
  .e { border-radius: 10px; margin: .35rem 0; border: 1px solid #23232a;
       background: #141419; overflow: hidden; }
  .e > summary { list-style: none; cursor: pointer; padding: .4rem .55rem;
       display: flex; gap: .5rem; align-items: baseline; }
  .e > summary::-webkit-details-marker { display: none; }
  .e > summary::before { content: "▸"; color: #6a6a75; flex: none; }
  .e[open] > summary::before { content: "▾"; }
  .who { font-size: .64rem; text-transform: uppercase; letter-spacing: .06em;
         color: #8a8a95; flex: none; }
  .head { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;
          white-space: nowrap; color: #b8b8c2;
          font: 11.5px/1.3 ui-monospace, SFMono-Regular, Menlo, monospace; }
  .size { flex: none; font-size: .64rem; color: #6a6a75; }
  .e[open] .head { display: none; }
  .e pre { margin: 0; padding: 0 .6rem .5rem; white-space: pre-wrap;
           word-break: break-word;
           font: 12px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; }
  /* Machinery is hidden until asked for. In a real session that is 2452 of
     2823 entries -- the transcript is unreadable with them in. */
  #hist:not(.noise) .e[data-k="tool"],
  #hist:not(.noise) .e[data-k="out"],
  #hist:not(.noise) .e[data-k="env"],
  #hist:not(.noise) .e[data-k="think"] { display: none; }
  /* Reading view, the default: no summary line, no disclosure arrow, no
     border and no rounding -- plain blocks of text butted together, each
     keeping a background colour so you can still see where a turn starts.
     Assistant text sits on the page's own black. "detailed" restores the
     transcript chrome. */
  #hist:not(.detailed) .e > summary { display: none; }
  #hist:not(.detailed) .e { border: 0; border-radius: 0; margin: 0; }
  #hist:not(.detailed) .e .md { padding: .1rem .6rem .5rem; }
  #hist:not(.detailed) .e pre { padding: .5rem .7rem;
      /* Prose, not console: the monospace grid is for the terminal, and
         this is the part you sit and read. */
      font: .95rem/1.68 var(--c-read); font-weight: 350;
      color: var(--c-read-ink); }
  #hist:not(.detailed) .asst { background: var(--c-bg); }
  #hist:not(.detailed) .asst pre, #hist:not(.detailed) .asst .md { color: var(--c-text); }
  /* A person's turn as a bubble against the right edge, the way the web
     conversation reads: the band across the full width made every question
     look like a section heading. Text inside stays left-aligned — only the
     box moves. */
  /* Clear air on both sides: in a wall of assistant prose the bubble has
     to announce itself as the moment someone spoke. */
  #hist:not(.detailed) .user { background: none; margin: 1.45rem 0; }
  #hist:not(.detailed) .user .md,
  #hist:not(.detailed) .user pre { background: var(--c-raised);
      color: var(--c-text); border-radius: 16px; padding: .5rem .85rem;
      width: fit-content; max-width: 85%; margin-left: auto; }
  #hist:not(.detailed) .asst { margin-bottom: .3rem; }
  #noiselab, #noiselab2, #noiselab3 { font-size: .72rem; color: #9a9aa6; display: flex;
              align-items: center; gap: .3rem; }
  #noiselab input, #noiselab2 input, #noiselab3 input { margin: 0; }
  .e[data-ess="1"] > summary .who { color: #d8b45a; }
  /* Rendered markdown. The table scrolls inside its own box: a wide table
     must not make the whole page scroll sideways on a phone. */
  .e .md { padding: .1rem .7rem .55rem; color: var(--c-read-ink);
           font: .95rem/1.68 var(--c-read); font-weight: 350;
           -webkit-font-smoothing: antialiased;
           -moz-osx-font-smoothing: grayscale; }
  /* One step up from the text, not three. */
  .e .md strong, .e .md b { font-weight: 600; color: #f0eeea; }
  .e .md h1, .e .md h2, .e .md h3, .e .md h4 {
      font-weight: 600; color: #f0eeea; }
  .e .md > :first-child { margin-top: 0; }
  .e .md > :last-child { margin-bottom: 0; }
  .e .md p, .e .md ul, .e .md ol, .e .md blockquote { margin: .45rem 0; }
  .e .md h1, .e .md h2, .e .md h3, .e .md h4, .e .md h5, .e .md h6 {
      margin: .6rem 0 .3rem; font-size: 1.02em; font-weight: 700; }
  .e .md ul, .e .md ol { padding-left: 1.2rem; }
  .e .md li { margin: .15rem 0; }
  .e .md table { border-collapse: collapse; margin: .5rem 0; font-size: .93em;
                 display: block; max-width: 100%; overflow-x: auto; }
  .e .md th, .e .md td { border: 1px solid var(--c-line-strong); padding: .3rem .55rem;
                         text-align: left; vertical-align: top;
                         overflow-wrap: break-word; }
  .e .md th { background: rgba(255,255,255,.06); font-weight: 600; }
  .e .md code { background: rgba(255,255,255,.08); border-radius: 4px;
                padding: .05rem .3rem; font: .855em/1.4 var(--c-mono); }
  .e .md pre.code { background: var(--c-deep); border: 1px solid var(--c-line);
                    border-radius: 8px; margin: .55rem 0; padding: .5rem .6rem;
                    overflow-x: auto; white-space: pre;
                    font: 13px/1.5 var(--c-mono); }
  .e .md blockquote { border-left: 2px solid var(--c-line-strong);
                      padding-left: .7rem; color: var(--c-muted); }
  /* Display maths is a block of its own here too, so that a formula the
     typesetter could not reach still reads as a formula. */
  .math, .imath { font: 13px/1.5 var(--c-mono); }
  /* Only as wide as the formula. A full-width band for a six-character
     inequality reads as a section break rather than as one line of maths;
     fit-content sizes the box to what is in it, and the cap plus the scroll
     handle a formula wider than the column. */
  .math { display: block; width: fit-content; max-width: 100%;
          white-space: pre-wrap; overflow-x: auto;
          background: var(--c-deep); border: 1px solid var(--c-line);
          border-radius: 8px; margin: .55rem 0; padding: .5rem .6rem; }
  /* KaTeX centres display maths. In a document of left-aligned prose that
     leaves each formula stranded in the middle of a wide line, disconnected
     from the sentence that introduces it, so it is set flush left like
     everything else. The outer .math box keeps the horizontal scroll for a
     formula wider than the column. */
  .math .katex-display,
  .math .katex-display > .katex { text-align: left; }
  .math .katex-display { margin: 0; }
  .noks { border-color: var(--c-clay) !important; opacity: .85; }
  .e .md a { color: var(--c-accent); }
  .e .md a.fileref::after { content: " \\2197"; font-size: .85em; opacity: .7; }
  .e .md, .e .md p, .e .md li, .e .md td { overflow-wrap: anywhere; }
  .e .md a .u { color: var(--c-muted); font-size: .84em; overflow-wrap: anywhere; }
  /* Where codex cut the thread. Its own "Conversation recap" is drawn by
     the TUI and never written to the rollout, so the words cannot be shown
     -- but the seam can, and a jump in the conversation then has a reason. */
  .e.mark { display: flex; align-items: center; gap: .6rem; border: 0;
            margin: .9rem 0; padding: 0; background: none;
            color: var(--c-muted); font-size: .72rem; letter-spacing: .04em;
            text-transform: uppercase; }
  .e.mark::before, .e.mark::after { content: ""; flex: 1 1 auto;
            border-top: 1px solid var(--c-line); }

  /* A link whose address is on the hover rather than on the page says so
     with a dotted underline; the button beside it copies the address. */
  a.named { text-decoration: none; border-bottom: 1px dotted currentColor; }
  .linkhost { opacity: .6; font-size: .85em; }

  /* Copy the link, because selecting a URL that wraps over three lines with a
     thumb is not a thing anybody manages. Click copies the address; hold
     shift and it copies the path or URL as written instead. */
  .cp { font: inherit; font-size: .8em; line-height: 1; cursor: pointer;
        vertical-align: baseline; margin-left: .25em; padding: .1em .3em;
        color: var(--c-muted); background: none;
        border: 1px solid var(--c-line); border-radius: 5px;
        -webkit-tap-highlight-color: transparent; }
  .cp:hover { color: var(--c-text); border-color: var(--c-line-strong); }
  .cp.done { color: #89d185; border-color: #89d185; }

  /* A partial path keeps the shape it was written in, with what it resolves
     to on the line below, so the link is checkable at a glance. */
  .e .md a.pathref .u { display: block; }
  .e .md a.gh { display: block; color: var(--c-accent); font-size: .84em;
             word-break: break-all; opacity: .85; }
  .ghn { color: var(--c-muted); }
  /* A fence full of artefact URLs should wrap and be tappable, not scroll
     off the side of a phone. */
  .e .md pre.code:has(a) { white-space: pre-wrap; word-break: break-all; }
  .e .md pre.code a .u { color: var(--c-accent); font-size: 1em; }
  .e .md a:hover .u { color: var(--c-accent); }
  .e .md hr { border: 0; border-top: 1px solid var(--c-line); margin: .7rem 0; }
  .user { background: #16233a; border-color: #2c4670; }
  .asst { background: #14211a; border-color: #2a4a35; }
  .tool { background: #201a12; border-color: #4a3a22; }
  .out  { background: #101014; border-color: #26262e; color: #b6b6c0; }
</style></head><body>
<header>
  __HOMELINK__
  <span class="n">@__NAME__</span>
  <button id="tterm" class="tab on" type="button">terminal</button>
  <button id="thist" class="tab" type="button">history</button>
  <button id="copy" type="button" title="copy what is selected in the terminal">copy</button>
  <button id="shot" type="button" title="send a picture to this session">img</button>
  <input id="shotfile" type="file" accept="image/*" hidden>
  <button id="diag" type="button" title="terminal state / send a trace">·</button>
  <button id="exp" class="gone" type="button">expand all</button>
  <button id="ess" class="gone" type="button">essential</button>
  <button id="col" class="gone" type="button">collapse all</button>
  <label id="noiselab" class="gone"><input type="checkbox" id="detailed">
    detailed</label>
  <label id="noiselab2" class="gone"><input type="checkbox" id="noise">
    tool calls</label>
  <label id="noiselab3" class="gone"><input type="checkbox" id="mdbox" checked>
    render as .md</label>
  __HIST__
  <button id="replybtn" class="gone" type="button"></button>
  <select id="toslot" title="forward this session's last answer">
    <option value="">↪ Forward to…</option>
  </select>
  <span class="r" id="meta">…</span>
  <span id="link" class="gone">reconnecting…</span>
</header>
<div id="slots"></div>
<div id="pull"><span>pull for history</span></div>
<button id="toend" class="gone" type="button">↓ live</button>
<div id="paused" class="gone"><div>
  <b>paused</b>
  <p>another page is reading this terminal.</p>
  <button id="takeover" type="button">read it here</button>
</div></div>
<!-- No src until this page has claimed the terminal. Claiming disconnects
     whoever held it, and connecting first would only mean being disconnected
     by our own claim. -->
<iframe id="term" data-src="term/?arg=__NAME__" title="__NAME__"></iframe>
<div id="hist" class="gone">loading conversation…</div>
<script>
// The transcript comes from codex's rollout file, so it is the real
// conversation rather than terminal scrollback — which dtach does not keep.
const FILE = __FILE__;
const hist = document.getElementById("hist");
const meta = document.getElementById("meta");
// A path in a transcript opens in a window of its own. This tab is the
// session — the terminal is live in it — and following a file link out of
// here used to replace it, so you came back to a reconnect instead of the
// place you were reading. External links already carry target=_blank; the
// local ones are deliberately plain so the file viewer can follow them in
// place, which leaves this the one page that has to say otherwise.
hist.addEventListener("click", (e) => {
  const a = e.target.closest && e.target.closest("a.fileref, a.pathref");
  if (!a || e.button || e.metaKey || e.ctrlKey || e.shiftKey) return;
  e.preventDefault();
  const w = window.open(a.href, "_blank");   // not "noopener": returns null
  if (!w) window.location = a.href;          // actually blocked: go there
});
let atBottom = true;
hist.addEventListener("scroll", () => {
  atBottom = hist.scrollTop + hist.clientHeight >= hist.scrollHeight - 40;
});
const PAGE = 150;            // messages pulled per scroll-up
// An ABSOLUTE index into the transcript, not "the last N". Requesting the tail
// meant every new entry pushed an old one off the top — history visibly
// vanished as soon as you typed. Anchored here, new entries extend the view
// and what you were reading stays put.
let first = null;
let more = 0, loadingOlder = false;

let lastTotal = -1;

async function tick(force) {
  if (!FILE) { hist.innerHTML = "<em>no transcript for this session</em>"; return; }
  // The transcript is half a megabyte of HTML; rebuilding it on a timer while
  // the tab is hidden was stealing frames from the terminal next to it.
  if (!force && hist.classList.contains("gone")) return;
  try {
    if (!force) {
      // Ask what changed before asking for it. ~60 bytes against ~500KB.
      const c = await fetch("api/codex?file=" + encodeURIComponent(FILE)
                            + "&count=1" + convArg());
      const cj = await c.json();
      if (cj.total === lastTotal) return;
    }
    const q = ((first === null) ? "&items=" + PAGE
                               : "&from=" + first + "&items=100000")
              + "&md=" + (mdBox.checked ? "1" : "0") + convArg();
    const r = await fetch("api/codex?file=" + encodeURIComponent(FILE) + q);
    const j = await r.json();
    if (j.error) { meta.textContent = j.error; return; }
    const open = new Set();
    hist.querySelectorAll("details.e").forEach((d, i) => { if (d.open) open.add(i); });
    const had = hist.querySelector("details.e") !== null;
    hist.innerHTML = j.html;
    if (had) hist.querySelectorAll("details.e").forEach((d, i) => {
      if (open.has(i)) d.open = true;
    });
    typeset(hist);
    applyDetailed();   // the reading view re-opens what the refresh replaced
    applyAll();        // an explicit expand/collapse survives the refresh
    // First load lands on the tail; remember where that window starts so
    // every later poll asks for the same anchor onwards.
    lastTotal = j.total || 0;
    if (first === null) first = Math.max(0, (j.total || 0) - PAGE);
    more = (j.from !== null && j.from !== undefined) ? j.from : (j.more || 0);
    const hid = hist.querySelectorAll('.e[data-k="tool"], .e[data-k="out"],'
                                     + ' .e[data-k="env"]').length;
    meta.textContent = (j.total || 0) + " items"
      + (more ? " · " + more + " older" : "")
      + (hid && !noiseBox.checked ? " · " + hid + " hidden" : "");
    if (atBottom) hist.scrollTop = hist.scrollHeight;
  } catch (e) { meta.textContent = "disconnected"; }
}

// Scroll up to backfill, the way Claude Code does it: the terminal has no
// scrollback, so older turns come from the transcript on demand rather than
// from the pty. Anchor on scrollHeight so the view does not jump when older
// entries are prepended above what you are reading.
async function older() {
  if (loadingOlder || !more || !FILE) return;
  loadingOlder = true;
  meta.textContent = "loading older…";
  const before = hist.scrollHeight, top = hist.scrollTop;
  first = Math.max(0, first - PAGE);
  await tick(true);
  hist.scrollTop = top + (hist.scrollHeight - before);
  loadingOlder = false;
}
hist.addEventListener("scroll", () => {
  if (hist.scrollTop < 60) older();
});

tick(); setInterval(tick, 5000);
// expand/collapse all. null = leave entries as the renderer set them, which
// is conversation open and tool calls closed.
// null = as the renderer set it (conversation open, tool calls shut);
// true/false = all; "ess" = only what carries the thread — the questions and
// the message that ends each turn, which is where codex says what came of it.
let allState = null;
function applyAll() {
  if (allState === null) return;
  hist.querySelectorAll("details.e").forEach((d) => {
    d.open = allState === "ess" ? d.dataset.ess === "1" : allState;
  });
}
document.getElementById("exp").onclick = () => { allState = true; applyAll(); };
document.getElementById("ess").onclick = () => { allState = "ess"; applyAll(); };
document.getElementById("col").onclick = () => { allState = false; applyAll(); };

// Tool calls, their output and the environment blob: off unless asked for,
// remembered per browser so it is a decision made once.
const noiseBox = document.getElementById("noise");
try { noiseBox.checked = localStorage.getItem("roost.noise") === "1"; }
catch (e) { /* private mode */ }
// Machinery is dropped by the server, so PAGE counts messages. Asking for
// it back is a refetch, not a class toggle.
function convArg() { return noiseBox.checked ? "" : "&conv=1"; }

function applyNoise() {
  hist.classList.toggle("noise", noiseBox.checked);
  lastTotal = -1;          // the count means something different now
  first = null;            // and so does the window anchor
  if (typeof tick === "function") tick(true);
  try { localStorage.setItem("roost.noise", noiseBox.checked ? "1" : "0"); }
  catch (e) { /* not fatal */ }
}
noiseBox.onchange = applyNoise;
applyNoise();

// Detailed off is the reading view: every entry is opened so its text shows,
// and CSS hides the summary line. It has to run after each render, because
// the open flag lives on elements the refresh replaces.
const detBox = document.getElementById("detailed");
try { detBox.checked = localStorage.getItem("roost.detailed") === "1"; }
catch (e) { /* private mode */ }
function applyDetailed() {
  hist.classList.toggle("detailed", detBox.checked);
  if (!detBox.checked)
    hist.querySelectorAll("details.e").forEach((d) => { d.open = true; });
  syncControls();
  try { localStorage.setItem("roost.detailed", detBox.checked ? "1" : "0"); }
  catch (e) { /* not fatal */ }
}
detBox.onchange = applyDetailed;

// Markdown is rendered on the server, so switching it needs a refetch rather
// than a class toggle. Default on: tables and bold are most of why the
// transcript is worth reading at all.
const mdBox = document.getElementById("mdbox");
try {
  const v = localStorage.getItem("roost.md");
  if (v !== null) mdBox.checked = v === "1";
} catch (e) { /* private mode */ }
mdBox.onchange = () => {
  try { localStorage.setItem("roost.md", mdBox.checked ? "1" : "0"); }
  catch (e) { /* not fatal */ }
  tick(true);
};

// expand/essential/collapse act on open state, which the reading view does
// not have; the checkboxes belong to the history tab either way.
function syncControls() {
  const onTerm = hist.classList.contains("gone");
  for (const el of [expBtn, colBtn, document.getElementById("ess")])
    el.classList.toggle("gone", onTerm || !detBox.checked);
  for (const id of ["noiselab", "noiselab2", "noiselab3"])
    document.getElementById(id).classList.toggle("gone", onTerm);
}

// Terminal and history are two views of the same session, one at a time.
// Terminal is the default: this page exists to type into codex, and the
// history is what you consult. The iframe is hidden rather than removed, so
// switching tabs never drops the websocket or re-attaches dtach.
const term = document.getElementById("term");
const tabTerm = document.getElementById("tterm");
const tabHist = document.getElementById("thist");
const expBtn = document.getElementById("exp");
const colBtn = document.getElementById("col");

function show(which) {
  const t = which === "term";
  // The tab lives in the URL, so a refresh, a bookmark or a link back comes
  // up on the view you were reading rather than on the terminal.
  try {
    const u = new URL(window.location);
    if (t) u.searchParams.delete("tab");
    else u.searchParams.set("tab", "hist");
    history.replaceState(null, "", u);
  } catch (e) { /* not fatal */ }
  term.classList.toggle("gone", !t);
  hist.classList.toggle("gone", t);
  tabTerm.classList.toggle("on", t);
  tabHist.classList.toggle("on", !t);
  // expand/collapse act on the transcript, so they only belong to that tab.
  syncControls();
  if (t) { termToEnd(); return; }
  tick(true).then(() => { if (atBottom) hist.scrollTop = hist.scrollHeight; });
}
tabTerm.onclick = () => show("term");
tabHist.onclick = () => show("hist");
const START = new URLSearchParams(location.search).get("tab") === "hist"
            ? "hist" : "term";
// After the controls it reaches: syncControls() touches expBtn and colBtn,
// which are const and would still be in the dead zone further up.
applyDetailed();
if (START === "hist") show("hist");     // ?tab=hist, from a refresh or a link

// Scroll the terminal to the newest output. xterm.js keeps its own scrolling
// viewport inside the iframe, so this reaches into that element directly
// rather than through ttyd's JS, which does not expose the terminal.
// Same origin and port, so the reach is allowed.
function viewport() {
  try {
    return term.contentDocument
        && term.contentDocument.querySelector(".xterm-viewport");
  } catch (e) { return null; }   // not loaded yet
}
function termToEnd() {
  const v = viewport();
  if (v) v.scrollTop = v.scrollHeight;
}

// dtach redraws the pane on attach (-r winch), and that redraw lands in
// pieces over the first few seconds — one scroll on load would fire before
// the output it is supposed to scroll past. Nudge it repeatedly at first,
// then let the observer below take over.
function chaseEnd() {
  let n = 0, mine = -1;
  const startedAt = Date.now();
  const id = setInterval(() => {
    tuneTerminal();
    // Give up the moment the reader takes over. Standing down for 1.2s and
    // then yanking again is worse than not chasing at all: it fights every
    // scroll for the whole five seconds.
    if (termTouching || lastTermTouch > startedAt) { clearInterval(id); return; }
    const v = viewport();
    // And give up on the evidence rather than on being told. A wheel over
    // the terminal lands on .xterm-screen, which is a SIBLING of the
    // scrolling element -- so a listener on the viewport never hears it and
    // lastTermTouch stays where it was while the view is being scrolled.
    // The position does not lie: if it is above where this left it, someone
    // moved it, and chasing the end from here is yanking it back.
    if (v && mine >= 0 && v.scrollTop < mine - 8) { clearInterval(id); return; }
    termToEnd();
    if (v) mine = v.scrollTop;
    if (++n > 20) clearInterval(id);
  }, 250);
}
// xterm keeps 1000 lines and throws the rest away, which is about fifteen
// page-ups and then the conversation simply stops. ttyd never sets the
// option, and raising it there would mean restarting ttyd -- which would
// take down the dtach masters it fathered, and those masters ARE the
// sessions. The live instance takes it just as well, so it is set from out
// here, as early as the terminal exists and before its first output.
const SCROLLBACK = 20000;
function setScrollback() {
  try {
    const x = term.contentWindow && term.contentWindow.term;
    if (!x || !x.options) return false;
    if (x.options.scrollback !== SCROLLBACK) x.options.scrollback = SCROLLBACK;
    return true;
  } catch (e) { return false; }
}
// ttyd builds the terminal some time after the frame loads, so this waits
// for it rather than assuming it. Twenty milliseconds apart for three
// seconds: the cost is a property read, and losing the race means losing
// the top of the conversation.
function chaseScrollback(n) {
  if (setScrollback() || (n || 0) > 150) return;
  setTimeout(() => chaseScrollback((n || 0) + 1), 20);
}
term.addEventListener("load", () => chaseScrollback(0));
chaseScrollback(0);

term.addEventListener("load", chaseEnd);
if (term.contentDocument && term.contentDocument.readyState === "complete") chaseEnd();

// --- paths in the terminal become links --------------------------------
// A path in the transcript has been a link for a while; the same path in
// the terminal was just text, and a path on a phone is not something you
// retype. ttyd keeps its xterm instance at window.term and the iframe is
// same-origin, so an extra link provider can be registered from out here
// without patching ttyd or rewriting its stream.
//
// Only under the roots the file viewer will actually serve: underlining a
// path that opens nothing is worse than leaving it as text.
const FILE_ROOTS = __ROOTDIRS__, HOME = __HOME__, CWD = __CWD__;
const TERM_PATH = /(?:~|[/])[A-Za-z0-9._+@%-]*(?:[/][A-Za-z0-9._+@%~-]+)+[/]?/g;
// Built rather than written: a quote inside a quoted string in a template
// that is itself a Python string loses an escape on the way out.
const TRAILING = ".,;:!?)]}>" + String.fromCharCode(34, 39);

// The extensions worth offering. Deliberately a list rather than "a dot and
// some letters": that matched "0.5", "e.g" and every version number on the
// screen, and a terminal underlined half its own output.
const TERM_EXT = ("md|markdown|txt|rst|tex|json|jsonl|yaml|yml|toml|csv|tsv|"
                  + "ini|cfg|conf|lock|log|py|js|mjs|ts|tsx|jsx|sh|bash|c|h|"
                  + "cpp|hpp|rs|go|java|rb|pl|sql|html|css|xml|patch|diff|"
                  + "lean|hs|ml|mli|nix|tf|proto|jl|kt|swift|scala|bib|sty|"
                  + "sv|vhd|zig|ipynb|"
                  + "png|jpg|jpeg|gif|svg|webp|pdf");
// A bare name or a relative path: the directory part is optional and
// greedy, so "Added tmp/codex-01a06fd2-t42/.../NOTES.md" links the
// whole path rather than just its last component.
const TERM_NAME = new RegExp(
  "\\\\b(?:[A-Za-z0-9][A-Za-z0-9._+-]*/)*"
  + "[A-Za-z0-9][A-Za-z0-9._+-]*\\\\.(?:" + TERM_EXT + ")\\\\b", "g");

const nameCache = new Map();         // name or relative path -> absolute, or ""

function resolveName(name) {
  if (nameCache.has(name)) return Promise.resolve(nameCache.get(name));
  const u = new URL("api/find", document.baseURI);
  u.searchParams.set("name", name);
  u.searchParams.set("base", CWD);
  return fetch(u.href)
    .then((r) => (r.ok ? r.json() : null))
    .then((j) => {
      const hit = (j && j.path) || "";
      nameCache.set(name, hit);
      return hit;
    }, () => "");
}

function openPath(abs) {
  // Absolute, from this page's own base. A relative URL here resolved
  // against the IFRAME -- the click starts inside ttyd, even though this
  // function belongs to the page around it -- and "file?path=..." became
  // "/term/file?path=...", which is ttyd's 404 rather than the viewer.
  const u = new URL("file", document.baseURI);
  u.searchParams.set("path", abs);
  const w = window.open(u.href, "_blank");
  if (!w) window.location = u.href;
}

function underRoot(p) {
  for (const r of FILE_ROOTS) if (p === r || p.indexOf(r + "/") === 0) return true;
  return false;
}

function addPathLinks(win) {
  let t = null;
  try { t = win && win.term; } catch (e) { return false; }   // not loaded yet
  if (!t || !t.registerLinkProvider || t._roostPaths) return !!(t && t._roostPaths);
  t._roostPaths = true;
  t.registerLinkProvider({
    provideLinks(y, cb) {
      try { findLinks(t, y, cb); }
      catch (e) { cb(undefined); }     // never leave xterm mid-gesture
    },
  });
  return true;
}

function findLinks(t, y, cb) {
      const buf = t.buffer.active;
      let first = y - 1;
      if (!buf.getLine(first)) { cb(undefined); return; }
      // A path wraps at the edge of a narrow terminal — which on a phone is
      // most of them — so the whole logical line is what gets searched, and
      // a match is mapped back to the row and column it started on.
      while (first > 0 && buf.getLine(first).isWrapped) first--;
      const rows = [];
      for (let r = first; ; r++) {
        const ln = buf.getLine(r);
        if (!ln) break;
        rows.push(ln.translateToString(false));
        const nx = buf.getLine(r + 1);
        if (!nx || !nx.isWrapped) break;
      }
      const text = rows.join("");
      const offs = [];
      let acc = 0;
      for (const r of rows) { offs.push(acc); acc += r.length; }
      const at = (i) => {
        for (let k = rows.length - 1; k >= 0; k--)
          if (i >= offs[k]) return { x: i - offs[k] + 1, y: first + k + 1 };
        return { x: 1, y: first + 1 };
      };
      const out = [], taken = [];
      TERM_PATH.lastIndex = 0;
      let m;
      while ((m = TERM_PATH.exec(text))) {
        let hit = m[0];
        // The full stop that ends the sentence is not part of the name.
        while (hit && TRAILING.indexOf(hit[hit.length - 1]) >= 0)
          hit = hit.slice(0, -1);
        if (hit.length < 4) continue;
        const abs = hit[0] === "~" ? HOME + hit.slice(1) : hit;
        if (!underRoot(abs)) continue;
        taken.push([m.index, m.index + hit.length]);
        out.push({
          range: { start: at(m.index), end: at(m.index + hit.length - 1) },
          text: abs,
          activate() { openPath(abs); },
        });
      }
      // Bare names next -- "Added DESIGN-NOTES.md (+91 -0)". A name with
      // no folder on it means nothing by itself, so the server resolves it
      // against the folder this session runs in (and the folders below it,
      // the same search the transcript's links use). Answers are cached,
      // misses included: an unhelpful name should cost one request, not one
      // per hover.
      const pend = [];
      if (CWD) {
        TERM_NAME.lastIndex = 0;
        while ((m = TERM_NAME.exec(text))) {
          const name = m[0], from = m.index, to = from + name.length;
          if (taken.some((t) => from < t[1] && to > t[0])) continue;
          pend.push({ name: name, from: from, to: to });
        }
      }
      if (!pend.length) { cb(out.length ? out : undefined); return; }
      Promise.all(pend.map((q) => resolveName(q.name))).then((paths) => {
        pend.forEach((q, i) => {
          const abs = paths[i];
          if (!abs) return;
          out.push({
            range: { start: at(q.from), end: at(q.to - 1) },
            text: abs,
            activate() { openPath(abs); },
          });
        });
        cb(out.length ? out : undefined);
      }, () => cb(out.length ? out : undefined));
}

// --- reading width -----------------------------------------------------
// Set in the document viewer, which has the three buttons, and honoured here
// so a transcript reads the way documents do. There is nothing to press in
// this bar: a line that runs the whole of a wide window is as hard to follow
// in a conversation as in a document, and one choice covers both. Only the
// transcript moves; the terminal is a grid and takes whatever it is given.
let width = "read";
try {
  const w = localStorage.getItem("roost.width");
  if (w === "read" || w === "mid" || w === "wide") width = w;
} catch (e) {}
document.body.classList.toggle("mid", width === "mid");
document.body.classList.toggle("wide", width === "wide");

// --- staying connected -------------------------------------------------
// The terminal's websocket is relayed through roost, so every roost restart
// takes it down. ttyd tries to come back on its own, but badly for this
// case: its retry is immediate and unconditional, so the socket it opens
// while the server is still down fails -- and its error handler sets
// doReconnect = false. The terminal then parks on "Press ⏎ to Reconnect"
// and stays there. Waiting for a person to press a key is not a reconnect.
//
// So this watches from out here. It polls the server, and brings the frame
// back when the server has returned, or has returned as a DIFFERENT server,
// which is what a restart makes it -- that second test catches a restart
// quick enough that no poll ever saw it down. dtach still holds the
// session, so what comes back is the same screen.
let lastServerId = null, serverDown = false, lastRevive = 0;
const linkChip = document.getElementById("link");

function termOverlayText() {
  // ttyd's overlay is a bare <div> appended to the terminal element, with
  // no class of its own; its text is the whole of its state.
  try {
    const el = term.contentDocument && term.contentDocument.querySelector(".xterm");
    if (!el) return "";
    for (const n of el.children)
      if (n.tagName === "DIV" && !n.className) return n.textContent || "";
  } catch (e) {}
  return "";
}

function reviveTerm() {
  // Not while another page is reading it. Being disconnected is the point
  // of standing down, so bringing the connection back is the watchdog
  // undoing the handover -- and then the reader poll pauses it again, and
  // the two take turns for as long as both pages are open.
  if (typeof standingDown !== "undefined" && standingDown) return;
  if (Date.now() - lastRevive < 5000) return;   // one attempt at a time
  lastRevive = Date.now();
  try { term.contentWindow.location.reload(); }
  catch (e) { term.src = term.src; }            // cross-document: reload it
}

async function watchLink() {
  let j = null;
  try {
    const r = await fetch("api/ping", { cache: "no-store" });
    j = r.ok ? await r.json() : null;
  } catch (e) { j = null; }
  if (!j) {
    serverDown = true;
    linkChip.classList.remove("gone");
    return;
  }
  const restarted = lastServerId !== null && j.id !== lastServerId;
  const wasDown = serverDown;
  lastServerId = j.id;
  serverDown = false;
  // Stuck on ttyd's own dead end, whatever the server has been doing.
  const stuck = /Reconnect|Connection Closed/.test(termOverlayText());
  if (wasDown || restarted || stuck) {
    linkChip.classList.remove("gone");
    reviveTerm();
    setTimeout(() => linkChip.classList.add("gone"), 1500);
  } else {
    linkChip.classList.add("gone");
  }
}
setInterval(watchLink, 4000);
watchLink();

// --- copying out of the terminal ---------------------------------------
// ttyd runs xterm with the webgl renderer, so what is on screen is painted,
// not markup: there is no DOM text for the browser to select, which is why
// the right-click Copy is greyed out and why Ctrl-C goes to the program as
// an interrupt rather than to the clipboard. xterm keeps its own selection,
// though, and hands it over on request -- so copying is a thing this page
// has to do, not something the browser can be left to.
//
// Select with the mouse as usual, then this button or Ctrl-Shift-C.
const copyBtn = document.getElementById("copy");

function termSelection() {
  try {
    const t = term.contentWindow && term.contentWindow.term;
    return (t && t.getSelection && t.getSelection()) || "";
  } catch (e) { return ""; }
}

function mouseGrabbed() {
  // Claude Code turns on mouse reporting so it can handle clicks itself.
  // xterm then hands the drag to the program instead of selecting with it --
  // which is why a codex terminal selects on a plain drag and a Claude one
  // does not. Shift is the documented way past it.
  try {
    const t = term.contentWindow && term.contentWindow.term;
    const m = t && t.modes && t.modes.mouseTrackingMode;
    return !!m && m !== "none";
  } catch (e) { return false; }
}

async function copySelection() {
  const text = termSelection();
  if (!text) {
    flashCopy(mouseGrabbed() ? "select with shift-drag" : "nothing selected");
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    flashCopy("copied " + text.length + " chars");
  } catch (e) {
    // No clipboard permission (an insecure context, or a browser that wants
    // a gesture it did not see): fall back to the old trick.
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (err) {}
    ta.remove();
    flashCopy(ok ? "copied " + text.length + " chars" : "could not copy");
  }
}

let copyTimer = null;
function flashCopy(msg) {
  copyBtn.textContent = msg;
  clearTimeout(copyTimer);
  copyTimer = setTimeout(() => { copyBtn.textContent = "copy"; }, 1600);
}
copyBtn.onclick = copySelection;

// Ctrl-Shift-C inside the frame as well: the terminal has focus while you
// are reading it, and reaching for a button to copy is the part that makes
// people give up and retype.
function bindCopyKey(win) {
  try {
    const d = win && win.document;
    if (!d || d._roostCopyKey) return false;
    d._roostCopyKey = true;
    d.addEventListener("keydown", (e) => {
      if (e.ctrlKey && e.shiftKey && (e.key === "C" || e.key === "c")) {
        e.preventDefault();
        e.stopPropagation();
        copySelection();
      }
    }, true);
    return true;
  } catch (e) { return false; }
}

// --- pasting a picture -------------------------------------------------
// xterm pastes text and nothing else: its paste handler reads text/plain and
// stops, so an image on the clipboard has never reached a browser terminal.
// The image is uploaded instead, and its path typed into the session without
// a newline -- the agent reads files, so a path is the form it can act on,
// and you get to say what to do with it before pressing Enter.
const shot = document.getElementById("shot");
const shotFile = document.getElementById("shotfile");

async function sendImage(file) {
  if (!file) return;
  flashCopy("sending picture…");
  try {
    const body = await file.arrayBuffer();
    const r = await fetch("api/paste?name=" + encodeURIComponent(NAME), {
      method: "POST",
      headers: { "content-type": file.type || "image/png" },
      body: body,
    });
    const j = await r.json();
    if (j.path) {
      flashCopy("pasted " + j.path.split("/").pop());
      show("term");                     // watch it land
    } else {
      flashCopy(j.error || "could not send");
    }
  } catch (e) {
    flashCopy("could not send");
  }
}

function imageFrom(ev) {
  const items = (ev.clipboardData && ev.clipboardData.items) || [];
  for (const it of items)
    if (it.kind === "file" && (it.type || "").indexOf("image/") === 0)
      return it.getAsFile();
  return null;
}

function handlePaste(ev) {
  const f = imageFrom(ev);
  if (!f) return;                       // text: xterm's business, not ours
  ev.preventDefault();
  ev.stopPropagation();
  sendImage(f);
}

document.addEventListener("paste", handlePaste, true);

function bindPaste(win) {
  try {
    const d = win && win.document;
    if (!d || d._roostPaste) return false;
    d._roostPaste = true;
    d.addEventListener("paste", handlePaste, true);
    return true;
  } catch (e) { return false; }
}

// A phone has no Ctrl-V worth the name: the button opens the camera roll.
shot.onclick = () => shotFile.click();
shotFile.onchange = () => {
  sendImage(shotFile.files && shotFile.files[0]);
  shotFile.value = "";                  // so the same picture can go twice
};

// --- telling the dashboard what this screen is showing -------------------
// A codex session that has stopped to ask something looks exactly like one
// that has finished: the transcript stops growing either way, and codex
// records no event for the question. The screen is the only place it exists.
//
// This reads the screen -- not the private files behind it, which are
// codex's own business and change without notice -- and it reads it from
// out here rather than from inside the terminal's data path. The relay in
// the server carries the same bytes and could be tapped instead, with the
// same coverage (both need a tab open), but a bug in a reader must not be
// able to break the terminal it is reading.
//
// Nothing polls. xterm says when the screen changed; that is debounced, and
// only the last dozen rows of the VIEWPORT are looked at -- not the
// scrollback. A report goes out only when the answer changes.
const ASKING = [
  "\\\\([yY]/[nN]\\\\)", "\\\\[[yY]/[nN]\\\\]",
  "\\\\b[yY]\\\\s*/\\\\s*[nN]\\\\b",
  "\\\\bdo you (?:want|trust|wish)\\\\b",
  "\\\\ballow\\\\b[^?]*\\\\?", "\\\\bapprove\\\\b",
  "\\\\bpress\\\\s+(?:enter|y)\\\\b",
  "(?:^|\\\\s)1\\\\.\\\\s[\\\\s\\\\S]{0,80}?(?:^|\\\\s)2\\\\.\\\\s",
  "\\\\bhang tight\\\\b", "\\\\bretry with\\\\b",
].map((p) => new RegExp(p, "i"));
const SCREEN_TAIL = 12;
let screenSaid = null, screenTimer = null;

function screenState(win) {
  const t = win && win.term;
  if (!t || !t.buffer) return null;
  const buf = t.buffer.active;
  const top = buf.viewportY || 0;
  const rows = [];
  for (let y = Math.max(0, top + t.rows - SCREEN_TAIL); y < top + t.rows; y++) {
    const l = buf.getLine(y);
    if (!l) continue;
    const txt = l.translateToString(true).trim();
    if (txt) rows.push(txt);
  }
  // The composer is chrome, not content, and it is always the last line --
  // but codex draws its OPTIONS behind the same marker:
  //     \u203a 1. Retry with a faster model  2. Dismiss and keep waiting
  // Dropping every line that starts with one threw away the question. So a
  // line is chrome only if what follows the marker is the empty placeholder.
  const MARKER = new RegExp("^[\\\\u258c\\\\u2502>\\\\u203a]\\\\s*", "");
  const EMPTY = new RegExp(
      "^(?:ask anything|send a message|type a message|\\\\s*)$", "i");
  while (rows.length) {
    const last = rows[rows.length - 1];
    if (!MARKER.test(last) || !EMPTY.test(last.replace(MARKER, ""))) break;
    rows.pop();
  }
  if (!rows.length) return { asking: false, text: "" };
  const tail = rows.slice(-6).join(" ");
  // Either it looks like a prompt, or the settled screen simply ends on a
  // question -- which is the same thing said in words.
  const asking = ASKING.some((rx) => rx.test(tail))
              || /[?]\\s*$/.test(rows[rows.length - 1]);
  // The question itself: the last line that ends in one, else the last line
  // with words in it.
  let text = "";
  for (let i = rows.length - 1; i >= 0 && !text; i--)
    if (rows[i].indexOf("?") >= 0) text = rows[i];
  return { asking: asking, text: (text || rows[rows.length - 1]).slice(0, 200) };
}

function reportScreen() {
  let w = null;
  try { w = term.contentWindow; } catch (e) { return; }
  const st = screenState(w);
  if (!st) return;
  const key = (st.asking ? "1" : "0") + st.text;
  if (key === screenSaid) return;           // nothing new to say
  screenSaid = key;
  try {
    fetch("api/screen?name=" + encodeURIComponent(NAME),
          { method: "POST", body: JSON.stringify(st) });
  } catch (e) { screenSaid = null; }
}

function watchScreen(win) {
  const t = win && win.term;
  if (!t || !t.onRender || t._roostScreen) return false;
  t._roostScreen = true;
  t.onRender(() => {
    clearTimeout(screenTimer);
    screenTimer = setTimeout(reportScreen, 1500);   // when it settles
  });
  setTimeout(reportScreen, 2000);
  return true;
}

function chasePathLinks() {
  let n = 0;
  const id = setInterval(() => {
    let w = null;
    try { w = term.contentWindow; } catch (e) {}
    const done = addPathLinks(w);
    bindCopyKey(w);
    bindPaste(w);
    watchScreen(w);
    if (done || ++n > 60) clearInterval(id);
  }, 250);
}
term.addEventListener("load", chasePathLinks);
chasePathLinks();

// --- touch scrolling inside the terminal -------------------------------
// ttyd's page is not built for a phone: the viewport scrolls, but the
// browser treats the gesture as ambiguous and there is no feedback when you
// reach the end. Both are fixed from out here by injecting into the iframe
// (same origin), so ttyd itself stays stock.
let termTouching = false, lastTermTouch = 0;

// --- live capture, for when scrolling still misbehaves ------------------
// Off unless the URL says ?trace=1. Records every touch event and samples
// the viewport every 32ms through the gesture and for 2s after the finger
// leaves — which is the whole question: does scrollTop keep moving once you
// let go? Posted to the server as one record per gesture.
let TRACE = location.search.indexOf("trace=1") >= 0;
let traceBuf = [], traceFlush = null, traceSampler = null;



// One handler for every copy button on the page, wherever the markup came
// from: the button carries the link, so nothing has to be looked up. Plain
// click copies the address you would send someone; shift-click copies the
// path or URL as it is written, which is what you want for a local file.
document.addEventListener("click", async (e) => {
  const b = e.target.closest && e.target.closest("button.cp");
  if (!b) return;
  e.preventDefault();
  e.stopPropagation();
  let text = b.dataset.p || "";
  if (!e.shiftKey) {
    try { text = new URL(b.dataset.u, document.baseURI).href; }
    catch (err) { text = b.dataset.u || text; }
  }
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; }
  catch (err) {
    // No clipboard permission, or an insecure context: the old way.
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { ok = document.execCommand("copy"); } catch (e2) {}
    ta.remove();
  }
  b.classList.toggle("done", ok);
  b.textContent = ok ? "\u2713" : "\u2717";
  setTimeout(() => { b.classList.remove("done"); b.textContent = "\u29c9"; }, 1200);
}, true);

// Typeset the maths the renderer set aside. Deliberately after the fact and
// deliberately optional: the server sends the LaTeX source, KaTeX turns it
// into type if it loaded, and if the CDN is unreachable — no internet on the
// tailnet, say — the source stays on screen exactly as it was, readable.
function typeset(root) {
  if (!window.katex) {
    // Say so rather than leaving raw LaTeX in the prose looking like a
    // rendering bug: this used to fail silently whenever the library did
    // not arrive, and "maths does not work" was indistinguishable from
    // "maths was never attempted".
    for (const el of (root || document).querySelectorAll(".math, .imath")) {
      el.classList.add("noks");
      el.title = "KaTeX did not load — showing the LaTeX source";
    }
    return;
  }
  const els = (root || document).querySelectorAll(
    ".math:not([data-tex]), .imath:not([data-tex])");
  for (const el of els) {
    el.dataset.tex = "1";
    let tex = el.textContent.trim();
    const display = el.classList.contains("math");
    // B is one backslash, written as an escape on purpose. These templates
    // are Python strings: a backslash-bracket pair written literally here
    // loses a level on the way out, and JavaScript then reads what is left
    // as a plain bracket. So the delimiter test never matched, nothing was
    // stripped, and KaTeX was handed the delimiters as part of the formula.
    // It rendered that as a parse error -- in red, which on screen looks
    // exactly like maths that was never typeset at all.
    const B = "\\u005c";
    for (const [open, close] of [[B + "[", B + "]"], ["$$", "$$"],
                                 [B + "(", B + ")"]]) {
      if (tex.startsWith(open) && tex.endsWith(close)) {
        tex = tex.slice(open.length, -close.length).trim();
        break;
      }
    }
    try {
      katex.render(tex, el, { displayMode: display, throwOnError: false,
                              output: "html" });
    } catch (e) {
      el.dataset.tex = "err";        // leave the source visible, untouched
    }
  }
}

const BUILD = "__BUILD__", NAME = "__NAME__";
const diag = document.getElementById("diag");
function termState() {
  let doc = null;
  try { doc = term.contentDocument; } catch (e) { return "cross-origin"; }
  if (!doc) return "no doc";
  const v = doc.querySelector(".xterm-viewport");
  if (!v) return doc.readyState === "complete" ? "no viewport" : "loading";
  return v.dataset.roostTuned ? "tuned" : "found, untuned";
}
setInterval(() => {
  diag.textContent = BUILD + " · " + termState() + (TRACE ? " · rec" : "");
}, 1000);
diag.onclick = () => {                 // tap it to start recording
  TRACE = true;
  trace("armed");
  flushTrace();
};

function doc0() { try { return term.contentDocument; } catch (e) { return null; } }
function traceEnv() {
  const v = viewport();
  if (!v) return { state: termState(), href: term.getAttribute("src"),
                   readyState: (() => {
                     try { return term.contentDocument
                             ? term.contentDocument.readyState : "null"; }
                     catch (e) { return "blocked"; } })() };
  const cs = term.contentWindow.getComputedStyle(v);
  return { state: termState(),
           touchAction: cs.touchAction, overflowY: cs.overflowY,
           overscrollY: cs.overscrollBehaviorY,
           scrollHeight: v.scrollHeight, clientHeight: v.clientHeight,
           scrollable: v.scrollHeight - v.clientHeight,   // 0 = nothing to do
           scrollTop: Math.round(v.scrollTop),
           rows: (doc0() || {}).querySelectorAll
                 ? doc0().querySelectorAll(".xterm-rows > div").length : null,
           tuned: !!v.dataset.roostTuned };
}
function trace(kind) {
  if (!TRACE) return;
  let v = null;
  try { v = viewport(); } catch (e) { v = null; }
  traceBuf.push({ t: Math.round(performance.now()), kind,
                  top: v ? Math.round(v.scrollTop) : null });
  if (traceBuf.length > 600) traceBuf.shift();
  clearTimeout(traceFlush);
  traceFlush = setTimeout(flushTrace, 2500);
  if (!traceSampler) {
    traceSampler = setInterval(() => {
      if (!termTouching && Date.now() - lastTermTouch > 2000) {
        clearInterval(traceSampler); traceSampler = null; return;
      }
      trace("sample");
    }, 32);
  }
}
// Reports itself once, a few seconds in, so a diagnosis does not depend on
// anyone reading a label off a phone screen and typing it back.
setTimeout(async () => {
  try {
    await fetch("api/trace", { method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ kind: "load", build: BUILD, name: document.title,
                             ua: navigator.userAgent, env: traceEnv(),
                             dpr: window.devicePixelRatio,
                             win: [window.innerWidth, window.innerHeight] }) });
  } catch (e) { /* the page works without this */ }
}, 4000);

async function flushTrace() {
  if (!traceBuf.length) return;
  const body = { name: document.title, ua: navigator.userAgent,
                 env: traceEnv(), samples: traceBuf };
  traceBuf = [];
  try {
    await fetch("api/trace", { method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body) });
    meta.textContent = "trace sent";
  } catch (e) { meta.textContent = "trace failed: " + e; }
}

function tuneTerminal() {
  const doc = term.contentDocument, v = viewport();
  if (!doc || !v || v.dataset.roostTuned) return;
  v.dataset.roostTuned = "1";
  const st = doc.createElement("style");
  st.textContent = ".xterm-viewport {"
    + "  -webkit-overflow-scrolling: touch;"   // momentum on iOS
    + "  overscroll-behavior: contain;"        // no page pull-to-refresh
    + "  touch-action: pan-y;"                 // pan at once, no tap delay
    + "  scrollbar-width: thin; }"
    + ".xterm { transition: transform .3s cubic-bezier(.2,.85,.25,1); }"
    + ".xterm.roost-pull { transition: none; }";
  doc.head.appendChild(st);
  // Scroll events are the other half of the evidence: touch events say what
  // the finger did, these say what the element did about it.
  v.addEventListener("scroll", () => trace("scroll"), { passive: true });
  // A wheel is someone scrolling, the same as a finger. Without this the
  // end-chase only deferred to touch, so on a desktop the view was dragged
  // back to the last line while you were reading.
  // Caught on the document, in the capture phase, because that is where the
  // event actually passes: xterm paints .xterm-screen OVER .xterm-viewport
  // as a sibling, so a wheel lands on the screen and never reaches the
  // element that scrolls. A listener on the viewport hears nothing, which
  // is how the end-chase came to believe nobody was reading.
  doc.addEventListener("wheel", () => { lastTermTouch = Date.now(); },
                       { capture: true, passive: true });
  rubberBand(doc, v);
  accelWheel(doc);
}

// Wheel acceleration, and only where the wheel is slow.
//
// The two kinds of terminal handle it differently. In a dtach one the wheel
// scrolls xterm's own buffer -- native, fast, nothing to improve. In a tmux
// one there is no buffer to scroll: tmux is on the alternate screen, so the
// wheel is reported to tmux, which scrolls a few lines of its own history
// per event. One round trip per tick, which is what makes it feel heavy.
//
// So: when the viewport has nothing to scroll -- exactly the tmux case --
// each real tick becomes several. It starts near normal and builds while you
// keep turning, then decays, so a nudge stays a nudge and a spin covers
// ground. Synthetic events are ignored on the way back in (isTrusted is
// false for them), which is what stops it feeding itself.
function accelWheel(doc) {
  let last = 0, run = 0;
  doc.addEventListener("wheel", (e) => {
    if (!e.isTrusted) return;
    const v = viewport();
    if (!v || v.scrollHeight > v.clientHeight + 1) return;   // xterm's job
    e.stopPropagation();
    const now = performance.now();
    run = (now - last < 140) ? Math.min(run + 1, 4) : 0;
    last = now;
    for (let i = 0; i < 2 + run; i++) {
      e.target.dispatchEvent(new WheelEvent("wheel", {
        deltaY: e.deltaY, deltaX: e.deltaX, deltaMode: e.deltaMode,
        bubbles: true, cancelable: true }));
    }
  }, { capture: true, passive: true });
}

// Touch handling, and where it has to live. The finger never lands on the
// element that scrolls: xterm paints .xterm-screen over .xterm-viewport as a
// SIBLING, so the touch target is not a descendant of the scroller. That is
// why xterm scrolls it by hand in a touchmove handler, why the browser can
// give no momentum here (the scrollable box is not in the target's ancestor
// chain), and why everything attached to the viewport saw nothing at all.
//
// So: listen on the document in the capture phase, which runs before xterm's
// own listener whatever the target is. xterm keeps doing the 1:1 drag — it
// works, and it feels direct — and the fling below is added after release.
function rubberBand(doc, v) {
  const box = doc.querySelector(".xterm") || v.parentNode;
  const MAX = 76, DAMP = .34, TRIP = 46;
  // Pulling up past the end is the other half of the gesture. Distance is
  // the wrong trigger there -- the end of a terminal is where a flick
  // naturally lands -- so this one is a HOLD: keep it stretched for a beat
  // and the terminal reattaches and redraws, which is the fix for a pane
  // that has gone stale behind a reconnect.
  const HOLD_MS = 1200, HOLD_PX = 10;
  const hint = document.getElementById("pull");
  const hintText = hint.firstElementChild;
  let y0 = 0, pull = 0, holdTimer = null, armed = false;

  const setHint = (px) => {
    const down = px < 0;
    hint.classList.toggle("down", down);
    hint.style.opacity = px === 0 ? 0 : Math.min(1, Math.abs(px) / TRIP);
    if (down) {
      hintText.textContent = armed ? "release to refresh" : "hold to refresh";
    } else {
      hintText.textContent = px >= TRIP ? "release for history"
                                        : "pull for history";
    }
  };

  const holdOff = () => {
    clearTimeout(holdTimer); holdTimer = null; armed = false;
  };
  const holdOn = () => {
    if (holdTimer || armed) return;
    holdTimer = setTimeout(() => {
      holdTimer = null;
      if (pull <= -HOLD_PX) { armed = true; setHint(pull); }
    }, HOLD_MS);
  };
  const opts = { capture: true, passive: true };

  doc.addEventListener("touchstart", (e) => {
    if (flinging) { cancelAnimationFrame(flinging); flinging = null; }
    vel = [];
    termTouching = true; y0 = e.touches[0].clientY; pull = 0;
    vel.push({ t: performance.now(), y: y0 });
    trace("touchstart");
  }, opts);

  doc.addEventListener("touchmove", (e) => {
    const y = e.touches[0].clientY, dy = y - y0;
    vel.push({ t: performance.now(), y });
    if (vel.length > 6) vel.shift();
    trace("touchmove");
    const atTop = v.scrollTop <= 0;
    const atEnd = v.scrollHeight - v.scrollTop - v.clientHeight <= 1;
    if ((atTop && dy > 0) || (atEnd && dy < 0)) {
      pull = Math.max(-MAX, Math.min(MAX, dy * DAMP));
      box.classList.add("roost-pull");        // follow the finger, no easing
      box.style.transform = "translateY(" + pull + "px)";
      if (pull <= -HOLD_PX) holdOn(); else holdOff();
      setHint(pull);
    } else if (pull) {
      pull = 0; holdOff(); box.style.transform = ""; setHint(0);
    }
  }, opts);

  const release = () => {
    termTouching = false; lastTermTouch = Date.now();
    const tripped = pull >= TRIP;
    const refresh = armed;
    pull = 0; holdOff();
    box.classList.remove("roost-pull");       // spring back under the easing
    box.style.transform = "";
    setHint(0);
    trace("touchend");
    if (tripped) { show("hist"); return; }
    if (refresh) { flashCopy("refreshing…"); reviveTerm(); return; }
    fling(v);
  };
  doc.addEventListener("touchend", release, opts);
  doc.addEventListener("touchcancel", release, opts);
}

// Momentum, computed from the finger. Velocity cannot come from scroll
// events here — those fire on an element the gesture never touches — so it
// is measured off the touch positions and converted: dragging the finger up
// moves the view down, which is the sign flip below.
let vel = [], flinging = null;

function fling(v) {
  if (flinging) { cancelAnimationFrame(flinging); flinging = null; }
  const s = vel; vel = [];
  if (s.length < 2) return;
  const a = s[0], b = s[s.length - 1];
  const dt = b.t - a.t;
  if (dt < 8 || dt > 400) return;             // stale samples: not a flick
  let speed = -(b.y - a.y) / dt;              // px of scrollTop per ms
  if (Math.abs(speed) < 0.15) return;         // a slow drag is not a flick
  speed = Math.max(-6, Math.min(6, speed));
  // xterm rounds scrollTop to whole rows behind our back:
  //     const e = buffer.ydisp * this._currentRowHeight;
  //     if (this._viewportElement.scrollTop !== e) ... scrollTop = e;
  // so the position has to be kept here as a float and never read back. It
  // also means a write worth less than a row moves nothing while still
  // costing a repaint — which is the stutter at the end of a coast. So: only
  // write once a full row has accumulated, and stop while still moving
  // faster than a third of a row per frame, rather than grinding to zero.
  const row = rowHeight(v);
  let pos = v.scrollTop, written = pos, last = performance.now();
  const max = v.scrollHeight - v.clientHeight;
  const step = (now) => {
    const d = Math.min(48, now - last); last = now;
    speed *= Math.pow(0.9965, d);             // ~ iOS deceleration
    pos = Math.max(0, Math.min(max, pos + speed * d));
    if (termTouching || pos <= 0 || pos >= max
        || Math.abs(speed) * 16 < row * .34) {
      if (Math.abs(pos - written) >= 1) v.scrollTop = Math.round(pos);
      flinging = null; return;                // hit an end, or slow enough
    }
    if (Math.abs(pos - written) >= row) {     // a whole row of travel
      v.scrollTop = Math.round(pos);          // absolute: no drift
      written = pos;
    }
    flinging = requestAnimationFrame(step);
  };
  flinging = requestAnimationFrame(step);
}

// One text row, in pixels. xterm keeps the number to itself, but it sizes a
// hidden measuring span to exactly one cell, which is the same thing.
function rowHeight(v) {
  let h = 0;
  try {
    const doc = term.contentDocument;
    const m = doc && doc.querySelector(".xterm-char-measure-element");
    if (m) h = m.getBoundingClientRect().height;
  } catch (e) { h = 0; }
  return (h > 4 && h < 60) ? h : 17;          // a sane default if it moves
}

// Keep following new output. A DOM observer would miss it — ttyd draws to a
// canvas, so output changes no nodes — and xterm's own scroll-on-output can
// be left behind by a redraw, so this polls the viewport instead. Only acts
// when the view is already near the bottom, so scrolling up to read
// something does not get yanked back down — and never during or just after a
// touch, which would fight the inertia mid-flick.
// The clipboard chips. Polled with everything else, and clicking one sends
// it — the slot holds a result until a person decides where it goes, which
// is the whole point of the arrangement.
// "Convert the last response into a slot": pick a target and it is captured
// here, waiting. The picker lists the other sessions because a person is
// choosing — this is the one place a destination is named, and no bot sees
// it.
const toSlot = document.getElementById("toslot");
(async () => {
  try {
    const r = await fetch("api/peers");
    for (const p of await r.json()) {
      if (p.name === NAME) continue;
      const o = document.createElement("option");
      o.value = p.name;
      o.textContent = "↪ @" + p.name + (p.kind === "codex" ? " (codex)" : "");
      toSlot.appendChild(o);
    }
  } catch (e) { /* the rest of the page does not depend on this */ }
})();
toSlot.onchange = async () => {
  const to = toSlot.value;
  toSlot.value = "";
  if (!to) return;
  try {
    const r = await fetch("api/slot/dump?from=" + encodeURIComponent(NAME)
                          + "&to=" + encodeURIComponent(to), { method: "POST" });
    const j = await r.json();
    const m = document.getElementById("msg");
    if (m) m.textContent = j.result || j.error;
    refreshSlots();
  } catch (e) { /* nothing captured; the chips will not change */ }
};

const replyBtn = document.getElementById("replybtn");
replyBtn.onclick = async () => {
  const to = replyBtn.dataset.to;
  if (!to) return;
  const r = await fetch("api/slot/dump?from=" + encodeURIComponent(NAME)
                        + "&to=" + encodeURIComponent(to), { method: "POST" });
  const j = await r.json();
  const m = document.getElementById("msg");
  if (m) m.textContent = j.result || j.error;
  refreshSlots();
};

const slotsEl = document.getElementById("slots");
async function refreshSlots() {
  try {
    const r = await fetch("api/slots?owner=" + encodeURIComponent(NAME));
    const d = await r.json();
    const auto = new Set(d.auto || []);
    // Whoever handed this session its work is where the answer goes back,
    // and it was learned when the handover was sent — so returning it is one
    // click here, with nothing to choose and nothing to set up.
    replyBtn.classList.toggle("gone", !d.reply);
    if (d.reply) {
      replyBtn.textContent = "↩ Reply to @" + d.reply;
      replyBtn.title = "answer @" + d.reply + ", who forwarded this work here";
      replyBtn.dataset.to = d.reply;
    }
    slotsEl.innerHTML = "";
    for (const s of d.slots || []) {
      const chip = document.createElement("div");
      const sticky = auto.has(s.from + ">" + s.to);
      chip.className = "chip" + (sticky ? " auto" : "");
      chip.title = s.chars + " characters from @" + s.from
                 + (sticky ? " — auto-forwarded on this route"
                           : " — click to forward");
      // ↪ forwarded on your say-so, ⇉ forwarded by itself. The icon is the
      // difference; the colour repeats it for anyone who reads colour first.
      const label = document.createElement("span");
      label.textContent = (sticky ? "⇉ Auto-fw @" : "↪ Fwd @") + s.to
                        + " · " + s.at;
      const x = document.createElement("span");
      x.className = "x";
      x.textContent = "✕";
      x.onclick = async (e) => {
        e.stopPropagation();
        await fetch("api/slot/drop?id=" + encodeURIComponent(s.id), { method: "POST" });
        refreshSlots();
      };
      chip.onclick = async () => {
        chip.textContent = "sending…";
        const res = await fetch("api/slot/send?id=" + encodeURIComponent(s.id),
                                { method: "POST" });
        const j = await res.json();
        document.getElementById("msg") &&
          (document.getElementById("msg").textContent = j.result || j.error);
        refreshSlots();
      };
      chip.append(label, x);
      slotsEl.appendChild(chip);
    }
  } catch (e) { /* the page works without them */ }
}
refreshSlots();
setInterval(refreshSlots, 5000);

const toEnd = document.getElementById("toend");
toEnd.onclick = () => {
  if (flinging) { cancelAnimationFrame(flinging); flinging = null; }
  termToEnd();
  toEnd.classList.add("gone");
};

setInterval(() => {
  tuneTerminal();     // cheap once done; re-arms if ttyd rebuilds the terminal
  setScrollback();    // a property read, unless ttyd built a fresh terminal
  // Scrolled up, the terminal stops following output on purpose — but with
  // no sign of that it reads as a terminal that froze. Offer the way back.
  const vv = viewport();
  const away = !!vv && vv.scrollHeight - vv.scrollTop - vv.clientHeight > 40;
  const onTerm = hist.classList.contains("gone");   // history hidden
  toEnd.classList.toggle("gone", !onTerm || !away);
  if (termTouching || Date.now() - lastTermTouch < 1200) return;
  const v = viewport();
  // >40px from the end means the reader parked it there — leave it alone.
  if (v && v.scrollHeight - v.scrollTop - v.clientHeight < 40) termToEnd();
}, 1500);

// --- one reader at a time ----------------------------------------------
// The session has a single pty and a single size, and every attached client
// sets that size when it connects. Two pages open on the same session hand
// the geometry back and forth, and the one not holding it draws its last
// rows at the wrong height -- the clipped composer, the garbled bottom.
//
// There is nothing to gain from two live readers, so there is one: opening
// or refreshing a page claims the terminal, and the page that held it
// before drops its connection and says so. Dropping it is the point -- a
// connection that stays open keeps a size, whether anyone is watching or
// not. Taking it back is one click, or a refresh.
const TERM_SRC = term.dataset.src;
const pausedEl = document.getElementById("paused");
const takeover = document.getElementById("takeover");
const CID = Math.random().toString(36).slice(2) + Date.now().toString(36);
let standingDown = false;

function claimURL(path) {
  const u = new URL(path, document.baseURI);
  u.searchParams.set("name", NAME);
  u.searchParams.set("cid", CID);
  return u.href;
}

async function takeTerm() {
  try { await fetch(claimURL("api/term-claim"), { method: "POST" }); }
  catch (e) { /* offline: connect anyway, there is no one to hand it to */ }
  standingDown = false;
  pausedEl.classList.add("gone");
  // Always after the claim, first load included: the claim disconnects the
  // clients that were there, and this connection has to be the one left.
  // Unconditionally, and from blank: comparing term.src is no good -- it
  // reads back absolute while TERM_SRC is relative -- and a connection that
  // was already opening has to be dropped rather than raced.
  term.src = "about:blank";
  setTimeout(() => { term.src = TERM_SRC; }, 60);
}

function standDown() {
  if (standingDown) return;
  standingDown = true;
  pausedEl.classList.remove("gone");
  term.src = "about:blank";          // closes the websocket, frees the pty
}

async function checkReader() {
  let cid = null;
  try {
    const u = new URL("api/term-reader", document.baseURI);
    u.searchParams.set("name", NAME);
    const r = await fetch(u.href, { cache: "no-store" });
    cid = r.ok ? (await r.json()).cid : null;
  } catch (e) { return; }            // server down: watchLink handles that
  if (!cid) { takeTerm(); return; }  // nobody holds it — after a restart
  if (cid !== CID) standDown();
}

takeover.onclick = takeTerm;
takeTerm();
// Often, because the gap between losing the terminal and saying so is a
// gap in which the watchdog sees a dead connection and reconnects it. The
// request is a name and a short string.
setInterval(checkReader, 1500);
</script>
</body></html>
""".replace("__KATEX__", KATEX_TAGS).replace("__INTER__", INTER_CSS).replace("__HOMELINK__", home_link(18))

# ---------------------------------------------------------------- app icon
# A remote control with two signal waves coming off it — the thing this page
# actually is. Drawn here rather than shipped as a file: the stdlib has no
# image library and no font, so the shapes come from signed-distance
# functions and the edges are anti-aliased from the distance itself. One
# drawing serves the favicon, the iOS home-screen icon and the manifest.

ICON_BG = (46, 125, 50)      # the dashboard's green
ICON_FG = (244, 244, 246)
ICON_RED = (209, 73, 63)

def _cov(d, px_per_unit):
    """Distance (in drawing units) -> pixel coverage, 0..1."""
    return min(1.0, max(0.0, 0.5 - d * px_per_unit))

def _sd_rrect(x, y, cx, cy, hx, hy, r):
    qx, qy = abs(x - cx) - hx + r, abs(y - cy) - hy + r
    return (min(max(qx, qy), 0.0)
            + math.hypot(max(qx, 0.0), max(qy, 0.0)) - r)

def _sd_circle(x, y, cx, cy, r):
    return math.hypot(x - cx, y - cy) - r

def _sd_arc(x, y, cx, cy, rad, half, k):
    """A ring of radius `rad`, kept only inside an upward cone of slope k —
    so it reads as a wave leaving the remote rather than a full circle."""
    dx, dy = x - cx, y - cy
    d = abs(math.hypot(dx, dy) - rad) - half
    n = math.hypot(k, 1.0)
    return max(d, (dy + k * dx) / n, (dy - k * dx) / n)

def _icon_shapes(x, y):
    """The glyph, as (distance, colour) in order back to front."""
    return (
        (_sd_arc(x, y, .46, .335, .215, .021, .62), ICON_FG),
        (_sd_arc(x, y, .46, .335, .145, .021, .62), ICON_FG),
        (_sd_rrect(x, y, .46, .655, .13, .275, .085), ICON_FG),
        (_sd_circle(x, y, .46, .465, .050), ICON_RED),
        (_sd_circle(x, y, .405, .585, .028), ICON_BG),
        (_sd_circle(x, y, .515, .585, .028), ICON_BG),
        (_sd_circle(x, y, .405, .675, .028), ICON_BG),
        (_sd_circle(x, y, .515, .675, .028), ICON_BG),
        (_sd_circle(x, y, .405, .765, .028), ICON_BG),
        (_sd_circle(x, y, .515, .765, .028), ICON_BG),
    )

def _png(rgba, w, h):
    raw = b"".join(b"\x00" + bytes(rgba[y * w * 4:(y + 1) * w * 4])
                   for y in range(h))
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))

_ICON_CACHE = {}

def icon_png(size, maskable=False):
    """`maskable` fills the square and insets the glyph: Android crops a
    home-screen icon to whatever shape the launcher uses, so the rounded
    corners have to go and the drawing needs room to lose."""
    key = (size, maskable)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    n = float(size)
    scale = .78 if maskable else 1.0
    radius = 0.0 if maskable else .22
    buf = bytearray(size * size * 4)
    o = 0
    for j in range(size):
        v = (j + .5) / n
        for i in range(size):
            u = (i + .5) / n
            a = _cov(_sd_rrect(u, v, .5, .5, .5, .5, radius), n)
            r, g, b = ICON_BG
            if a > 0:
                x, y = .5 + (u - .5) / scale, .5 + (v - .5) / scale
                if .09 < y < .96 and .17 < x < .76:   # glyph bounds: skip most
                    for d, col in _icon_shapes(x, y):
                        c = _cov(d, n * scale)
                        if c > 0:
                            r += (col[0] - r) * c
                            g += (col[1] - g) * c
                            b += (col[2] - b) * c
            buf[o] = int(r + .5); buf[o + 1] = int(g + .5)
            buf[o + 2] = int(b + .5); buf[o + 3] = int(a * 255 + .5)
            o += 4
    _ICON_CACHE[key] = _png(buf, size, size)
    return _ICON_CACHE[key]

MANIFEST = json.dumps({
    "name": "roost — remote control",
    "short_name": "roost",
    "start_url": ".",
    "scope": ".",
    "display": "standalone",
    "background_color": "#0e0e11",
    "theme_color": "#0e0e11",
    "icons": [
        {"src": "icon.png?s=192", "sizes": "192x192", "type": "image/png"},
        {"src": "icon.png?s=512", "sizes": "512x512", "type": "image/png"},
        {"src": "icon.png?s=512&m=1", "sizes": "512x512", "type": "image/png",
         "purpose": "maskable"},
    ],
})


# Shown inside the terminal iframe when the ttyd sub-path is not mapped on
# this port.
TERM_MISSING = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>terminal not mapped</title>
<style>
  body { margin: 0; padding: 1rem; background: #151515; color: #f0efec;
         font: 15px/1.6 system-ui, "Segoe UI", Roboto, sans-serif; }
  code { background: rgba(255,255,255,.08); border-radius: 4px;
         padding: .1rem .35rem; font: 13px/1.4 ui-monospace, Menlo, monospace;
         word-break: break-all; }
  p { max-width: 46rem; }
  .m { color: #898781; }
</style></head><body>
<p><strong>The terminal service is not mapped on this address.</strong></p>
<p class="m">ttyd is proxied at <code>/term</code> by tailscale serve, and
<code>__HOST__</code> has no mapping for it — so this request reached the
dashboard instead, which has no such page.</p>
<p>Add it, on the machine running roost:</p>
<p><code>cd ~/src/roost &amp;&amp; ./term.sh</code></p>
<p class="m">That re-adds the mapping on both the https and the plain-http
port. The transcript in the <em>history</em> tab does not depend on it and
works either way.</p>
</body></html>
"""

def with_nonce(page, extra=""):
    """(page, policy): the page's own <script> and <style> tags carry a fresh
    nonce, and only they may run. Everything a page shows from a file or a
    transcript is escaped, so a literal "<script>" can only be the
    template's. Style attributes stay allowed -- ANSI colours, table cells
    and KaTeX are drawn with them, from server-built values."""
    import secrets
    n = secrets.token_urlsafe(18)
    page = (page.replace("<script>", '<script nonce="%s">' % n)
                .replace("<style>", '<style nonce="%s">' % n))
    csp = ("default-src 'none'; base-uri 'none'; object-src 'none'; "
           "form-action 'none'; frame-ancestors 'self'; "
           "script-src 'self' 'nonce-%s'; style-src-elem 'self' 'nonce-%s'; "
           "style-src-attr 'unsafe-inline'; img-src 'self' data:; "
           "font-src 'self'; connect-src 'self'; manifest-src 'self'; "
           "frame-src 'self'%s" % (n, n, extra))
    return page, csp

class Handler(BaseHTTPRequestHandler):
    # A socket timeout while the request is read: without one, a client that
    # never finishes its headers holds a thread forever, and the identity
    # check only runs once they are in. term_relay clears it on the sockets
    # it takes over, so an idle terminal is unaffected.
    timeout = 30

    def _send(self, code, body, ctype="application/json"):
        csp = None
        if ctype == "text/html":
            body, csp = with_nonce(body)
        data = body.encode()
        self.send_response(code)
        if csp:
            self.send_header("Content-Security-Policy", csp)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._no_framing()
        self.end_headers()
        self.wfile.write(data)

    def _no_framing(self):
        # A page on another site must not be able to frame this one: a click
        # inside the frame is a same-origin request, so same_site() would
        # pass it, and a framed terminal takes keystrokes.
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _send_bytes(self, data, ctype, cache="public, max-age=604800"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self._no_framing()
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._guarded(self._get)

    def do_POST(self):
        self._guarded(self._post)

    def _guarded(self, fn):
        """One bad input answers 500; it never drops the connection with
        nothing said, and never takes a page down for everyone."""
        try:
            fn()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                self._send(500, json.dumps({"error": type(e).__name__}))
            except OSError:
                pass

    def _get(self):
        if not good_host(self.headers):
            self._send(403, json.dumps({"error": "unexpected Host"}))
            return
        if not allowed(self.headers):
            who = caller_login(self.headers) or "no Tailscale identity"
            self._send(403, DENIED.replace("__WHO__", _html.escape(who)),
                       "text/html")
            return
        # Another site may link here, and that is all. A cross-site request
        # that is not a top-level visit to the dashboard itself is somebody
        # else's page spending this server's time or memory as you.
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if site and site not in ("same-origin", "none") and not (
                (self.headers.get("Sec-Fetch-Mode") or "").lower() == "navigate"
                and self.path in ("/", "/index.html")):
            self._send(403, json.dumps({"error": "cross-site request refused"}))
            return
        if self.path in ("/", "/index.html"):
            # Escaped for a script element, not merely for HTML: pane
            # snippets are arbitrary terminal text, and "<!--<script>" in one
            # would swallow the rest of the page. textContent protects what
            # goes *into* the DOM; this blob is the script itself.
            init = _js(status_all())
            self._send(200, PAGE.replace("__INIT__", init), "text/html")
        elif self.path == "/file" or self.path.startswith("/file?"):
            from urllib.parse import parse_qs, urlparse
            want = (parse_qs(urlparse(self.path).query).get("path") or [""])[0]
            f = safe_file(want)
            if f is None:
                d = safe_dir(want) or (safe_dir(FILE_ROOT) if not want else None)
                if d is not None:
                    self._send(200, dir_page(d), "text/html")
                    return
                self._send(404, json.dumps(
                    {"error": "no such file under " + str(FILE_ROOT)}))
                return
            partial = False
            try:
                size = f.stat().st_size
                if size > FILE_MAX:
                    # Read the head only: a 950MB evidence file must not be
                    # pulled into memory to answer a page request.
                    with f.open("rb") as fh:
                        data = fh.read(FILE_HEAD)
                    partial = True
                else:
                    data = f.read_bytes()
            except OSError as e:
                self._send(403, json.dumps({"error": str(e)}))
                return
            raw = (parse_qs(urlparse(self.path).query).get("raw") or ["0"])[0] == "1"
            ctype = FILE_TYPES.get(f.suffix.lower())
            view = None if (raw or ctype) else file_view(f, data, size, partial)
            if view and view[0]:
                # A page we generated ourselves. Its script carries this
                # response's nonce, the file's content is escaped before it
                # goes in, and the policy admits nothing else.
                body, extra = view
                mb = _size_of(size)
                page = (_page_common(FILE_PAGE, f, f.parent)
                        .replace("__EXTRA__", extra)
                        .replace("__BODY__", body)
                        .replace("__NAME__", _html.escape(f.name))
                        .replace("__SIZE__", mb)
                        .replace("__PATH__", _crumbs(f.parent))
                        .replace("__RAWLABEL__", "raw")
                        .replace("__RAW__", "file?raw=1&path=" + _url_q(str(f))))
                page, csp = with_nonce(page)
                out = page.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(out)))
                self.send_header("Content-Security-Policy", csp)
                self.send_header("Cache-Control", "no-store")
                self._no_framing()
                self.end_headers()
                self.wfile.write(out)
                return
            # Images and PDFs get their real type so they render; everything
            # else goes out as text, so a file from a transcript cannot run
            # script against this origin.
            #
            # HTML is the exception, because a page that will not render is
            # not a page: a generated report is worth reading as itself and
            # useless as source. It is served as HTML but disowned -- CSP
            # "sandbox" puts it in an opaque origin, so it shares nothing
            # with roost, and "allow-scripts" is granted only alongside a
            # policy with no connect-src and no external anything, so its
            # script can open a collapsed section and cannot phone anywhere.
            html = f.suffix.lower() in (".html", ".htm")
            self.send_response(200)
            self.send_header("Content-Type",
                             "text/html; charset=utf-8" if html
                             else (ctype or "text/plain; charset=utf-8"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             ("sandbox allow-scripts; default-src 'none'; "
                              "script-src 'unsafe-inline' 'unsafe-eval'; "
                              "style-src 'unsafe-inline'; "
                              "font-src data:; "
                              "img-src data:; frame-ancestors 'self'")
                             if html else "sandbox; frame-ancestors 'self'")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            # ASCII only: a name with CR/LF would split the header, and one
            # outside latin-1 would not encode at all.
            self.send_header("Content-Disposition", 'inline; filename="%s"'
                             % "".join(c if " " <= c < "\x7f" and c not in '"\\'
                                       else "_" for c in f.name))
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith("/vendor/"):
            # The two libraries the pages use, served from disk. Confined to
            # the vendor directory and to the handful of extensions those
            # libraries are made of -- this is not a second file server.
            from urllib.parse import unquote, urlparse
            rel = unquote(urlparse(self.path).path)[len("/vendor/"):]
            try:
                f = (VENDOR / rel).resolve()
                root = VENDOR.resolve()
                ok = (f.is_relative_to(root) and f.suffix in _VENDOR_TYPES
                      and f.is_file())
            except (OSError, ValueError):
                ok = False
            if not ok:
                self._send(404, "not found", "text/plain")
                return
            self._send_bytes(f.read_bytes(), _VENDOR_TYPES[f.suffix])
        elif self.path.startswith("/icon.png") or self.path == "/favicon.ico":
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            try:
                size = int((q.get("s") or ["180"])[0])
            except ValueError:
                size = 180
            # Bounded: the drawing is per-pixel, so an unbounded size would be
            # a way to make the server spin.
            size = min((16, 32, 64, 180, 192, 512), key=lambda x: abs(x - size))
            self._send_bytes(icon_png(size, bool(q.get("m"))), "image/png")
        elif self.path.startswith("/manifest.webmanifest"):
            self._send_bytes(MANIFEST.encode(), "application/manifest+json")
        elif self.path.startswith("/t?") or self.path == "/t":
            from urllib.parse import parse_qs, urlparse
            n = (parse_qs(urlparse(self.path).query).get("name") or [""])[0]
            # Constrained before it is echoed into the page or the iframe src.
            if not n or not all(c.isalnum() or c in "-_" for c in n):
                self._send(400, json.dumps({"error": "bad name"}))
                return
            import re as _re2
            cmd = ""
            try:
                cmd = (Path.home() / ".dtach" / f"{n}.cmd").read_text()
            except OSError:
                pass
            m = _re2.search(r"resume\s+([0-9a-f-]{36})", cmd)
            files = codex_files(200)        # newest first
            hist = ""
            if m:
                uid = m.group(1)
                for f in files:
                    if uid in f["file"]:
                        hist = "codex?file=" + f["file"]
                        break
            if not hist:
                # A Claude terminal attaches to a tmux session; that session's
                # transcript is found by name, not by anything in the command.
                m2 = _re2.search(r"-t\s+([A-Za-z0-9_-]+)", cmd)
                if m2 and "tmux" in cmd:
                    f2 = claude_sid_by_tmux().get(m2.group(1))
                    if f2:
                        hist = "codex?file=" + f2
            if not hist and "claude" in cmd:
                # A migrated Claude session: "cd <dir> && claude --continue".
                # No resume id and no tmux to match on, so it fell through to
                # the codex-by-folder fallback below and picked up an old
                # codex rollout that happened to share the folder -- which is
                # why work2's history showed one item instead of 6718.
                cm2 = _re2.search(r"cd\s+(\S+)", cmd)
                want2 = ""
                if cm2:
                    try:
                        want2 = str(Path(cm2.group(1)).expanduser().resolve())
                    except (OSError, ValueError, RuntimeError):
                        want2 = ""
                for x in claude_files(60):
                    if want2 and x.get("cwd") == want2:
                        hist = "codex?file=" + x["file"]
                        break
            if not hist:
                # A session started fresh -- "cd <dir> && codex" -- carries no
                # id to match on, so its transcript is found by folder: the
                # newest rollout whose cwd is the one the command cd's to.
                cm = _re2.search(r"cd\s+(\S+)", cmd)
                if cm:
                    try:
                        want = str(Path(cm.group(1)).expanduser().resolve())
                    except (OSError, ValueError, RuntimeError):
                        want = ""
                    for f in files:
                        if want and f.get("cwd") == want:
                            hist = "codex?file=" + f["file"]
                            break
            # Where a bare file name in the terminal is resolved from: the
            # folder the session runs in.
            cwd = ""
            cmcwd = _re2.search(r"cd\s+(\S+)", cmd)
            if cmcwd:
                try:
                    cwd = str(Path(cmcwd.group(1)).expanduser().resolve())
                except (OSError, ValueError, RuntimeError):
                    cwd = ""
            body = TERMWRAP_PAGE.replace("__BUILD__", BUILD).replace(
                # The roots the terminal may linkify, which are exactly the
                # roots the file viewer will serve.
                "__ROOTDIRS__", _js([str(d) for d, _ in FILE_ROOTS])).replace(
                "__CWD__", _js(cwd)).replace(
                "__HOME__", _js(str(Path.home()))).replace(
                "__NAME__", n).replace(
                "__FILE__", _js(hist.split("file=")[1] if hist else "")).replace(
                # The transcript now has its own tab; a link to the
                # standalone page would just be a second way in.
                "__HIST__",
                "" if hist else '<span class="r">no transcript found</span>')
            self._send(200, body, "text/html")
        elif self.path.startswith("/term/") or self.path == "/term":
            # Relay to ttyd rather than letting tailscale serve hand /term
            # straight to it. That puts the terminal behind the same identity
            # check as everything else -- serve has no notion of "this user
            # only" -- and lets ttyd bind loopback, so it cannot be reached
            # directly on the tailnet IP. Once the websocket upgrade is done
            # this is just bytes in both directions.
            #
            # same_site() as well, although this is a GET: the websocket
            # behind it is a writable shell, and a page on any site can open
            # one -- the login header rides along whoever asked.
            if not same_site(self.headers):
                self._send(403, json.dumps({"error": "cross-site"}))
                return
            if term_relay(self):
                return
            # Only reached when tailscale serve has no /term mapping on the
            # port this page came in on: with one, the proxy takes the
            # request long before it gets here. It used to fall through to
            # the JSON 404, which inside the terminal iframe read as
            # {"error": "not found"} and explained nothing.
            host = req_host(self.headers)
            self._send(200, TERM_MISSING.replace("__HOST__", _html.escape(host)),
                       "text/html")
        elif self.path.startswith("/terminals"):
            # Links into the single ttyd service (one port, ?arg=<name>).
            # Reached over the same tailnet hostname as this page: ttyd
            # listens on a private socket, so everything goes through the
            # dashboard's relay.
            import os
            d = Path.home() / ".dtach"
            # Relative link: the terminal is mounted at /term on THIS port
            # via `tailscale serve --set-path`, so one URL covers everything.
            rows = []
            for f in sorted(d.glob("*.cmd")):
                n = _html.escape(f.stem, quote=True)
                live = "running" if (d / f.stem).is_socket() else "not started"
                cmd = _html.escape(f.read_text()[:90])
                rows.append(
                    f'<li><a href="/t?name={n}">{n}</a>'
                    f' <span class="s">{live}</span><div class="c">{cmd}</div></li>')
            body = TERMS_PAGE.replace("__ROWS__", "".join(rows) or "<li><em>none defined yet</em></li>")
            self._send(200, body, "text/html")
        elif self.path.startswith("/codex"):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            want = (q.get("file") or [""])[0]
            files = claude_files(40) + codex_files()
            if not any(f["file"] == want for f in files):
                want = files[0]["file"] if files else ""
            # Grouped by cwd rather than repeating it on every row. Codex
            # forks a fresh rollout per resume, so one folder owns four or
            # five files and the path -- the longest part of the label -- was
            # restated each time, which on a phone left no room for the two
            # fields that actually distinguish the rows.
            groups = {}
            for x in files:
                key = x["cwd"].replace(str(Path.home()), "~") or "?"
                key += " · Claude Code" if x["file"].startswith("claude:") else " · codex"
                groups.setdefault(key, []).append(x)
            opts = "".join(
                '<optgroup label="{g}">{rows}</optgroup>'.format(
                    g=_html.escape(k),
                    rows="".join(
                        '<option value="{f}"{sel}>{w} · {mb}MB</option>'.format(
                            f=_html.escape(x["file"], quote=True),
                            sel=" selected" if x["file"] == want else "",
                            w=x["when"], mb=x["mb"])
                        for x in v))
                for k, v in groups.items())
            termof = {v: k for k, v in claude_sid_by_tmux().items()}
            self._send(200, CODEX_PAGE.replace("__OPTIONS__", opts)
                       .replace("__TERMOF__", _js(termof)), "text/html")
        elif self.path.startswith("/api/codex"):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            want = (q.get("file") or [""])[0]
            if not want:                      # default to the newest session
                files = codex_files(1)
                want = files[0]["file"] if files else ""
            f = _codex_path(want)
            if f is None:
                self._send(404, json.dumps({"error": "unknown session file"}))
                return
            try:
                n = max(10, min(100000, int((q.get("items") or ["100000"])[0])))
            except ValueError:
                n = 200
            try:
                skip = max(0, int((q.get("skip") or ["0"])[0]))
            except ValueError:
                skip = 0
            frm = None
            if q.get("from"):
                try:
                    frm = max(0, int(q["from"][0]))
                except ValueError:
                    frm = None
            index = codex_files(200) + claude_files(60)
            cwd_of = next((x["cwd"] for x in index if x["file"] == want), "")
            md = (q.get("md") or ["1"])[0] == "1"
            conv = (q.get("conv") or ["0"])[0] == "1"
            if (q.get("all") or ["0"])[0] == "1":
                # Every rollout sharing this one's folder, oldest first, each
                # with its own heading. Codex starts a new file on every
                # resume, so one line of work is spread across several -- and
                # reading them one at a time is exactly the thing the folder
                # grouping makes visible. `items` still caps EACH file, so a
                # 37 MB rollout cannot swamp the response.
                idx = index
                cwd = next((x["cwd"] for x in idx if x["file"] == want), None)
                mates = [x for x in idx if x["cwd"] == cwd] if cwd is not None \
                    else [x for x in idx if x["file"] == want]
                parts, total = [], 0
                for x in reversed(mates):          # index is newest-first
                    p = _codex_path(x["file"])
                    if p is None:
                        continue
                    b, t = codex_html(p, n, md=md, base=x["cwd"], conv=conv)
                    parts.append(
                        '<div class="filehdr">{w} · {mb}MB · {f}</div>'.format(
                            w=_html.escape(x["when"]), mb=x["mb"],
                            f=_html.escape(x["file"])) + b)
                    total += t
                self._send(200, json.dumps(
                    {"html": "".join(parts), "total": total,
                     "files": len(parts), "target": codex_target(cwd_of)}))
                return
            if (q.get("count") or ["0"])[0] == "1":
                # "Has anything changed?" — the poll's real question. Building
                # the HTML to answer it costs half a megabyte a time.
                _, total = codex_html(f, 10, 0, None, conv=conv)
                self._send(200, json.dumps({"total": total, "counted": True,
                                            "target": codex_target(cwd_of)}))
                return
            body, total = codex_html(f, n, skip, frm, md, cwd_of, conv)
            self._send(200, json.dumps({"html": body, "total": total,
                                        "skip": skip, "from": frm,
                                        "more": (frm if frm is not None
                                                 else max(0, total - skip - n)),
                                        "target": codex_target(cwd_of)}))
        elif self.path.startswith("/logs"):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            want = (q.get("name") or [""])[0]
            sessions = tmux_sessions()
            if not any(x["name"] == want for x in sessions):
                want = sessions[0]["name"] if sessions else ""
            # Name the agent, not the process. pane_current_command is the
            # binary ("claude", "node", "bash"), which does not say which
            # assistant is in the pane -- and with codex sessions alongside
            # claude ones that is the thing you are choosing between.
            def _agent(cmd):
                c = (cmd or "").lower()
                if "codex" in c:
                    return "codex"
                if "claude" in c or c == "node":
                    return "claude"
                return cmd or "—"
            opts = "".join(
                '<option value="{n}"{sel}>{n} · {a}</option>'.format(
                    n=_html.escape(x["name"], quote=True),
                    sel=" selected" if x["name"] == want else "",
                    a=_html.escape(_agent(x["cmd"])))
                for x in sessions)
            body = (LOGS_PAGE.replace("__OPTIONS__", opts)
                             .replace("__HIST__", _js(claude_sid_by_tmux()))
                             .replace("__TITLE__", _html.escape(f"{want} — logs")))
            self._send(200, body, "text/html")
        elif self.path.startswith("/api/pane"):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            name = (q.get("name") or [""])[0]
            try:
                n = max(10, min(5000, int((q.get("lines") or ["200"])[0])))
            except ValueError:
                n = 200
            if not any(x["name"] == name for x in tmux_sessions()):
                self._send(404, json.dumps({"error": "unknown session"}))
                return
            self._send(200, json.dumps({"html": pane_html(name, n)}))
        elif self.path.startswith("/api/doc"):
            # The body of a document, for swapping into a page that is
            # already open. Same resolution and the same renderers as /file;
            # what it leaves out is the page.
            from urllib.parse import parse_qs, urlparse
            want = (parse_qs(urlparse(self.path).query).get("path") or [""])[0]
            d = safe_dir(want)
            if d is not None:
                body, n = dir_body(d)
                self._send(200, json.dumps({
                    "kind": "dir", "path": str(d), "name": d.name or str(d),
                    "size": "%d items" % n, "dir": str(d),
                    "crumbs": _crumbs(d), "extra": _DIR_CSS, "body": body,
                    "raw": "file?path=" + _url_q(str(d)), "rawlabel": "browse"}))
                return
            f = safe_file(want)
            if f is None:
                self._send(404, json.dumps({"error": "no such file"}))
                return
            try:
                size = f.stat().st_size
                partial = size > FILE_MAX
                with f.open("rb") as fh:
                    data = fh.read(FILE_HEAD if partial else size)
            except OSError as e:
                self._send(403, json.dumps({"error": str(e)}))
                return
            if FILE_TYPES.get(f.suffix.lower()):
                # An image or a PDF is not markup to drop into the page; let
                # the browser fetch it as itself.
                self._send(200, json.dumps({"kind": "raw",
                                            "url": "file?path=" + _url_q(str(f))}))
                return
            raw_q = (parse_qs(urlparse(self.path).query).get("raw") or [""])[0]
            if raw_q == "1":
                # The source of a file that normally renders -- JSON, a
                # document, a table -- shown in the page it was opened from,
                # with the tree still beside it. It used to be a link out to
                # text/plain, which is a different page with none of that.
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    self._send(200, json.dumps({"kind": "raw",
                                                "url": "file?path=" + _url_q(str(f))}))
                    return
                lang = _HL_LANG.get(f.suffix.lower().lstrip("."), "")
                body = ('<pre class="src"><code class="%s">%s</code></pre>'
                        % ("language-" + lang if lang else "nohighlight",
                           _html.escape(text)))
                self._send(200, json.dumps({
                    "kind": "file", "path": str(f), "name": f.name,
                    "size": _size_of(size) + " · source",
                    "dir": str(f.parent), "crumbs": _crumbs(f),
                    "extra": _SRC_CSS, "body": body,
                    "raw": "file?path=" + _url_q(str(f)),
                    "rawlabel": "rendered"}))
                return
            view = file_view(f, data, size, partial)
            if view and view[0]:
                body, extra = view
            else:
                try:
                    body = "<pre>%s</pre>" % _html.escape(data.decode("utf-8"))
                    extra = ""
                except UnicodeDecodeError:
                    self._send(200, json.dumps({"kind": "raw",
                                                "url": "file?path=" + _url_q(str(f))}))
                    return
            self._send(200, json.dumps({
                "kind": "file", "path": str(f), "name": f.name,
                "size": _size_of(size), "dir": str(f.parent),
                "crumbs": _crumbs(f.parent), "extra": extra, "body": body,
                "raw": "file?raw=1&path=" + _url_q(str(f)), "rawlabel": "raw"}))
        elif self.path.startswith("/api/slots"):
            from urllib.parse import parse_qs, urlparse
            owner = (parse_qs(urlparse(self.path).query).get("owner") or [""])[0]
            d = _slots_read()
            self._send(200, json.dumps({
                "slots": [{k: v for k, v in x.items() if k != "text"}
                          for x in slots_for(owner)],
                "defs": slot_defs(owner),
                "auto": [k for k, v in d.get("auto", {}).items()
                         if v and k.startswith(owner.lstrip("@") + ">")],
                "reply": reply_target(owner)}))
        elif self.path.startswith("/api/gitstate"):
            from urllib.parse import parse_qs, urlparse
            want = (parse_qs(urlparse(self.path).query).get("path") or [""])[0]
            f = safe_file(want) or safe_dir(want)
            if f is None:
                self._send(404, json.dumps({"error": "no such path"}))
                return
            self._send(200, json.dumps(
                {"state": git_state(f, f.is_dir())}))
        elif self.path.startswith("/api/find"):
            # A bare file name off a terminal line -- "Added DESIGN-NOTES.md"
            # -- resolved against the folder the session runs in, then the
            # folders below it. The same search the transcript's links use.
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            name = (q.get("name") or [""])[0]
            base = (q.get("base") or [""])[0]
            # A bare name or a relative path -- "NOTES.md" or
            # "tmp/codex-01a06fd2-t42/solver-v1/NOTES.md".
            # Not an absolute one (that route needs no help) and nothing
            # that climbs: containment below would refuse it anyway, and
            # refusing it here keeps the search from wandering.
            parts = name.split("/")
            if (not name or name.startswith("/") or ".." in parts
                    or "." in parts or "" in parts):
                self._send(404, json.dumps({"error": "not a file name"}))
                return
            d = safe_dir(base) if base else None
            hit = None
            if d is not None:
                hit = (safe_file(d / name) or safe_dir(d / name)
                       or _find_below(d, name))
            if hit is None:
                self._send(404, json.dumps({"error": "not found"}))
                return
            self._send(200, json.dumps({"path": str(hit)}))
        elif self.path.startswith("/api/tree"):
            # One directory at a time, for the viewer's sidebar. Lazy on
            # purpose: a root like ~/src has tens of thousands of files under
            # it, and a tree that reads them all to draw one level is a tree
            # that never opens.
            from urllib.parse import parse_qs, urlparse
            want = (parse_qs(urlparse(self.path).query).get("path") or [""])[0]
            d = safe_dir(want) if want else (FILE_ROOTS[0][0] if FILE_ROOTS else None)
            if d is None:
                self._send(404, json.dumps({"error": "not a readable folder"}))
                return
            kids = []
            try:
                listing = sorted(listable(d),
                                 key=lambda x: (not x.is_dir(), x.name.lower()))[:2000]
                # Both orders are sent as one answer: the times ride along
                # and the page re-sorts without asking again, so flipping the
                # button is instant.
                times = entry_times(listing)
                here = where(d, True)
                for x in listing:
                    try:
                        isdir = x.is_dir()
                        e = {"name": x.name, "path": str(x), "dir": isdir,
                             "mt": int(times.get(x.name, 0)),
                             "size": 0 if isdir else x.stat().st_size}
                        # Only the differences travel: every row in a folder
                        # shares its session and its repository unless the row
                        # is itself a session root or a checkout of its own.
                        w = where(x, isdir)
                        for k in ("sess", "sroot", "git", "branch"):
                            if w[k] != here[k]:
                                e["w"] = w
                                break
                        kids.append(e)
                    except OSError:
                        continue           # a broken symlink: skip it
            except OSError as e:
                self._send(403, json.dumps({"error": str(e)}))
                return
            root = root_of(d)[0]
            self._send(200, json.dumps({
                "path": str(d), "name": d.name or str(d),
                "parent": str(d.parent) if root and d != root else "",
                "root": str(root) if root else "",
                "roots": [str(r) for r, _ in FILE_ROOTS],
                "where": here, "entries": kids}))
        elif self.path.startswith("/api/repos"):
            self._send(200, json.dumps(repo_tree()))
        elif self.path.startswith("/api/peers"):
            self._send(200, json.dumps(
                [{"kind": k, "name": n, "session": a, "via": t}
                 for k, n, a, t in ccmsg.targets()]))
        elif self.path.startswith("/api/term-reader"):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            n = q.get("name", [""])[0]
            self._send(200, json.dumps({"cid": TERM_READER.get(n, "")}))
        elif self.path.startswith("/api/ping"):
            self._send(200, json.dumps({"id": SERVER_ID, "build": BUILD}))
        elif self.path.startswith("/api/whoami"):
            # What tailscaled's serve proxy says about the caller. These are
            # populated for tailnet traffic only, and tailscaled strips any
            # client-supplied copy before forwarding -- which is the property
            # the whole identity scheme rests on, so it is worth being able
            # to look at directly rather than taking on faith.
            self._send(200, json.dumps({
                "login": self.headers.get("Tailscale-User-Login", ""),
                "name": self.headers.get("Tailscale-User-Name", ""),
                "host": self.headers.get("Host", ""),
                "peer": self.client_address[0],
                "all_tailscale_headers": {k: v for k, v in self.headers.items()
                                          if k.lower().startswith("tailscale")},
                "forwarded": {k: v for k, v in self.headers.items()
                              if k.lower().startswith("x-forwarded")},
            }))
        elif self.path == "/api/status":
            self._send(200, json.dumps(status_all()))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def _post(self):
        # Everything that changes something is a POST, so this is the whole
        # of the write surface.
        if not good_host(self.headers):
            self._send(403, json.dumps({"error": "unexpected Host"}))
            return
        if not same_site(self.headers):
            self._send(403, json.dumps(
                {"error": "cross-site request refused"}))
            return
        if not allowed(self.headers):
            who = caller_login(self.headers) or "no Tailscale identity"
            self._send(403, DENIED.replace("__WHO__", _html.escape(who)),
                       "text/html")
            return
        if self.path.startswith("/api/session"):
            # A card for a folder that already exists on disk. The session
            # itself is started the way every other one is -- by pressing the
            # card -- so there is one path into a running claude, not two.
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            agent = (q.get("agent") or ["claude"])[0]
            name, err = add_session((q.get("path") or [""])[0],
                                    (q.get("name") or [""])[0], agent)
            if err:
                self._send(400, json.dumps({"error": err}))
                return
            result = "added"
            if (q.get("start") or ["1"])[0] == "1":
                result = (("codex started" if start_terminal(name[len("term:"):])
                           else "could not start codex")
                          if name.startswith("term:") else press(name))
            self._send(200, json.dumps({"result": result, "name": name}))
            return
        if self.path.startswith("/api/favorite"):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            name = (q.get("name") or [""])[0]
            on = (q.get("on") or ["1"])[0] == "1"
            if name not in card_names():
                self._send(400, json.dumps({"error": "unknown card"}))
                return
            self._send(200, json.dumps({"result": "ok", "favorites": set_favorite(name, on)}))
            return
        if self.path == "/api/trace":
            # Debug sink for ?trace=1 on a terminal page. Appended, not
            # served back: it exists to be read from the machine.
            try:
                n = int(self.headers.get("Content-Length") or 0)
                rec = self.rfile.read(min(n, 1 << 20)).decode("utf-8", "replace")
                trace = Path.home() / ".roost" / "trace.jsonl"
                trace.parent.mkdir(mode=0o700, exist_ok=True)
                if trace.exists() and trace.stat().st_size > 64 << 20:
                    trace.replace(trace.with_suffix(".jsonl.old"))
                fd = os.open(trace, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a") as fh:
                    fh.write(rec.replace("\n", " ") + "\n")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
                return
            self._send(200, json.dumps({"result": "ok"}))
            return
        if self.path == "/api/reorder":
            try:
                n = int(self.headers.get("Content-Length", 0))
                names = (_jobj(self.rfile.read(n) or b"{}") or {}).get("names", [])
                if not isinstance(names, list) or \
                   not all(isinstance(x, str) for x in names):
                    raise ValueError
            except (ValueError, OSError, AttributeError):
                self._send(400, json.dumps({"error": "bad body"}))
                return
            ok, msg = reorder(names)
            self._send(200 if ok else 409, json.dumps(
                {"result": msg} if ok else {"error": msg}))
            return
        if self.path.startswith("/api/restart"):
            from urllib.parse import parse_qs, urlparse
            name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
            if name.startswith("term:"):
                # Restarting a codex terminal is stopping it and starting it
                # again from the same definition. The conversation is in its
                # rollout file, not in the process, so nothing is lost that
                # "codex resume" would not pick back up.
                t = name[len("term:"):]
                if not t or not all(c.isalnum() or c in "-_" for c in t):
                    self._send(400, json.dumps({"error": "bad name"}))
                    return
                sock = Path.home() / ".dtach" / t
                # The program first, while it still has a terminal to be
                # told on, then the master, then whatever clients are
                # attached. The other order leaves the program orphaned.
                master, kids = dtach_tree(sock)
                for pid in reversed(kids):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except OSError:
                        pass
                for _ in range(20):          # let it flush and go
                    if not any(Path("/proc/%d" % p).exists() for p in kids):
                        break
                    time.sleep(0.1)
                for pid in master:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except OSError:
                        pass
                # Anchored: unanchored, "dtach -A ~/.dtach/fo" also matched
                # the master of ~/.dtach/foo.
                subprocess.run(["pkill", "-f", "^dtach -A %s( |$)"
                                % _re.escape(str(sock))], capture_output=True)
                time.sleep(1)
                try:
                    sock.unlink()
                except OSError:
                    pass
                ok = start_terminal(t)
                self._send(200 if ok else 400, json.dumps(
                    {"result": "restarted"} if ok else {"error": "could not start " + t}))
                return
            if name not in FOLDERS:
                self._send(400, json.dumps({"error": "unknown session"}))
                return
            self._send(200, json.dumps({"result": restart(name)}))
            return
        if self.path.startswith("/api/screen"):
            # A page reporting what its own terminal is showing. Kept in
            # memory, never on disk: it describes a moment, not a fact.
            from urllib.parse import parse_qs, urlparse
            name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
            if not name or not all(c.isalnum() or c in "-_" for c in name):
                self._send(400, json.dumps({"error": "bad name"}))
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = _jobj(self.rfile.read(min(n, 8192)) or b"{}")
                if body is None:
                    raise ValueError
            except (ValueError, OSError):
                self._send(400, json.dumps({"error": "bad body"}))
                return
            if len(SCREEN_SAYS) > 1000 and name not in SCREEN_SAYS:
                self._send(400, json.dumps({"error": "too many screens"}))
                return
            SCREEN_SAYS[name] = {"t": time.time(),
                                 "asking": bool(body.get("asking")),
                                 "text": str(body.get("text") or "")[:200]}
            self._send(200, json.dumps({"result": "noted"}))
            return
        if self.path.startswith("/api/paste"):
            # An image from the clipboard or the phone's camera roll. xterm
            # pastes text and nothing else -- its paste handler reads
            # text/plain and stops -- so an image dropped on a browser
            # terminal has never gone anywhere. It is saved here instead and
            # its path typed into the session, which is the form the agent
            # can actually act on: it reads the file itself.
            from urllib.parse import parse_qs, urlparse
            name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
            if not name or not all(c.isalnum() or c in "-_" for c in name):
                self._send(400, json.dumps({"error": "bad name"}))
                return
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
                   "image/webp": ".webp"}.get(ctype)
            if not ext:
                self._send(415, json.dumps({"error": "not an image"}))
                return
            live = is_dtach(name) or any(x["name"] == name for x in tmux_sessions())
            if not live:
                self._send(400, json.dumps({"error": "unknown session"}))
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n <= 0 or n > PASTE_MAX:
                self._send(413, json.dumps({"error": "0 to %d bytes, got %d"
                                            % (PASTE_MAX, n)}))
                return
            data = b""
            while len(data) < n:
                chunk = self.rfile.read(min(n - len(data), 1 << 16))
                if not chunk:
                    break
                data += chunk
            try:
                PASTE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
                f = PASTE_DIR / (time.strftime("%Y%m%d-%H%M%S") + "-"
                                 + _uuid.uuid4().hex[:6] + ext)
                f.write_bytes(data)
            except OSError as e:
                self._send(500, json.dumps({"error": str(e)}))
                return
            # A space after it, so whatever you type next is a separate word.
            paste_into(name, str(f) + " ")
            self._send(200, json.dumps({"path": str(f), "bytes": len(data)}))
            return
        if self.path.startswith("/api/type"):
            # Types one line into a tmux session and presses Enter. Note this
            # makes /logs no longer read-only: watching a run can now disturb
            # it, which the page says out loud rather than leaving to be
            # discovered.
            #
            # Scoped to sessions tmux actually reports, NOT to FOLDERS: the
            # point is to reach panes that config.json never listed. Validated
            # against the live list so an arbitrary string cannot become a
            # send-keys target.
            from urllib.parse import parse_qs, urlparse
            name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
            if not any(x["name"] == name for x in tmux_sessions()):
                self._send(400, json.dumps({"error": "unknown session"}))
                return
            try:
                n = int(self.headers.get("Content-Length", 0))
                text = (_jobj(self.rfile.read(n) or b"{}") or {}).get("text", "")
                if not isinstance(text, str):
                    raise ValueError
            except (ValueError, OSError):
                self._send(400, json.dumps({"error": "bad body"}))
                return
            text = text.replace("\r", "").replace("\n", " ").strip()
            if not text:
                self._send(400, json.dumps({"error": "empty"}))
                return
            type_into(name, text)
            self._send(200, json.dumps({"result": "sent"}))
            return
        if self.path.startswith("/api/slot/"):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            what = urlparse(self.path).path[len("/api/slot/"):]
            g = lambda k: (q.get(k) or [""])[0]
            if what == "dump":
                # Name it after the target when a person did not say: a slot
                # called "@app2" is the easiest thing to hold in your head,
                # and any other name still works for a bot that was given one.
                slot = g("slot") or (("@" + g("to").lstrip("@")) if g("to") else "out")
                rec, msg = slot_dump(g("from"), slot, g("to"))
                self._send(200 if rec else 400,
                           json.dumps({"result": msg, "slot": rec and rec["id"]}
                                      if rec else {"error": msg}))
            elif what == "send":
                ok, msg = slot_send(g("id"))
                self._send(200 if ok else 400,
                           json.dumps({"result" if ok else "error": msg}))
            elif what == "drop":
                self._send(200, json.dumps({"result": "dropped"
                                            if slot_drop(g("id")) else "gone"}))
            elif what == "bind":
                slot = g("slot") or (("@" + g("to").lstrip("@")) if g("to") else "out")
                ok, msg = slot_bind(g("from"), slot, g("to"))
                self._send(200 if ok else 400,
                           json.dumps({"result" if ok else "error": msg}))
            elif what == "auto":
                on = slot_auto(g("from"), g("to"), g("on") == "1")
                self._send(200, json.dumps({"result": "auto" if on else "manual"}))
            else:
                self._send(404, json.dumps({"error": "no such slot action"}))
            return
        if self.path.startswith("/api/handover"):
            # roost does the whole thing: reads @from's last answer out of its
            # transcript and types it into @to. Neither session has to
            # cooperate, and the source does not have to be asked to repeat
            # what it just said.
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            src = (q.get("from") or [""])[0]
            dst = (q.get("to") or [""])[0]
            dry = (q.get("dry") or ["0"])[0] == "1"
            if not src or not dst:
                self._send(400, json.dumps({"error": "need from= and to="}))
                return
            ok, detail = ccmsg.forward(dst, src, dry)
            self._send(200 if ok else 400,
                       json.dumps({"result" if ok else "error": detail}))
            return
        if self.path.startswith("/api/msg"):
            # One address space for both kinds of agent. The routing and the
            # two write paths live in ccmsg, so the same thing works from a
            # shell when this server is not running -- which is the point:
            # an agent should not need the dashboard to reach another agent.
            from urllib.parse import parse_qs, urlparse
            to = parse_qs(urlparse(self.path).query).get("to", [""])[0]
            try:
                n = int(self.headers.get("Content-Length", 0))
                text = (_jobj(self.rfile.read(n) or b"{}") or {}).get("text", "")
                if not isinstance(text, str):
                    raise ValueError
            except (ValueError, OSError):
                self._send(400, json.dumps({"error": "bad body"}))
                return
            ok, detail = ccmsg.send(to, text)
            self._send(200 if ok else 400,
                       json.dumps({"result" if ok else "error": detail}))
            return
        if self.path.startswith("/api/term-claim"):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            n, cid = q.get("name", [""])[0], q.get("cid", [""])[0]
            if not n or not cid:
                self._send(400, json.dumps({"error": "name and cid"}))
                return
            if not all(c.isalnum() or c in "-_" for c in n) or len(cid) > 100 \
               or len(TERM_READER) > 1000 and n not in TERM_READER:
                self._send(400, json.dumps({"error": "bad name"}))
                return
            self._send(200, json.dumps({"cid": claim_term(n, cid)}))
            return
        if self.path.startswith("/api/press"):
            name = ""
            if "name=" in self.path:
                from urllib.parse import parse_qs, urlparse, unquote
                name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
            if name.startswith("term:"):
                # A codex terminal: its .cmd is the whole definition, so
                # starting it is starting the session.
                t = name[len("term:"):]
                if not all(c.isalnum() or c in "-_" for c in t):
                    self._send(400, json.dumps({"error": "bad name"}))
                    return
                live = (Path.home() / ".dtach" / t).is_socket()
                ok = start_terminal(t)
                self._send(200 if ok else 400, json.dumps(
                    {"result": "already running" if live else "started"} if ok
                    else {"error": "no definition for " + t}))
                return
            if name not in FOLDERS:
                self._send(400, json.dumps({"error": "unknown session"}))
                return
            self._send(200, json.dumps({"result": press(name)}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, fmt, *args):
        pass  # keep the tmux pane quiet


if __name__ == "__main__":
    import socket as _socket
    import socketserver as _ss

    class UnixHTTPServer(_ss.ThreadingMixIn, _ss.UnixStreamServer):
        daemon_threads = True
        request_queue_size = 64     # a burst must not turn serve away

        def get_request(self):
            conn, _ = self.socket.accept()
            return conn, ("local", 0)     # handlers expect (host, port)

    # A socket left by a previous run is removed only if nothing answers on
    # it; a live one means another server is already up.
    import stat as _stat
    # The socket's directory must be ours alone: in a shared directory
    # another account could put its own listener where tailscaled looks.
    SOCK.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for d in (SOCK.parent, TTYD_SOCK.parent):
        st = os.stat(d)
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            sys.stderr.write(f"{d} must be owned by this user and mode 0700\n")
            sys.exit(3)
    try:
        st = os.lstat(SOCK)
    except FileNotFoundError:
        st = None
    if st is not None:
        if not _stat.S_ISSOCK(st.st_mode):
            sys.stderr.write(f"{SOCK} exists and is not a socket\n")
            sys.exit(3)
        probe = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        probe.settimeout(2)
        try:
            probe.connect(str(SOCK))
            sys.stderr.write(f"{SOCK} is already being served\n")
            sys.exit(3)
        except (ConnectionRefusedError, FileNotFoundError):
            SOCK.unlink()
        except OSError as e:
            sys.stderr.write(f"{SOCK}: {e}\n")
            sys.exit(3)
        finally:
            probe.close()
    srv = UnixHTTPServer(str(SOCK), Handler)
    os.chmod(SOCK, 0o600)
    print(f"roost serving on unix:{SOCK} (sessions: {', '.join(FOLDERS)})")
    srv.serve_forever()
