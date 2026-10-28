"""Arabic text -> transparent PNG via headless Chromium (HarfBuzz shaping, full RTL support).

Pillow on Windows ships without libraqm, and Chromium shapes Uthmani text with all its
marks correctly, so every text layer is laid out as HTML and screenshotted with a
transparent background.
"""
from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .util import ROOT, cache_dir  # noqa: I001 — sets PLAYWRIGHT_BROWSERS_PATH before playwright loads
from playwright.sync_api import sync_playwright

log = logging.getLogger(__name__)

FONTS_DIR = ROOT / "assets" / "fonts"

PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;padding:0;background:transparent;width:1080px;height:1920px;overflow:hidden}
#stage{position:absolute;inset:0}
.card{position:absolute;left:50%;transform:translateX(-50%);direction:rtl;text-align:center;
      box-sizing:border-box;width:1000px;padding:46px 50px;border-radius:44px;word-spacing:.1em}
.card.band{background:linear-gradient(180deg,rgba(8,10,14,.64),rgba(8,10,14,.52));
      box-shadow:inset 0 0 0 2px rgba(242,210,122,.40),0 18px 50px rgba(0,0,0,.35)}
/* frame: thin gold rules above and below over a soft dark wash */
.card.frame{border-radius:6px;background:radial-gradient(ellipse at center,rgba(6,8,12,.62),rgba(6,8,12,.38) 70%,rgba(6,8,12,.18));
      border-top:2px solid rgba(242,210,122,.85);border-bottom:2px solid rgba(242,210,122,.85)}
/* ribbon: an edge-to-edge strip across the whole screen */
.card.ribbon{width:1080px;border-radius:0;padding-left:60px;padding-right:60px;
      background:linear-gradient(180deg,rgba(0,0,0,.35),rgba(0,0,0,.66) 18%,rgba(0,0,0,.66) 82%,rgba(0,0,0,.35))}
/* dark outline painted under the fill: letters stay crisp on any footage */
.w,.m{paint-order:stroke fill;-webkit-text-stroke:.05em rgba(0,0,0,.55)}
.card.band .w,.card.band .m,.card.frame .w,.card.frame .m,.card.ribbon .w,.card.ribbon .m{text-shadow:0 3px 8px rgba(0,0,0,.6)}
.card.shadow .w,.card.shadow .m{text-shadow:0 2px 4px rgba(0,0,0,.95),0 0 26px rgba(0,0,0,.75)}
.card.soft .w,.card.soft .m{text-shadow:0 2px 6px rgba(0,0,0,.8),0 0 18px rgba(0,0,0,.5)}
.w.todo{opacity:.72}
.w.cur{color:var(--accent);text-shadow:0 0 22px rgba(242,210,122,.55),0 2px 5px rgba(0,0,0,.85)}
.w.hide,.m.hide{visibility:hidden}
.m,.g{white-space:nowrap}
.br{font-family:"B";color:var(--accent)}
.lbl{position:absolute;left:0;right:0;text-align:center;direction:rtl;font-family:"L";
     color:#fff;text-shadow:0 2px 6px rgba(0,0,0,.85)}
.pill{display:inline-block;padding:.18em .9em .28em;border-radius:999px;background:rgba(0,0,0,.38);
      font-weight:700;letter-spacing:.02em;text-shadow:0 1px 3px rgba(0,0,0,.7)}
</style></head><body><div id="stage"></div>
<script>
async function useFonts(q, l, b){
  for (const [name,file] of [["Q",q],["L",l],["B",b]]){
    const f = new FontFace(name, `url("${file}")`);
    await f.load(); document.fonts.add(f);
  }
  await document.fonts.ready;
}
// pause marks arrive as "word ۚ": keep them glued to their word (no line break before the mark).
// attach=true drops the space so the mark sits on the word's last letter (fonts that float it).
let ATTACH=false;
function esc(s){
  s=s.replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
  return ATTACH ? s.replace(/ (?=[ۖ-ۜ۟-۪ۤۧۨ-ۭ])/g,"").replace(/ /g," ")
                : s.replace(/ /g," ");
}
// p: {words, marker, style, color, accent, size, minSize, top, maxH, brackets}
function layoutAyah(p){
  ATTACH=!!p.attach;
  const st=document.getElementById("stage"); st.innerHTML="";
  const c=document.createElement("div"); c.className="card "+p.style; c.id="card";
  c.style.fontFamily='"Q"'; c.style.color=p.color; c.style.setProperty("--accent",p.accent);
  c.style.fontWeight=p.weight||400;
  // a long ayah is split into screens: opening bracket on the first, marker + closing on the last
  // the brackets / ayah number are glued to the first / last word (nowrap) so a bracket can
  // never end up alone on a line
  const ws0=p.words.map((w,i)=>`<span class="w" id="w${i}">${esc(w)}</span>`);
  let head=p.open?'<span class="br m">﴿</span> ':"", tail="";
  if(p.marker) tail+=` <span class="m tail" id="mk">${esc(p.marker)}</span>`;
  if(p.close) tail+=' <span class="br m tail">﴾</span>';
  const n=ws0.length;
  let h;
  if(n<=1) h=`<span class="g">${head}${ws0.join("")}${tail}</span>`;
  else h=`<span class="g">${head}${ws0[0]}</span> `+ws0.slice(1,n-1).join(" ")+(n>2?" ":"")+
         `<span class="g">${ws0[n-1]}${tail}</span>`;
  c.innerHTML=h; st.appendChild(c);
  let size=p.size;
  const fit=()=>{c.style.fontSize=size+"px"; c.style.lineHeight=(p.lineHeight||1.85);};
  fit();
  while(c.offsetHeight>p.maxH && size>p.minSize){size-=2; fit();}
  // vertical placement: top = anchor y of card centre
  c.style.top=Math.round(p.centerY - c.offsetHeight/2)+"px";
  // line index per word
  const ws=[...c.querySelectorAll(".w")];
  const tops=[...new Set(ws.map(w=>Math.round(w.offsetTop/4)*4))].sort((a,b)=>a-b);
  const line=ws.map(w=>tops.indexOf(Math.round(w.offsetTop/4)*4));
  const r=c.getBoundingClientRect();
  return {size, lines:tops.length, line, box:[r.left,r.top,r.width,r.height],
          overflow: c.offsetHeight>p.maxH};
}
// mode: "all" | {upToLine:k} | {word:i}
function setState(s, line){
  const ws=[...document.querySelectorAll(".w")];

  ws.forEach((w,i)=>{
    w.className="w";
    if(s.upToLine!==undefined && line[i]>s.upToLine) w.classList.add("hide");
    if(s.word!==undefined){ if(i===s.word) w.classList.add("cur"); else if(i>s.word) w.classList.add("todo"); }
  });
  const last=Math.max(...line);
  // ayah number + closing bracket only appear with the last line
  document.querySelectorAll(".tail").forEach(t=>t.classList.toggle("hide", s.upToLine!==undefined && s.upToLine<last));
}
function labels(items){
  const st=document.getElementById("stage"); st.innerHTML="";
  for(const it of items){
    const d=document.createElement("div"); d.className="lbl";
    d.style.top=it.y+"px"; d.style.fontSize=it.size+"px"; d.style.opacity=it.opacity||1;
    // isolate Latin runs ("@EZDADD") so RTL layout doesn't move the @ to the wrong side
    const txt=it.text.replace(/[@A-Za-z0-9_.]+/g,m=>"⁦"+m+"⁩");
    if(it.pill){ const s=document.createElement("span"); s.className="pill"; s.textContent=txt; d.appendChild(s); }
    else d.textContent=txt;
    st.appendChild(d);
  }
}
</script></body></html>"""


@dataclass
class Layer:
    image: Image.Image   # RGBA, full 1080x1920 canvas
    box: tuple[int, int, int, int]  # x, y, w, h of the text card


class TextRenderer:
    def __init__(self, cfg):
        self.cfg = cfg
        self._pw = None
        self._browser = None
        self._page = None
        self._fonts = None

    def __enter__(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        self._page = self._browser.new_page(viewport={"width": 1080, "height": 1920})
        html = cache_dir(self.cfg, "render") / "page.html"
        html.write_text(PAGE, encoding="utf-8")
        self._page.goto(html.as_uri())
        return self

    def __exit__(self, *exc):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    def use_fonts(self, quran_font: str, label_font: str = "Amiri-Regular.ttf",
                  bracket_font: str = "AmiriQuran-Regular.ttf"):
        key = (quran_font, label_font, bracket_font)
        if self._fonts == key:
            return
        uris = [(FONTS_DIR / f).as_uri() for f in key]
        self._page.evaluate("([q,l,b]) => useFonts(q,l,b)", uris)
        self._fonts = key

    def _shot(self) -> Image.Image:
        png = self._page.screenshot(omit_background=True, full_page=False)
        return Image.open(io.BytesIO(png)).convert("RGBA")

    def layout_ayah(self, words: list[str], marker: str | None, *, style: str, color: str,
                    accent: str, size: int, center_y: int, max_h: int, open_bracket: bool = True,
                    close_bracket: bool = True, attach_marks: bool = False, weight: int = 400,
                    min_size: int = 56) -> dict:
        p = {"words": words, "marker": marker, "style": style, "color": color, "accent": accent,
             "size": size, "minSize": min_size, "centerY": center_y, "maxH": max_h,
             "open": open_bracket, "close": close_bracket, "attach": attach_marks, "weight": weight}
        info = self._page.evaluate("p => layoutAyah(p)", p)
        if info["overflow"]:
            log.warning("ayah text overflows max height even at %spx", info["size"])
        return info

    def state(self, info: dict, *, up_to_line: int | None = None, word: int | None = None) -> Layer:
        s: dict = {}
        if up_to_line is not None:
            s["upToLine"] = up_to_line
        if word is not None:
            s["word"] = word
        self._page.evaluate("([s,l]) => setState(s,l)", [s, info["line"]])
        x, y, w, h = (int(round(v)) for v in info["box"])
        return Layer(self._shot(), (x, y, w, h))

    def labels(self, items: list[dict]) -> Image.Image:
        self._page.evaluate("items => labels(items)", items)
        return self._shot()
