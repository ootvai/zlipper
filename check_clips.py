"""
Klippivahti — seuraa Twitch- ja Kick-kanavia ja ilmoittaa Telegramiin,
kun jollekin seuratulle kanavalle syntyy uusi klippi.

Ajetaan ajastetusti GitHub Actionsilla. Jo nähdyt klipit muistetaan
state.json-tiedostossa, joka commitataan takaisin repoon.

Ympäristömuuttujat (GitHub secrets):
  TWITCH_CLIENT_ID
  TWITCH_CLIENT_SECRET
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""

import json
import os
from datetime import datetime, timedelta, timezone

import requests

CONFIG_PATH = "config.json"
STATE_PATH = "state.json"

TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

KICK_HEADERS = {
    # Kickin klippi-endpoint ei ole osa virallista docs.kick.com -APIa.
    # Voi lakata toimimasta ilman varoitusta.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

MAX_REMEMBERED_PER_CHANNEL = 300


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


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


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("VAROITUS: Telegram-token tai chat_id puuttuu, ei lähetetä.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=15,
        )
        if r.status_code >= 300:
            print(f"Telegram epäonnistui ({r.status_code}): {r.text[:300]}")
    except requests.RequestException as e:
        print(f"Telegram-virhe: {e}")


# ---------------------------------------------------------------------------
# TWITCH  (virallinen Helix API — vakaa)
# ---------------------------------------------------------------------------

def get_twitch_token():
    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        print("Twitch-tunnukset puuttuvat, ohitetaan Twitch.")
        return None
    try:
        r = requests.post(
            "https://id.twitch.tv/oauth2/token",
            params={
                "client_id": TWITCH_CLIENT_ID,
                "client_secret": TWITCH_CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
            timeout=15,
        )
        r.raise_for_status()
        return r.json()["access_token"]
    except (requests.RequestException, KeyError) as e:
        print(f"Twitch-token epäonnistui: {e}")
        return None


def twitch_headers(token):
    return {"Client-Id": TWITCH_CLIENT_ID, "Authorization": f"Bearer {token}"}


def get_twitch_user_ids(usernames, token):
    if not usernames:
        return {}
    ids = {}
    # Helix sallii max 100 loginia per kutsu
    for i in range(0, len(usernames), 100):
        batch = usernames[i:i + 100]
        try:
            r = requests.get(
                "https://api.twitch.tv/helix/users",
                headers=twitch_headers(token),
                params=[("login", u) for u in batch],
                timeout=15,
            )
            r.raise_for_status()
            for u in r.json().get("data", []):
                ids[u["login"].lower()] = u["id"]
        except requests.RequestException as e:
            print(f"Twitch users -haku epäonnistui: {e}")
    return ids


def get_twitch_clips(broadcaster_id, token, since_iso):
    try:
        r = requests.get(
            "https://api.twitch.tv/helix/clips",
            headers=twitch_headers(token),
            params={
                "broadcaster_id": broadcaster_id,
                "started_at": since_iso,
                "first": 50,
            },
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("data", [])
    except requests.RequestException as e:
        print(f"Twitch clips -haku epäonnistui: {e}")
        return []


def format_twitch_message(channel, clip):
    title = escape_html(clip.get("title") or "Uusi klippi")
    creator = escape_html(clip.get("creator_name") or "?")
    views = clip.get("view_count", 0)
    duration = clip.get("duration", 0)
    return (
        f"🟣 <b>Twitch · {escape_html(channel)}</b>\n"
        f"<b>{title}</b>\n"
        f"Klippasi: {creator} · {duration:.0f}s · {views} katselua\n"
        f"{clip['url']}"
    )


# ---------------------------------------------------------------------------
# KICK  (epävirallinen endpoint)
# ---------------------------------------------------------------------------

def get_kick_clips(channel):
    url = f"https://kick.com/api/v2/channels/{channel}/clips"
    try:
        r = requests.get(
            url,
            headers=KICK_HEADERS,
            params={"cursor": 0, "sort": "date"},
            timeout=20,
        )
        if r.status_code == 404:
            print(f"Kick: kanavaa '{channel}' ei löytynyt — tarkista nimi")
            return []
        if r.status_code != 200:
            print(f"Kick: {channel} -> HTTP {r.status_code}, ohitetaan")
            return []
        data = r.json()
        if isinstance(data, dict):
            return data.get("clips", [])
        if isinstance(data, list):
            return data
        return []
    except (requests.RequestException, ValueError) as e:
        print(f"Kick: {channel} -> virhe ({e}), ohitetaan")
        return []


def format_kick_message(channel, clip):
    clip_id = clip.get("id")
    title = escape_html(clip.get("title") or "Uusi klippi")
    creator = clip.get("creator") or {}
    creator_name = escape_html(creator.get("username") or "?")
    views = clip.get("views", 0)
    duration = clip.get("duration", 0)
    link = clip.get("clip_url") or f"https://kick.com/{channel}/clips/{clip_id}"
    return (
        f"🟢 <b>Kick · {escape_html(channel)}</b>\n"
        f"<b>{title}</b>\n"
        f"Klippasi: {creator_name} · {duration}s · {views} katselua\n"
        f"{link}"
    )


# ---------------------------------------------------------------------------

def prune(seen_list):
    return seen_list[-MAX_REMEMBERED_PER_CHANNEL:]


def main():
    config = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {"twitch": {}, "kick": {}})
    state.setdefault("twitch", {})
    state.setdefault("kick", {})

    lookback = config.get("poll_lookback_minutes", 15)
    since_iso = (
        datetime.now(timezone.utc) - timedelta(minutes=lookback)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    first_run = not state["twitch"] and not state["kick"]
    if first_run:
        print("Ensimmäinen ajo: merkitään nykyiset klipit nähdyiksi ilman ilmoituksia.")

    found = 0

    # --- Twitch ---
    twitch_channels = [c.lower().strip() for c in config.get("twitch_channels", [])]
    if twitch_channels:
        token = get_twitch_token()
        if token:
            ids = get_twitch_user_ids(twitch_channels, token)
            for channel in twitch_channels:
                bid = ids.get(channel)
                if not bid:
                    print(f"Twitch: kanavaa '{channel}' ei löytynyt — tarkista nimi")
                    continue
                seen = state["twitch"].get(channel, [])
                seen_set = set(seen)
                clips = get_twitch_clips(bid, token, since_iso)
                new = [c for c in clips if c["id"] not in seen_set]
                new.sort(key=lambda c: c.get("created_at", ""))
                for clip in new:
                    if not first_run:
                        send_telegram(format_twitch_message(channel, clip))
                        found += 1
                    seen.append(clip["id"])
                state["twitch"][channel] = prune(seen)

    # --- Kick ---
    for channel in [c.lower().strip() for c in config.get("kick_channels", [])]:
        seen = state["kick"].get(channel, [])
        seen_set = set(seen)
        clips = get_kick_clips(channel)
        new = [c for c in clips if str(c.get("id")) not in seen_set]
        new.sort(key=lambda c: c.get("created_at", ""))
        for clip in new:
            if not first_run:
                send_telegram(format_kick_message(channel, clip))
                found += 1
            seen.append(str(clip.get("id")))
        state["kick"][channel] = prune(seen)

    save_json(STATE_PATH, state)

    if first_run:
        print("Perustila tallennettu. Seuraavasta ajosta alkaen tulee ilmoituksia.")
    elif found:
        print(f"{found} uutta klippiä, ilmoitukset lähetetty.")
    else:
        print("Ei uusia klippejä.")


if __name__ == "__main__":
    main()
