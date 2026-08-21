/**
 * Telegram-webhook -> GitHub repository_dispatch.
 *
 * Klippi-ilmoituksessa on kolme nappia: [Zoomattu], [Koko kuva] ja
 * [Lataa klippi] (klippi sellaisenaan, ilman rajausta). Napin
 * painallus tulee tänne callback_query-päivityksenä, ja tämä Worker poimii
 * alkuperäisestä viestistä Kick-klippilinkin ja käynnistää "Rajaa klippi"
 * -workflown GitHubissa.
 *
 * Vanha tapa — vastaa ilmoitukseen numerolla 1, 2 tai 3 — toimii yhä. Ennen
 * nappien käyttöönottoa lähetetyissä ilmoituksissa ei ole nappeja, ja
 * numerovastaus on myös varakeino jos nappi ei jostain syystä toimi.
 *
 * Ympäristö (wrangler secret put / vars):
 *   TELEGRAM_BOT_TOKEN       botin token, kuittausviestien lähettämiseen
 *   TELEGRAM_WEBHOOK_SECRET  setWebhookin secret_token, tarkistetaan joka pyynnöstä
 *   GITHUB_TOKEN             PAT, oikeus Contents: read & write kohderepoon
 *   GITHUB_REPO              esim. "ootvai/zlipper"
 *   ALLOWED_CHAT_ID          vain tämä chat saa laukaista ajoja
 */

const KICK_CLIP_RE = /https?:\/\/kick\.com\/[A-Za-z0-9_.-]+\/clips\/[A-Za-z0-9_-]+/;
const TWITCH_CLIP_RE = /https?:\/\/(clips\.twitch\.tv|www\.twitch\.tv)\/\S+/;

const MODEL_NAMES = { "1": "zoomattu", "2": "koko kuva", "3": "lataus" };

// Malli 3 ei rajaa mitään, joten "Rajataan" olisi siitä harhaanjohtavaa.
function busyLabel(model) {
  return model === "3"
    ? "⏳ Ladataan klippiä"
    : `⏳ Rajataan: ${MODEL_NAMES[model]}`;
}

function startedText(model) {
  return model === "3"
    ? "Ladataan klippiä — tiedosto tulee tähän kun se on valmis."
    : `Rajaus käynnistetty — ${MODEL_NAMES[model]}.`;
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("zlipper webhook", { status: 200 });
    }

    // Telegram lähettää tämän otsakkeen, kun webhook on rekisteröity
    // secret_tokenin kanssa. Ilman tarkistusta kuka tahansa voisi
    // laukaista Actions-ajoja pelkällä Worker-osoitteella.
    const given = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (!env.TELEGRAM_WEBHOOK_SECRET || given !== env.TELEGRAM_WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return ok();
    }

    if (update.callback_query) {
      return handleButton(env, update.callback_query);
    }
    return handleReply(env, update.message || update.edited_message);
  },
};

// ---------------------------------------------------------------------------
// Napit
// ---------------------------------------------------------------------------

async function handleButton(env, query) {
  const msg = query.message;

  // Tilanappi (⏳ / ✅) ei tee mitään — kuitataan vain, jotta Telegramin
  // latauskehrä pysähtyy.
  if (query.data === "noop") {
    await answer(env, query, "Tämä nappi näyttää vain tilan.");
    return ok();
  }

  const match = /^crop:([123])$/.exec(query.data || "");
  if (!match) {
    await answer(env, query);
    return ok();
  }
  const model = match[1];

  if (!msg || String(msg.chat?.id) !== String(env.ALLOWED_CHAT_ID)) {
    await answer(env, query);
    return ok();
  }

  // Telegram jättää viestin sisällön pois, jos viesti on hyvin vanha.
  // Silloin linkkiä ei saada napista, mutta numerovastaus toimii yhä.
  const kick = KICK_CLIP_RE.exec(msg.text || msg.caption || "");
  if (!kick) {
    await answer(
      env,
      query,
      "En saanut klippilinkkiä tästä viestistä. Vastaa viestiin numerolla 1, 2 tai 3.",
      true
    );
    return ok();
  }

  const dispatched = await dispatch(env, {
    clip_url: kick[0],
    model,
    message_id: msg.message_id,
    // Napit ovat itse ilmoituksessa, joten process_clip.py päivittää ne
    // samaan viestiin kun rajaus on valmis tai kaatuu.
    markup_message_id: msg.message_id,
  });

  if (!dispatched) {
    await answer(env, query, "GitHubin käynnistys epäonnistui. Katso Workerin loki.", true);
    return ok();
  }

  // Ei erillistä kuittausviestiä: tila näkyy itse ilmoituksessa, joten
  // chattiin ei kerry rinnalle ylimääräisiä rivejä. process_clip.py
  // vaihtaa napin tekstin uudestaan kun video on valmis tai rajaus kaatuu.
  await answer(env, query, startedText(model));
  await setButtons(env, msg.chat.id, msg.message_id, statusKeyboard(busyLabel(model)));
  return ok();
}

// ---------------------------------------------------------------------------
// Numerovastaus (vanhat ilmoitukset ja varakeino)
// ---------------------------------------------------------------------------

async function handleReply(env, msg) {
  if (!msg || !msg.text) return ok();

  // Vain sallitusta chatista. Muut jätetään huomiotta hiljaisesti.
  if (String(msg.chat?.id) !== String(env.ALLOWED_CHAT_ID)) return ok();

  const model = msg.text.trim();
  if (!MODEL_NAMES[model]) return ok();

  const source = msg.reply_to_message;
  if (!source) {
    await reply(env, msg, "Vastaa numerolla siihen klippi-ilmoitukseen, jota tarkoitat.");
    return ok();
  }

  const sourceText = source.text || source.caption || "";
  const kick = KICK_CLIP_RE.exec(sourceText);

  if (!kick) {
    const note = TWITCH_CLIP_RE.test(sourceText)
      ? "Twitch-klippejä ei voi rajata — putki toimii vain Kickillä."
      : "En löytänyt viestistä Kick-klippilinkkiä.";
    await reply(env, msg, note);
    return ok();
  }

  // Ennen nappien käyttöönottoa lähetetyissä ilmoituksissa ei ole nappeja,
  // eikä niihin pidä niitä lisätä: jäljelle jäisi tilanappi jota kukaan ei
  // enää päivitä. Vaihdetaan napit vain jos niitä on.
  const hasButtons = Boolean(source.reply_markup);

  const dispatched = await dispatch(env, {
    clip_url: kick[0],
    model,
    message_id: msg.message_id,
    markup_message_id: hasButtons ? source.message_id : undefined,
  });

  await reply(
    env,
    msg,
    dispatched
      ? `${startedText(model)} (malli ${model})`
      : "GitHubin käynnistys epäonnistui. Katso Workerin loki."
  );

  if (dispatched && hasButtons) {
    // Ettei samaa klippiä aja vahingossa vielä napistakin.
    await setButtons(env, source.chat.id, source.message_id, statusKeyboard(busyLabel(model)));
  }

  return ok();
}

// ---------------------------------------------------------------------------

function ok() {
  return new Response("ok", { status: 200 });
}

function statusKeyboard(label) {
  return { inline_keyboard: [[{ text: label, callback_data: "noop" }]] };
}

async function dispatch(env, payload) {
  const url = `https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`;
  const r = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
      "User-Agent": "zlipper-webhook",
    },
    body: JSON.stringify({ event_type: "process_clip", client_payload: payload }),
  });
  if (r.status !== 204) {
    console.log("repository_dispatch epäonnistui", r.status, await r.text());
    return false;
  }
  return true;
}

async function telegram(env, method, body) {
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    console.log(`${method} epäonnistui`, r.status, await r.text());
  }
  return r.ok;
}

async function reply(env, msg, text) {
  return telegram(env, "sendMessage", {
    chat_id: msg.chat.id,
    text,
    reply_to_message_id: msg.message_id,
  });
}

async function answer(env, query, text, alert = false) {
  return telegram(env, "answerCallbackQuery", {
    callback_query_id: query.id,
    text: text || "",
    show_alert: alert,
  });
}

async function setButtons(env, chatId, messageId, markup) {
  // Epäonnistuu esim. yli 48 h vanhalle viestille tai jos napit ovat jo
  // samat. Kumpikaan ei ole vika, joten virhe vain lokitetaan.
  return telegram(env, "editMessageReplyMarkup", {
    chat_id: chatId,
    message_id: messageId,
    reply_markup: markup,
  });
}
