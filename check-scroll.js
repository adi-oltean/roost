// Does the terminal stay where the reader put it?
//
//   node check-scroll.js <saved /t?name=... page>
//
// This one keeps coming back, so it gets a test. The end-chase drags the
// terminal to the last line for five seconds after the iframe loads, and it
// is supposed to stand down the moment someone scrolls. It learned about
// scrolling from a listener on .xterm-viewport -- but xterm paints
// .xterm-screen OVER the viewport as a SIBLING, so a wheel lands on the
// screen and never reaches the element that scrolls. The listener heard
// nothing, the chase believed nobody was reading, and the view snapped back
// to the bottom under the wheel.
//
// So the fixture below is deliberately shaped like the real thing: screen
// and viewport as siblings, the wheel dispatched on the screen. A fix that
// only listens on the viewport fails here, which is the point.
// jsdom, unlike check-page.js's hand-rolled stub, because this test is about
// event dispatch and hit testing -- there is nothing to measure without a
// real DOM. It is the only dependency in the repo and it is not vendored:
//   npm install jsdom          (anywhere, then run this with NODE_PATH set)
const fs = require("fs");
const path = require("path");
let JSDOM;
try {
  ({ JSDOM } = require("jsdom"));
} catch (e) {
  for (const dir of [process.env.JSDOM_PATH, path.join(__dirname, "node_modules"),
                     path.join(process.env.HOME || "", "node_modules")]) {
    if (!dir) continue;
    try { ({ JSDOM } = require(path.join(dir, "jsdom"))); break; } catch (e2) {}
  }
}
if (!JSDOM) {
  console.error("check-scroll needs jsdom: npm install jsdom, then either run "
                + "this from that directory or set JSDOM_PATH to its node_modules");
  process.exit(2);
}

const file = process.argv[2];
if (!file) { console.error("usage: node check-scroll.js <page.html>"); process.exit(2); }
const html = fs.readFileSync(file, "utf8")
  .replace(/(href|src)="vendor\/([^"?]+)[^"]*"/g,
           (m, a, p) => `${a}="file://${__dirname}/vendor/${p}"`);

const dom = new JSDOM(html, {
  runScripts: "dangerously", resources: "usable", pretendToBeVisual: true,
  url: "https://x/t?name=test",
  beforeParse(w) { w.fetch = () => new Promise(() => {}); },
});
const w = dom.window, d = w.document;

const inner = new JSDOM('<div class="xterm"><div class="xterm-viewport"></div>'
                      + '<div class="xterm-screen"></div></div>').window.document;
const vp = inner.querySelector(".xterm-viewport");
const screen = inner.querySelector(".xterm-screen");
let top = 0;
Object.defineProperty(vp, "scrollHeight", { get: () => 5000 });
Object.defineProperty(vp, "clientHeight", { get: () => 500 });
Object.defineProperty(vp, "scrollTop", { get: () => top,
                                         set: (v) => { top = Math.min(v, 4500); } });

const fail = (m) => { console.log(`${file}: FAIL — ${m}`); process.exit(1); };

w.addEventListener("load", () => setTimeout(() => {
  const f = d.getElementById("term");
  Object.defineProperty(f, "contentDocument", { get: () => inner });
  Object.defineProperty(f, "contentWindow", { get: () => ({ term: {} }) });
  f.dispatchEvent(new w.Event("load"));
  setTimeout(() => {
    if (top !== 4500) fail(`the chase never reached the end (${top})`);
    top = 3000;                                   // the reader wheels up
    screen.dispatchEvent(new inner.defaultView.WheelEvent(
      "wheel", { deltaY: -120, bubbles: true }));
    setTimeout(() => {
      if (top !== 3000) fail(`scrolled to 3000, dragged back to ${top}`);
      console.log(`${file}: the terminal stays where it is put`);
      process.exit(0);
    }, 900);
  }, 700);
}, 400));
setTimeout(() => fail("timed out"), 20000);
