"""Telegram-lähetys, joka ei hukkaa viestejä.

Bot API päästää läpi noin yhden viestin sekunnissa per chat. Klippiryöpyssä
(striimi käynnissä, klippejä tulee kymmenittäin) raja ylittyy ja Telegram
vastaa 429:llä. Aiemmin viesti katosi silloin kokonaan: klippi oli jo
merkitty nähdyksi state.jsoniin, joten sitä ei koskaan yritetty uudelleen.

Lähetysfunktiot palauttavat siksi aina yhden kolmesta tuloksesta, jotta
kutsuja voi päättää merkitseekö klipin käsitellyksi:

  SENT      perillä
  REJECTED  Telegram hylkäsi pysyvästi (esim. rikkinäinen HTML tai väärä
            chat_id) — uusinta ei auta, joten klippi kannattaa merkitä
            nähdyksi silti, ettei se jää ikuiseen kierteeseen
  FAILED    tilapäinen vika (429, 5xx, verkkokatko) — kannattaa yrittää
            uudestaan seuraavalla ajolla
"""

import json
import os
import time

import requests

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SENT = "sent"
REJECTED = "rejected"
FAILED = "failed"

# Telegramin oma raja on ~1 viesti/s per chat. Pieni marginaali päälle,
# koska rajaa mitataan palvelimen päässä eikä meidän kellolla.
MIN_INTERVAL_S = 1.2

MAX_ATTEMPTS = 4

# Jos Telegram pyytää odottamaan tätä kauemmin, ei jäädä notkumaan —
# GitHub Actions -ajo on lyhyt ja klippi yritetään uudestaan 10 min päästä.
MAX_RETRY_AFTER_S = 45

_last_send = 0.0


def escape_html(text):
    """Telegramin HTML-parse_mode vaatii näiden escapetuksen."""
    if not text:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _throttle():
    """Pitää huolen ettei kahta viestiä lähetetä liian tiheästi peräkkäin."""
    global _last_send
    wait = MIN_INTERVAL_S - (time.monotonic() - _last_send)
    if wait > 0:
        time.sleep(wait)
    _last_send = time.monotonic()


def _retry_after(response):
    """Telegramin pyytämä odotusaika sekunteina, tai None."""
    try:
        value = response.json().get("parameters", {}).get("retry_after")
    except ValueError:
        value = None
    if value is None:
        value = response.headers.get("Retry-After")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _call(method, data=None, files=None, timeout=30):
    """Kutsuu Bot APIa uudelleenyrityksin. Palauttaa (tulos, vastaus-json)."""
    if not BOT_TOKEN or not CHAT_ID:
        print("VAROITUS: Telegram-token tai chat_id puuttuu, ei lähetetä.")
        return REJECTED, None

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    backoff = 2.0

    for attempt in range(1, MAX_ATTEMPTS + 1):
        _throttle()
        try:
            r = requests.post(url, data=data, files=files, timeout=timeout)
        except requests.RequestException as e:
            print(f"Telegram-virhe ({method}, yritys {attempt}): {e}")
            time.sleep(backoff)
            backoff *= 2
            continue

        if r.status_code == 200:
            try:
                return SENT, r.json()
            except ValueError:
                return SENT, None

        if r.status_code == 429:
            wait = _retry_after(r) or backoff
            if wait > MAX_RETRY_AFTER_S:
                print(
                    f"Telegram 429: pyytää {wait:.0f} s odotusta — liian "
                    f"pitkä, yritetään seuraavalla ajolla."
                )
                return FAILED, None
            print(f"Telegram 429: odotetaan {wait:.0f} s ja yritetään uudelleen.")
            time.sleep(wait + 0.5)
            continue

        if r.status_code >= 500:
            print(f"Telegram {r.status_code} ({method}), yritys {attempt}.")
            time.sleep(backoff)
            backoff *= 2
            continue

        # Muut 4xx ovat meidän virheitämme — sama pyyntö epäonnistuu aina.
        print(f"Telegram hylkäsi ({r.status_code}, {method}): {r.text[:300]}")
        return REJECTED, None

    print(f"Telegram: {method} ei mennyt läpi {MAX_ATTEMPTS} yrityksellä.")
    return FAILED, None


def send_message(text, reply_markup=None, disable_web_page_preview=False):
    data = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true" if disable_web_page_preview else "false",
    }
    if reply_markup is not None:
        data["reply_markup"] = reply_markup
    status, _ = _call("sendMessage", data=data)
    return status


def send_video(path, caption="", reply_to_message_id=None):
    """Lähettää videon tiedostona. Bot API:n yläraja on 50 MB."""
    data = {
        "chat_id": CHAT_ID,
        "caption": caption,
        "parse_mode": "HTML",
        "supports_streaming": "true",
    }
    if reply_to_message_id:
        data["reply_to_message_id"] = str(reply_to_message_id)
    with open(path, "rb") as f:
        status, _ = _call(
            "sendVideo",
            data=data,
            files={"video": (os.path.basename(path), f, "video/mp4")},
            timeout=300,
        )
    return status


def send_document(path, caption="", reply_to_message_id=None):
    """Lähettää tiedoston koskemattomana. Bot API:n yläraja on 50 MB.

    sendVideo antaa Telegramille luvan käsitellä tiedostoa omalla
    tavallaan. sendDocument ei: vastaanottaja saa täsmälleen ne tavut
    jotka lähetettiin. Siksi rajaamaton klippi menee tätä kautta.
    """
    data = {
        "chat_id": CHAT_ID,
        "caption": caption,
        "parse_mode": "HTML",
    }
    if reply_to_message_id:
        data["reply_to_message_id"] = str(reply_to_message_id)
    with open(path, "rb") as f:
        status, _ = _call(
            "sendDocument",
            data=data,
            files={"document": (os.path.basename(path), f, "video/mp4")},
            timeout=300,
        )
    return status

# ---------------------------------------------------------------------------
# Inline-napit
#
# callback_data pidetään lyhyenä: Telegramin raja on 64 tavua, ja jos se
# ylittyy, koko sendMessage hylätään 400:lla eli ilmoitus katoaisi. Klippi-
# linkkiä ei siis pakata nappiin — Worker lukee sen viestin tekstistä.
# ---------------------------------------------------------------------------

def crop_keyboard():
    """Napit, jotka korvaavat numerovastauksen klippi-ilmoituksessa."""
    return json.dumps(
        {
            "inline_keyboard": [
                [
                    {"text": "🔍 Zoomattu", "callback_data": "crop:1"},
                    {"text": "🖼️ Koko kuva", "callback_data": "crop:2"},
                ],
                [
                    {"text": "⬇️ Lataa klippi", "callback_data": "crop:3"},
                ],
            ]
        }
    )


def status_keyboard(label):
    """Yksi nappi, joka vain kertoo viestin tilan (⏳ / ✅).

    Nappi jää painettavaksi, mutta Worker vastaa "noop"-dataan pelkällä
    kuittauksella. Näin napit eivät jää houkuttelemaan toiseen ajoon
    samasta klipistä.
    """
    return json.dumps(
        {"inline_keyboard": [[{"text": label, "callback_data": "noop"}]]}
    )


def edit_reply_markup(message_id, reply_markup):
    """Vaihtaa jo lähetetyn viestin napit. Telegram sallii 48 h ajan."""
    status, _ = _call(
        "editMessageReplyMarkup",
        data={
            "chat_id": CHAT_ID,
            "message_id": str(message_id),
            "reply_markup": reply_markup,
        },
    )
    return status
