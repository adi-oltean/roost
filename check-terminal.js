// Does one page, and only one page, hold the terminal?
//
//   npm i playwright && npx playwright install chromium
//   ROOST_URL=https://<machine>.<tailnet>.ts.net:8444 node check-terminal.js
//   (roost listens on a Unix socket; a browser reaches it through serve)
//
// A real browser, because the bug is invisible to a DOM stub: xterm measures
// itself from layout, connects over a websocket, and the size it computes is
// pushed onto a pty that every other client shares. jsdom has none of that,
// and said everything was fine while a page fetched its own API and got
// ttyd's 404 back.
//
// It drives a scratch session called pwtest, never a real one, and checks
// what actually matters: how many clients are attached to the socket, and
// what window size the pty ended up with.
const path = require("path");
let chromium;
try {
  ({ chromium } = require("playwright"));
} catch (e) {
  for (const dir of [process.env.PLAYWRIGHT_PATH,
                     path.join(process.env.HOME || "", "node_modules")]) {
    if (!dir) continue;
    try { ({ chromium } = require(path.join(dir, "playwright"))); break; } catch (e2) {}
  }
}
if (!chromium) {
  console.error("check-terminal needs playwright: npm install playwright && "
                + "npx playwright install chromium, then set PLAYWRIGHT_PATH "
                + "to that node_modules");
  process.exit(2);
}
const { execSync } = require("child_process");
const BASE = process.env.ROOST_URL || (console.error("set ROOST_URL to the https://…:8444 address"), process.exit(2)), NAME = "pwtest";
const DTACH = path.join(require("os").homedir(), ".dtach");
const SOCK = path.join(DTACH, NAME);
// The identity the dashboard expects, as restart.sh sends it.
const LOGIN = (() => {
  try { return (require("./config.json").allow_logins || [])[0] || ""; }
  catch (e) { return ""; }
})();

const sh = (c) => { try { return execSync(c, {encoding:"utf8", shell:"/bin/bash"}).trim(); }
                    catch (e) { return ""; } };
const pids = (c) => sh(c).split("\n").map(x => x.trim()).filter(x => /^\d+$/.test(x));
const clients = () => pids(`pgrep -f "dtach -[aA] ${SOCK}" || true`)
                        .filter(p => pids(`pgrep -P ${p} || true`).length === 0);
const ptySize = () => {
  const m = pids(`pgrep -f "dtach -n ${SOCK}" || true`)[0];
  if (!m) return "no master";
  const kid = pids(`pgrep -P ${m} || true`)[0];
  return kid ? sh(`stty size < /proc/${kid}/fd/0 2>/dev/null || true`) : "no program";
};
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
let bad = 0;
const check = (label, got, want) => {
  const ok = String(got) === String(want);
  if (!ok) bad++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${label}: ${got}${ok ? "" : "   (wanted " + want + ")"}`);
};

(async () => {
  sh(`printf 'sleep 3600' > ${DTACH}/${NAME}.cmd`);
  sh(`pkill -f "dtach -[aAn] ${SOCK}"; rm -f ${SOCK}`);
  const b = await chromium.launch();
  const ctx = (h) => b.newContext({ viewport: { width: 1200, height: h },
    extraHTTPHeaders: { "Tailscale-User-Login": LOGIN } });

  const c1 = await ctx(820), p1 = await c1.newPage();
  p1.on("pageerror", e => console.log("    page1 threw:", String(e).split("\n")[0]));
  await p1.goto(`${BASE}/t?name=${NAME}`); await sleep(5000);
  check("one client after the first page", clients().length, 1);
  const size1 = ptySize();  console.log(`        pty ${size1}`);

  // A tab from before the protocol existed: it never asks who holds the
  // terminal, so only eviction removes it. stdin held open, or it exits.
  sh(`setsid bash -c 'sleep 300 | script -q -c "stty rows 31 cols 100; exec ${path.join(__dirname, "attach.sh")} ${NAME}" /dev/null >/dev/null 2>&1' >/dev/null 2>&1 & disown; sleep 3`);
  check("stale client is attached", clients().length, 2);
  console.log(`        pty ${ptySize()}  (the stale one took it)`);

  const c2 = await ctx(560), p2 = await c2.newPage();
  p2.on("pageerror", e => console.log("    page2 threw:", String(e).split("\n")[0]));
  await p2.goto(`${BASE}/t?name=${NAME}`); await sleep(6000);
  check("stale client evicted, one left", clients().length, 1);
  const size2 = ptySize();  console.log(`        pty ${size2}`);
  check("the pty followed the new page", size2 !== size1 && size2 !== "31 100", true);
  check("first page says paused",  await p1.locator("#paused").isVisible(), true);
  check("second page is reading",  await p2.locator("#paused").isVisible(), false);

  await p1.locator("#takeover").click(); await sleep(5000);
  check("one client after taking it back", clients().length, 1);
  check("the pty came back", ptySize(), size1);
  check("first page is reading again", await p1.locator("#paused").isVisible(), false);
  await sleep(5000);
  check("second page stood down", await p2.locator("#paused").isVisible(), true);

  await b.close();
  sh(`pkill -f "dtach -[aAn] ${SOCK}"; rm -f ${SOCK} ${SOCK}.cmd`);
  console.log(bad ? `\n  ${bad} FAILED` : "\n  all checks passed");
  process.exit(bad ? 1 : 0);
})();
