/**
 * Telegram-webhook -> GitHub repository_dispatch.
 *
 * Kun klippivahdin ilmoitukseen vastataan Telegramissa numerolla 1 tai 2,
 * tämä Worker poimii alkuperäisestä viestistä Kick-klippilinkin ja
 * käynnistää "Rajaa klippi" -workflown GitHubissa.
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

const MODEL_NAMES = { "1": "zoomattu", "2": "koko kuva" };

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

    const msg = update.message || update.edited_message;
    if (!msg || !msg.text) return ok();

    // Vain sallitusta chatista. Muut jätetään huomiotta hiljaisesti.
    if (String(msg.chat?.id) !== String(env.ALLOWED_CHAT_ID)) return ok();

    const model = msg.text.trim();
    if (!MODEL_NAMES[model]) return ok();

    const source = msg.reply_to_message;
    if (!source) {
      await reply(env, msg, "Vastaa numerolla siihen klippi-ilmoitukseen, jonka haluat rajata.");
      return ok();
    }

    const sourceText = source.text || source.caption || "";
    const kick = sourceText.match(KICK_CLIP_RE);

    if (!kick) {
      const note = TWITCH_CLIP_RE.test(sourceText)
        ? "Twitch-klippejä ei voi rajata — putki toimii vain Kickillä."
        : "En löytänyt viestistä Kick-klippilinkkiä.";
      await reply(env, msg, note);
      return ok();
    }

    const dispatched = await dispatch(env, {
      clip_url: kick[0],
      model,
      message_id: msg.message_id,
    });

    await reply(
      env,
      msg,
      dispatched
        ? `Rajaus käynnistetty — malli ${model} (${MODEL_NAMES[model]}). Video tulee tähän kun se on valmis.`
        : "GitHubin käynnistys epäonnistui. Katso Workerin loki."
    );

    return ok();
  },
};

function ok() {
  return new Response("ok", { status: 200 });
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

async function reply(env, msg, text) {
  await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      chat_id: msg.chat.id,
      text,
      reply_to_message_id: msg.message_id,
    }),
  });
}
