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
//
// Control group (more people in control): add the bot to a Telegram group with them and make it
// an admin there; the owner gets a "use this group" button privately. From then on the bot posts
// in the group and obeys the group + the owner's private chat. GET /config tells the GitHub jobs.

const MAKE_CRON = "7 */3 * * *";   // must match tools/deploy_worker.py
const MENU_URL = (env) => `https://raw.githubusercontent.com/${env.REPO}/main/data/bot_menu.json`;
const COUNTS = [1, 3, 5, 10];

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    if (req.method === "POST" && url.pathname === "/tg") return telegram(req, env, ctx);
    if (url.pathname === "/updates" || url.pathname === "/ack" || url.pathname === "/config") {
      if (req.headers.get("x-worker-key") !== env.WORKER_KEY) return new Response("forbidden", { status: 403 });
      if (url.pathname === "/config")
        return Response.json({ control_chat: (await env.UPDATES.get("control_chat")) || "" });
      return url.pathname === "/updates" ? listUpdates(env) : ack(req, env);
    }
    return new Response("ok");
  },
  // Cron trigger (every 10 min): GitHub's own schedule fires only about once an hour, so the
  // Worker keeps time — the 2-hour agent timer and the Quran auto-checks then run on time.
  async scheduled(event, env, ctx) {
    // "7 */3 * * *": make the next Quran video(s) (daily.yml); every 10 min: the review poller
    ctx.waitUntil(event.cron === MAKE_CRON ? dispatch(env, null, "daily.yml") : dispatch(env, null));
  },
};

// ---------------------------------------------------------------- Telegram webhook
async function telegram(req, env, ctx) {
  if (req.headers.get("x-telegram-bot-api-secret-token") !== env.TG_SECRET)
    return new Response("forbidden", { status: 403 });
  const u = await req.json();
  const cq = u.callback_query, msg = u.message;
  const chat = String((cq && cq.message && cq.message.chat.id) || (msg && msg.chat.id) ||
                      (u.my_chat_member && u.my_chat_member.chat.id) || "");
  const control = (await env.UPDATES.get("control_chat")) || "";
  const owner = String(env.CHAT_ID);
  if (msg && msg.migrate_to_chat_id && chat === control) {        // group became a supergroup: new id
    await env.UPDATES.put("control_chat", String(msg.migrate_to_chat_id));
    return new Response("ok");
  }
  if (msg && msg.migrate_from_chat_id && String(msg.migrate_from_chat_id) === control) {
    await env.UPDATES.put("control_chat", chat);
    return new Response("ok");
  }
  if (cq && cq.data && cq.data.startsWith("link:") && chat === owner) {   // owner: "use this group"
    ctx.waitUntil(linkGroup(env, cq));
    return new Response("ok");
  }
  if (chat !== owner && chat !== control) {      // anyone else is ignored; a new group is offered
    const c = (msg && msg.chat) || (u.my_chat_member && u.my_chat_member.chat);
    if (c && (c.type === "group" || c.type === "supergroup")) ctx.waitUntil(offerGroup(env, c));
    return new Response("ignored");
  }
  if (!cq && !msg) return new Response("ok");

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
  if (msg && /^\/generate(@\w+)?\s*$/.test(msg.text || "")) {
    ctx.waitUntil(sendMenu(env, msg.chat.id));
    return new Response("ok");
  }
  // everything else: keep it for the poller and start it now
  await env.UPDATES.put(`u:${String(u.update_id).padStart(12, "0")}`, JSON.stringify(u),
                        { expirationTtl: 7 * 86400 });
  if (cq) {
    const note = cq.data === "g:que" ? "📊 ثواني وتوصلك الأرقام" :
                 cq.data.startsWith("f:") ? "🤖 المصحح بدأ — النسخة المصححة هتوصلك خلال دقايق" :
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
  const p = d.split(":");          // ... or g:h:<topic>:<count> (hadith), g:k:<count> (kids)
  return (p[1] === "q" && p.length === 5) || (p[1] === "P" && p.length === 4) || (p[1] === "h" && p.length === 4) ||
    (p[1] === "k" && p.length === 3);
}

// The update itself rides along with the run (KV *listing* can lag ~60 s behind writes, so the
// poller would often not see it yet); KV stays the backup for runs that get superseded.
async function dispatch(env, u, workflow = "telegram.yml") {
  const update = u ? JSON.stringify(u) : "";
  const r = await fetch(`https://api.github.com/repos/${env.REPO}/actions/workflows/${workflow}/dispatches`, {
    method: "POST",
    headers: { authorization: `Bearer ${env.GH_TOKEN}`, accept: "application/vnd.github+json",
               "user-agent": "ezdadd-bot", "content-type": "application/json" },
    body: JSON.stringify({ ref: "main", inputs: update && update.length < 60000 ? { update } : {} }),
  });
  if (!r.ok) console.log("dispatch failed", r.status, await r.text());
}

// ---------------------------------------------------------------- poller API
// ---------------------------------------------------------------- control group
async function offerGroup(env, c) {
  const id = String(c.id);
  if (await env.UPDATES.get(`grp:${id}`)) return;              // asked about this group already
  await env.UPDATES.put(`grp:${id}`, JSON.stringify({ title: c.title || "" }), { expirationTtl: 7 * 86400 });
  await tg(env, "sendMessage", { chat_id: env.CHAT_ID,
    text: `🤝 البوت اتضاف لجروب «${c.title || id}».\nلو عايز الجروب ده يبقى جروب التحكم (الفيديوهات والأزرار ` +
          "تروح هناك، وكل اللي في الجروب يقدر يوافق ويرفض ويعدّل ويبعت لينكات)، دوس تحت.\n" +
          "مهم: خلّي البوت أدمن في الجروب عشان يشوف الرسايل واللينكات.",
    reply_markup: { inline_keyboard: [[{ text: "✅ استخدم الجروب ده", callback_data: `link:${id}` }]] } });
}

async function linkGroup(env, cq) {
  const id = cq.data.slice(5);
  const g = await env.UPDATES.get(`grp:${id}`, "json");
  if (!g) {
    await tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text: "انتهت — شيل البوت من الجروب وضيفه تاني" });
    return;
  }
  await env.UPDATES.put("control_chat", id);
  await Promise.all([
    tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text: "اتربط ✅" }),
    tg(env, "editMessageText", { chat_id: cq.message.chat.id, message_id: cq.message.message_id,
      text: `✅ جروب «${g.title || id}» بقى جروب التحكم. الفيديوهات الجديدة والأزرار هتروح هناك، ` +
            "وتقدر برضو تكلم البوت هنا عادي." }),
    tg(env, "sendMessage", { chat_id: id,
      text: "✅ الجروب ده بقى جروب التحكم في ازْدَدْ.\nأي حد هنا يقدر يوافق ✅ ويرفض ❌ ويعدّل الترجمة ويبعت " +
            "لينكات ويستخدم /generate و /que. الفيديوهات الجديدة هتوصل هنا." }),
  ]);
  await dispatch(env, null);                   // the GitHub side picks the group up right away
}

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
    return ["⚙️ عايز تعمل إيه؟", kb([[["📖 آيات قرآن", "g:q"]], [["📜 حديث شريف", "g:h"]], ...(m.kids ? [[["🧒 قصة للأطفال (حديث)", "g:k"]]] : []),
                                     [["🎙 مقطع من لينك", "g:l"]],
                                     [["📊 الطابور (/que)", "g:que"]]])];
  if (data === "g:q")
    return ["📖 آيات قرآن — اختار الموضوع، أو اكتب سورة وآيات بعينها:",
            kb([[["🎲 أي موضوع (الأغلب حكمة وصبر ووعظ)", "g:q:-"]], ...pairs(m.themes.map((t, i) => [t, `g:q:${i}`])),
                [["✍️ سورة وآيات بالاسم", "g:s"]], [["⬅️ رجوع", "g:t"]]])];
  if (data === "g:h")
    return ["📜 حديث شريف (صحيح أو حسن بس، بمصدره ودرجته) — اختار الموضوع:",
            kb([[["🎲 أي موضوع", "g:h:-"]], ...pairs((m.hadith_topics || []).map((t, i) => [t, `g:h:${i}`])),
                [["⬅️ رجوع", "g:t"]]])];
  if (data === "g:k")
    return ["🧒 قصة مرسومة للأطفال بتشرح حديث صحيح (الحديث بنصه، والقصة والرسوم بالذكاء الاصطناعي) — كام فيديو؟",
            kb([[1, 2, 3].map((n) => [String(n), `g:k:${n}`]), [["⬅️ رجوع", "g:t"]]])];
  if (p[1] === "h" && p.length === 3) {
    const name = p[2] === "-" ? "أي موضوع" : (m.hadith_topics || [])[+p[2]];
    return [`📜 ${name}\nكام فيديو؟`, kb([[1, 2, 3, 5].map((n) => [String(n), `${data}:${n}`]), [["⬅️ رجوع", "g:h"]]])];
  }
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
  await tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text: "" });
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
