// Instant front door for the ازْدَدْ Telegram bot (Cloudflare Worker, free plan).
//
// Telegram sends every update here (webhook) the moment it happens:
//   * the /generate menu steps are answered right here, instantly;
//   * every other update (approve / reject, links, edits, /que ...) is stored in KV and the
//     GitHub "telegram-review" workflow is started immediately to process it (~1 min);
//   * button presses get an instant "got it" toast.
// The GitHub poller reads the stored updates from GET /updates and deletes them with POST /ack.
//
// Bindings: KV namespace UPDATES. Secrets: TG_TOKEN, TG_SECRET (webhook header), CHAT_ID,
// WORKER_KEY (for /updates and /ack), GH_TOKEN (actions:write), REPO ("owner/name").

const MENU_URL = (env) => `https://raw.githubusercontent.com/${env.REPO}/main/data/bot_menu.json`;
const COUNTS = [1, 3, 5, 10];

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    if (req.method === "POST" && url.pathname === "/tg") return telegram(req, env, ctx);
    if (url.pathname === "/updates" || url.pathname === "/ack") {
      if (req.headers.get("x-worker-key") !== env.WORKER_KEY) return new Response("forbidden", { status: 403 });
      return url.pathname === "/updates" ? listUpdates(env) : ack(req, env);
    }
    return new Response("ok");
  },
};

// ---------------------------------------------------------------- Telegram webhook
async function telegram(req, env, ctx) {
  if (req.headers.get("x-telegram-bot-api-secret-token") !== env.TG_SECRET)
    return new Response("forbidden", { status: 403 });
  const u = await req.json();
  const cq = u.callback_query, msg = u.message;
  const chat = String((cq && cq.message && cq.message.chat.id) || (msg && msg.chat.id) || "");
  if (chat !== String(env.CHAT_ID)) return new Response("ignored");   // only the owner is obeyed

  if (cq && cq.data === "g:s") {               // ask for the passage (your reply goes to the poller)
    ctx.waitUntil(Promise.all([
      tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text: "اكتب السورة" }),
      tg(env, "editMessageText", { chat_id: chat, message_id: cq.message.message_id,
                                   text: "✍️ مستني اسم السورة والآيات (تحت)." }),
      tg(env, "sendMessage", { chat_id: chat,
        text: `${ASK_PASSAGE} كرد على الرسالة دي، مثلًا:\nالكهف 1-10\nالبقرة 286\n18:1-10\nأو اسم السورة بس وأنا أختار مقطع منها.`,
        reply_markup: { force_reply: true, input_field_placeholder: "الكهف 1-10" } }),
    ]));
    return new Response("ok");
  }
  if (cq && cq.data && cq.data.startsWith("g:") && !isFinal(cq.data)) {
    ctx.waitUntil(menuStep(env, cq));          // pure navigation: instant, nothing to store
    return new Response("ok");
  }
  if (msg && /^\/generate\s*$/.test(msg.text || "")) {
    ctx.waitUntil(sendMenu(env, msg.chat.id));
    return new Response("ok");
  }
  // everything else: keep it for the poller and start it now
  await env.UPDATES.put(`u:${String(u.update_id).padStart(12, "0")}`, JSON.stringify(u),
                        { expirationTtl: 7 * 86400 });
  if (cq) {
    const note = cq.data === "g:que" ? "📊 ثواني وتوصلك الأرقام" :
                 isFinal(cq.data) ? "اتسجل ✅ — بيبدأ خلال دقيقة" : "اتسجل ✅ — ثواني ويتحدث";
    ctx.waitUntil(tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text: note }));
    if (isFinal(cq.data) && cq.data !== "g:que")
      ctx.waitUntil(tg(env, "editMessageText", { chat_id: chat, message_id: cq.message.message_id,
                                                 text: "⏳ اتسجل — بيتجهز دلوقتي…" }));
  } else {
    ctx.waitUntil(tg(env, "sendChatAction", { chat_id: chat, action: "typing" }));
  }
  ctx.waitUntil(dispatch(env, u));
  return new Response("ok");
}

function isFinal(d) {     // g:q:<theme>:<sheikh>:<count>  or  g:P:<s.a-b>:<sheikh>  (g:que: needs the repo)
  if (d === "g:que") return true;
  const p = d.split(":");
  return (p[1] === "q" && p.length === 5) || (p[1] === "P" && p.length === 4);
}

// The update itself rides along with the run (KV *listing* can lag ~60 s behind writes, so the
// poller would often not see it yet); KV stays the backup for runs that get superseded.
async function dispatch(env, u) {
  const update = JSON.stringify(u);
  const r = await fetch(`https://api.github.com/repos/${env.REPO}/actions/workflows/telegram.yml/dispatches`, {
    method: "POST",
    headers: { authorization: `Bearer ${env.GH_TOKEN}`, accept: "application/vnd.github+json",
               "user-agent": "ezdadd-bot", "content-type": "application/json" },
    body: JSON.stringify({ ref: "main", inputs: update.length < 60000 ? { update } : {} }),
  });
  if (!r.ok) console.log("dispatch failed", r.status, await r.text());
}

// ---------------------------------------------------------------- poller API
async function listUpdates(env) {
  const out = [];
  let cursor;
  do {
    const page = await env.UPDATES.list({ prefix: "u:", cursor });
    for (const k of page.keys) {
      const v = await env.UPDATES.get(k.name);
      if (v) out.push(JSON.parse(v));
    }
    cursor = page.list_complete ? undefined : page.cursor;
  } while (cursor);
  out.sort((a, b) => a.update_id - b.update_id);
  return Response.json(out);
}

async function ack(req, env) {
  const { ids } = await req.json();
  await Promise.all((ids || []).map((id) => env.UPDATES.delete(`u:${String(id).padStart(12, "0")}`)));
  return Response.json({ deleted: (ids || []).length });
}

// ---------------------------------------------------------------- /generate menu (mirrors
// generate_menu() in src/review/telegram.py; themes and sheikhs come from data/bot_menu.json)
async function menuData(env) {
  const r = await fetch(MENU_URL(env), { cf: { cacheTtl: 300 } });
  return r.ok ? r.json() : { themes: [], reciters: [] };
}

const kb = (rows) => ({ inline_keyboard: rows.map((row) => row.map(([text, callback_data]) => ({ text, callback_data }))) });
// must start with ASK_PASSAGE in src/review/telegram.py: the poller recognises your reply by it
const ASK_PASSAGE = "✍️ اكتب اسم السورة والآيات";
const pairs = (items) => { const rows = []; for (let i = 0; i < items.length; i += 2) rows.push(items.slice(i, i + 2)); return rows; };

async function menu(env, data) {
  const m = await menuData(env);
  const p = data.split(":");
  const theme = (t) => (t === "-" ? "أي موضوع" : m.themes[+t]);
  const sheikhRows = (prefix, back) => [[["🎲 أي شيخ", `${prefix}:-`]],
    ...pairs(m.reciters.map(([, label], i) => [label, `${prefix}:${i}`])), [["⬅️ رجوع", back]]];
  if (data === "g:t")
    return ["⚙️ عايز تعمل إيه؟", kb([[["📖 آيات قرآن", "g:q"]], [["📜 حديث (قريبًا)", "g:h"]], [["🎙 مقطع من لينك", "g:l"]],
                                     [["📊 الطابور (/que)", "g:que"]]])];
  if (data === "g:q")
    return ["📖 آيات قرآن — اختار الموضوع، أو اكتب سورة وآيات بعينها:",
            kb([[["🎲 أي موضوع (الأغلب حكمة وصبر ووعظ)", "g:q:-"]], ...pairs(m.themes.map((t, i) => [t, `g:q:${i}`])),
                [["✍️ سورة وآيات بالاسم", "g:s"]], [["⬅️ رجوع", "g:t"]]])];
  if (data === "g:h")
    return ["📜 فيديوهات الأحاديث لسه بتتعمل: هتكون من مصادر موثقة بس (بدرجة الصحة والتخريج)، وأول ما تجهز هتظهر هنا.",
            kb([[["⬅️ رجوع", "g:t"]]])];
  if (data === "g:l")
    return ["🎙 ابعت اللينك (تيك توك / فيسبوك / إنستجرام / يوتيوب) أو الفيديو نفسه هنا في الشات، وممكن تضيف:\n" +
            "• وقت: 1:20-2:05\n• الشيخ ...\n• عنوان: ...\nالأطول من دقيقة بيتقسم أجزاء ورا بعض.", kb([[["⬅️ رجوع", "g:t"]]])];
  if (p[1] === "q" && p.length === 3) return [`📖 ${theme(p[2])} — اختار الشيخ:`, kb(sheikhRows(data, "g:q"))];
  if (p[1] === "q" && p.length === 4) {
    const who = p[3] === "-" ? "أي شيخ" : m.reciters[+p[3]][1];
    return [`📖 ${theme(p[2])} · ${who}\nكام فيديو؟`, kb([COUNTS.map((n) => [String(n), `${data}:${n}`]), [["⬅️ رجوع", `g:q:${p[2]}`]]])];
  }
  return null;
}

async function sendMenu(env, chat) {
  const [text, reply_markup] = await menu(env, "g:t");
  await tg(env, "sendMessage", { chat_id: chat, text, reply_markup });
}

async function menuStep(env, cq) {
  const step = await menu(env, cq.data);
  await tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text: cq.data === "g:h" ? "قريبًا" : "" });
  if (!step) return;
  await tg(env, "editMessageText", { chat_id: cq.message.chat.id, message_id: cq.message.message_id,
                                     text: step[0], reply_markup: step[1] });
}

async function tg(env, method, params) {
  const r = await fetch(`https://api.telegram.org/bot${env.TG_TOKEN}/${method}`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(params),
  });
  if (!r.ok) console.log(method, r.status, await r.text());
  return r;
}
