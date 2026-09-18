// Does the star actually move the card?
//
//   PLAYWRIGHT_PATH=<dir>/node_modules node check-order.js
//
// Pinning used to set a flag and nothing else: once anyone had dragged a
// card, card_order decided the whole arrangement and a starred card stayed
// exactly where it was. This clicks the star on the bottom card and checks
// it arrives at the top, then unpins it and checks it rejoins the others.
//
// It works on the live config.json, so it copies it aside first and puts it
// back afterwards -- including when a check fails.
const path = require("path");
const fs = require("fs");
const CFG = path.join(__dirname, "config.json");
const SAVED = fs.readFileSync(CFG);
const restore = () => { try { fs.writeFileSync(CFG, SAVED); } catch (e) {} };
process.on("exit", restore);
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
  console.error("check-order needs playwright: npm install playwright && "
                + "npx playwright install chromium, then set PLAYWRIGHT_PATH");
  process.exit(2);
}
const BASE = process.env.ROOST_URL || (console.error("set ROOST_URL to the https://…:8444 address"), process.exit(2));
// The identity the dashboard expects, as restart.sh sends it.
const LOGIN = (() => {
  try { return (require("./config.json").allow_logins || [])[0] || ""; }
  catch (e) { return ""; }
})();
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
let bad=0; const check=(l,g,w)=>{const ok=String(g)===String(w); if(!ok)bad++;
  console.log(`  ${ok?"ok  ":"FAIL"}  ${l}: ${g}${ok?"":"   (wanted "+w+")"}`);};
const layout = p => p.evaluate(() => [...document.querySelectorAll("#list .row")]
  .map(r => ({ name: r.dataset.name,
               fav: !!r.querySelector(".star.on") })));
(async()=>{
  const b=await chromium.launch();
  const c=await b.newContext({viewport:{width:1100,height:900},
    extraHTTPHeaders:{"Tailscale-User-Login":LOGIN}});
  const p=await c.newPage();
  await p.goto(BASE+"/"); await sleep(2500);
  let rows=await layout(p);
  console.log("  order:", rows.map(r=>(r.fav?"★":"·")+r.name).join(" "));
  const firstUnfav=rows.findIndex(r=>!r.fav);
  const lastFav=rows.map(r=>r.fav).lastIndexOf(true);
  check("every pinned card is above every unpinned one", lastFav < firstUnfav || firstUnfav<0, true);

  const victim=rows[rows.length-1];            // the very last, unpinned
  check("the card to pin is unpinned and last", victim.fav, false);
  console.log(`  pinning "${victim.name}" from the bottom…`);
  await p.evaluate(n=>document.querySelector(`#list .row[data-name="${n}"] .star`).click(), victim.name);
  await sleep(2500); rows=await layout(p);
  console.log("  order:", rows.map(r=>(r.fav?"★":"·")+r.name).join(" "));
  check("it jumped to the top", rows[0].name, victim.name);
  check("and shows as pinned", rows[0].fav, true);

  console.log(`  unpinning it again…`);
  await p.evaluate(n=>document.querySelector(`#list .row[data-name="${n}"] .star`).click(), victim.name);
  await sleep(2500); rows=await layout(p);
  console.log("  order:", rows.map(r=>(r.fav?"★":"·")+r.name).join(" "));
  const lf=rows.map(r=>r.fav).lastIndexOf(true), pos=rows.findIndex(r=>r.name===victim.name);
  check("it dropped below the pinned group", pos > lf, true);
  await b.close();
  console.log(bad?`\n  ${bad} FAILED`:"\n  all checks passed");
  process.exit(bad?1:0);
})();
