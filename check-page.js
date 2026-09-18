// Run a served page's script against a DOM stub, and report what it throws.
//
//   ./server.py &                       # or however it is already running
//   f=$(mktemp) && curl -s --unix-socket ~/.roost/roost.sock -H "Tailscale-User-Login: $LOGIN" 'http://localhost/t?name=x' -o "$f" && node check-page.js "$f"
//
// `node --check` only parses. It passed happily on a page whose script died
// on the first statement — an init call placed above the `const` it reaches,
// so a ReferenceError killed everything after it and the history tab simply
// stopped responding. Nothing about that is visible in the syntax.
//
// The stub is deliberately shallow: every id in the HTML becomes an element,
// timers and fetch return without doing anything. That is enough to execute
// the whole top level, which is where this class of bug lives.
const fs = require('fs'), vm = require('vm');
const html = fs.readFileSync(process.argv[2], 'utf8');
const src = /<script(?: nonce="[^"]*")?>([\s\S]*)<\/script>/.exec(html)[1];
const ids = [...html.matchAll(/id="([\w-]+)"/g)].map(m => m[1]);

const el = (id) => {
  const set = new Set();
  const e = {
    id, textContent: "", innerHTML: "", scrollTop: 0, scrollHeight: 0,
    clientHeight: 0, checked: false, dataset: {}, style: {}, contentDocument: null,
    classList: { add: c => set.add(c), remove: c => set.delete(c),
                 toggle: (c, on) => (on === undefined ? (set.has(c) ? set.delete(c) : set.add(c)) : (on ? set.add(c) : set.delete(c))),
                 contains: c => set.has(c) },
    addEventListener() {}, removeEventListener() {}, appendChild() {},
    querySelectorAll: () => [], querySelector: () => null,
    closest: () => null, append() {}, after() {}, insertBefore() {},
    nextElementSibling: null, offsetTop: 0, scrollIntoView() {},
    getAttribute: () => "", setAttribute() {}, getBoundingClientRect: () => ({height: 17}),
    firstElementChild: null, remove() {}, focus() {}, select() {},
  };
  e.firstElementChild = { textContent: "" };
  return e;
};
const nodes = {};
for (const id of ids) nodes[id] = el(id);
const missing = new Set();
const doc = {
  getElementById: (id) => { if (!nodes[id]) { missing.add(id); nodes[id] = el(id); } return nodes[id]; },
  createElement: () => el("new"), querySelectorAll: () => [],
  // Returns an element, not null. Returning null made every
  // querySelector("main") look like a crash, which is a false alarm that
  // teaches you to ignore this harness.
  querySelector: (sel) => (nodes[sel] || (nodes[sel] = el(sel))),
  addEventListener() {}, head: el("head"), body: el("body"), title: "t", readyState: "complete",
};
const ctx = {
  document: doc, console,
  window: { innerWidth: 411, innerHeight: 751, devicePixelRatio: 2.6,
            getComputedStyle: () => ({}), isSecureContext: false,
            addEventListener() {}, removeEventListener() {},
            setTimeout: () => 0, setInterval: () => 0 },
  location: { search: "", href: "http://x/codex?file=f", origin: "http://x",
              pathname: "/codex", toString() { return this.href; } }, navigator: { userAgent: "test" },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  performance: { now: () => 0 },
  fetch: () => Promise.resolve({ json: () => Promise.resolve({ html: "", total: 0 }) }),
  setTimeout: () => 0, setInterval: () => 0, clearTimeout() {}, clearInterval() {},
  requestAnimationFrame: () => 0, cancelAnimationFrame() {},
  URL, URLSearchParams, history: { replaceState() {}, pushState() {} }, JSON, Math, Date, Set, Map, Array, Object, String, Number, Promise, encodeURIComponent,
};
ctx.window.location = ctx.location; ctx.globalThis = ctx;
try {
  vm.createContext(ctx);
  new vm.Script(src, { filename: process.argv[2] }).runInContext(ctx);
  console.log(`${process.argv[2]}: script ran clean`);
  if (missing.size) console.log("  ids used but not in the HTML:", [...missing].join(", "));
} catch (e) {
  console.log(`${process.argv[2]}: THREW ${e.name}: ${e.message}`);
  process.exit(1);
}
