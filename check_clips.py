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
from zoneinfo import ZoneInfo

import requests

CONFIG_PATH = "config.json"
STATE_PATH = "state.json"
CREATORS_PATH = "creators.json"

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

# Ajokohtainen tilasto: montako hakuyritystä ja montako niistä epäonnistui.
# Käytetään terveysilmoitukseen — hiljaiset viat (Kickin endpoint hajoaa,
# Twitch-secret vanhenee) eivät muuten näy missään.
RUN_STATS = {
    "twitch": {"attempts": 0, "errors": 0, "details": []},
    "kick": {"attempts": 0, "errors": 0, "details": []},
}


def record(platform, ok, detail=""):
    stats = RUN_STATS[platform]
    stats["attempts"] += 1
    if not ok:
        stats["errors"] += 1
        if detail and detail not in stats["details"]:
            stats["details"].append(detail)


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


LOCAL_TZ = ZoneInfo("Europe/Helsinki")


def parse_ts(raw):
    """Jäsentää ISO-aikaleiman. Palauttaa None jos ei onnistu."""
    if not raw:
        return None
    txt = str(raw).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_time(raw):
    """'27.7. klo 21:05 (12 min sitten)' — Suomen aikaa."""
    dt = parse_ts(raw)
    if dt is None:
        return "aika tuntematon"
    local = dt.astimezone(LOCAL_TZ)
    stamp = local.strftime("%-d.%-m. klo %H:%M")

    mins = (datetime.now(timezone.utc) - dt).total_seconds() / 60
    if mins < 0:
        return stamp
    if mins < 60:
        ago = f"{int(mins)} min sitten"
    elif mins < 60 * 24:
        ago = f"{int(mins // 60)} h sitten"
    else:
        ago = f"{int(mins // (60 * 24))} vrk sitten"
    return f"{stamp} ({ago})"


def age_minutes(raw):
    """Klipin ikä minuutteina, tai None jos aikaa ei saada."""
    dt = parse_ts(raw)
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60


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

TOKEN_ERROR = []


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
        TOKEN_ERROR.append(str(e)[:120])
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


def get_twitch_clips(broadcaster_id, token, since_iso, max_pages=10):
    """Hakee kaikki aikaikkunan klipit sivuttamalla.

    HUOM: Twitch palauttaa klipit KATSELUKERTOJEN mukaan, ei aikajärjestyksessä.
    Uusi klippi (0-2 katselua) on siis listan pohjalla. Siksi pelkkä
    ensimmäinen sivu ei riitä isoilla kanavilla — pitää käydä läpi koko
    ikkuna, muuten tuoreimmat jäävät löytymättä.
    """
    clips = []
    cursor = None
    for _ in range(max_pages):
        params = {
            "broadcaster_id": broadcaster_id,
            "started_at": since_iso,
            "first": 100,  # API:n maksimi
        }
        if cursor:
            params["after"] = cursor
        try:
            r = requests.get(
                "https://api.twitch.tv/helix/clips",
                headers=twitch_headers(token),
                params=params,
                timeout=20,
            )
            r.raise_for_status()
            payload = r.json()
        except requests.RequestException as e:
            print(f"Twitch clips -haku epäonnistui: {e}")
            record("twitch", False, f"clips: {str(e)[:100]}")
            return clips

        page = payload.get("data", [])
        clips.extend(page)

        cursor = (payload.get("pagination") or {}).get("cursor")
        if not cursor or not page:
            break

    record("twitch", True)
    return clips


def format_twitch_message(channel, clip, count=0, threshold=0):
    title = escape_html(clip.get("title") or "Uusi klippi")
    creator = escape_html(clip.get("creator_name") or "?")
    views = clip.get("view_count", 0)
    duration = clip.get("duration", 0)
    star = "⭐ " if is_frequent(count, threshold) else ""
    note = creator_note(count, threshold)
    return (
        f"{star}🟣 <b>Twitch · {escape_html(channel)}</b>\n"
        f"<b>{title}</b>\n"
        f"Klippasi: {creator}{note} · {duration:.0f}s · {views} katselua\n"
        f"🕒 {format_time(clip.get('created_at'))}\n"
        f"{clip['url']}"
    )


# ---------------------------------------------------------------------------
# KICK  (epävirallinen endpoint)
# ---------------------------------------------------------------------------

def get_kick_clips(channel, max_pages=5):
    """Hakee Kick-klipit sivuttamalla.

    Kickin endpoint on dokumentoimaton eikä järjestys ole taattu. Jos se
    järjestää katselukertojen mukaan (kuten Twitch), tuore 0 katselun
    klippi on listan pohjalla — siksi haetaan useampi sivu, ei vain
    ensimmäistä.
    """
    url = f"https://kick.com/api/v2/channels/{channel}/clips"
    collected = []
    cursor = 0
    seen_cursors = set()

    for _ in range(max_pages):
        try:
            r = requests.get(
                url,
                headers=KICK_HEADERS,
                params={"cursor": cursor, "sort": "date"},
                timeout=20,
            )
        except requests.RequestException as e:
            print(f"Kick: {channel} -> virhe ({e}), ohitetaan")
            record("kick", False, f"{channel}: {str(e)[:80]}")
            return collected

        if r.status_code == 404:
            print(f"Kick: kanavaa '{channel}' ei löytynyt — tarkista nimi")
            record("kick", False, f"{channel}: HTTP 404 (nimi väärin?)")
            return collected
        if r.status_code != 200:
            print(f"Kick: {channel} -> HTTP {r.status_code}, ohitetaan")
            record("kick", False, f"{channel}: HTTP {r.status_code}")
            return collected

        try:
            data = r.json()
        except ValueError:
            print(f"Kick: {channel} -> vastaus ei ollut JSONia")
            record("kick", False, f"{channel}: vastaus ei JSONia")
            return collected

        if isinstance(data, list):
            page = data
            next_cursor = None
        elif isinstance(data, dict):
            page = data.get("clips") or data.get("data") or []
            next_cursor = (
                data.get("nextCursor")
                or data.get("next_cursor")
                or data.get("cursor")
            )
        else:
            break

        if not page:
            break
        collected.extend(page)

        if not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    record("kick", True)
    return collected


def kick_creator(clip):
    """Kickin klippi-endpoint on dokumentoimaton, joten tekijän kenttänimi
    voi vaihdella. Kokeillaan tunnetut vaihtoehdot; None jos ei löydy."""
    creator = clip.get("creator")
    if isinstance(creator, dict):
        for key in ("username", "slug", "name", "user_name"):
            if creator.get(key):
                return creator[key]
    if isinstance(creator, str) and creator:
        return creator
    for key in ("creator_username", "clipper", "user_username", "username"):
        val = clip.get(key)
        if isinstance(val, dict):
            val = val.get("username") or val.get("slug")
        if val:
            return val
    user = clip.get("user")
    if isinstance(user, dict):
        return user.get("username") or user.get("slug")
    return None


def format_kick_message(channel, clip, count=0, threshold=0):
    clip_id = clip.get("id")
    title = escape_html(clip.get("title") or "Uusi klippi")
    views = clip.get("views", clip.get("view_count", 0))
    duration = clip.get("duration", 0)
    link = clip.get("clip_url") or f"https://kick.com/{channel}/clips/{clip_id}"

    creator_name = kick_creator(clip)
    star = "⭐ " if is_frequent(count, threshold) else ""
    # Jos tekijää ei saada, jätetään koko maininta pois — parempi kuin "?"
    if creator_name:
        note = creator_note(count, threshold)
        meta = (
            f"Klippasi: {escape_html(creator_name)}{note} · "
            f"{duration}s · {views} katselua"
        )
    else:
        meta = f"{duration}s · {views} katselua"

    return (
        f"{star}🟢 <b>Kick · {escape_html(channel)}</b>\n"
        f"<b>{title}</b>\n"
        f"{meta}\n"
        f"🕒 {format_time(clip.get('created_at'))}\n"
        f"{link}"
    )


# ---------------------------------------------------------------------------

def bump_creator(creators, name):
    """Kasvattaa tekijän klippilaskuria ja palauttaa uuden lukeman.

    Laskurit ovat omassa tiedostossaan (creators.json), jotta state.json:n
    nollaus ei hävitä kertynyttä klippaajahistoriaa.
    """
    if not name:
        return 0
    key = str(name).strip().lower()
    if not key:
        return 0
    count = creators.get(key, 0) + 1
    creators[key] = count
    return count


def creator_note(count, threshold):
    """Teksti tekijän nimen perään, esim ' (23 klippiä)'. Tyhjä jos 1."""
    if count < 2:
        return ""
    return f" ({count} klippiä)"


def is_frequent(count, threshold):
    return threshold > 0 and count >= threshold


def check_health(state, config):
    """Ilmoittaa Telegramiin jos alusta on epäonnistunut monta kertaa peräkkäin.

    Ilmoitus lähetetään KERRAN katkoksen alussa ja kerran kun yhteys
    palautuu — ei joka ajolla. Hälytys laukeaa vain jos KAIKKI alustan
    hakuyritykset epäonnistuivat; yksi väärin kirjoitettu kanavanimi ei
    siis riitä.
    """
    threshold = config.get("health_alert_after_failures", 3) or 0
    if threshold <= 0:
        return

    health = state.setdefault("health", {})
    names = {"twitch": "Twitch", "kick": "Kick"}

    for platform, stats in RUN_STATS.items():
        if stats["attempts"] == 0:
            continue

        entry = health.setdefault(platform, {"fails": 0, "alerted": False})
        all_failed = stats["errors"] == stats["attempts"]

        if all_failed:
            entry["fails"] += 1
            print(
                f"{names[platform]}: epäonnistunut {entry['fails']} "
                f"kertaa peräkkäin"
            )
            if entry["fails"] >= threshold and not entry["alerted"]:
                detail = "; ".join(stats["details"][:3]) or "tuntematon virhe"
                send_telegram(
                    f"⚠️ <b>{names[platform]}-haku epäonnistunut "
                    f"{entry['fails']} kertaa peräkkäin</b>\n"
                    f"{escape_html(detail)}"
                )
                entry["alerted"] = True
        else:
            if entry["alerted"]:
                send_telegram(f"✅ <b>{names[platform]}-haku toimii taas</b>")
            entry["fails"] = 0
            entry["alerted"] = False


def prune(seen_list):
    return seen_list[-MAX_REMEMBERED_PER_CHANNEL:]


def main():
    for stats in RUN_STATS.values():
        stats.update({"attempts": 0, "errors": 0, "details": []})
    TOKEN_ERROR.clear()

    config = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {"twitch": {}, "kick": {}})
    state.setdefault("twitch", {})
    state.setdefault("kick", {})

    # Vanha sijainti: siirretään laskurit omaan tiedostoonsa jos niitä on
    creators = load_json(CREATORS_PATH, {})
    legacy = state.pop("creators", None)
    if legacy:
        for name, n in legacy.items():
            creators[name] = max(creators.get(name, 0), n)
        print(f"Siirretty {len(legacy)} klippaajaa creators.json-tiedostoon.")

    lookback = config.get("poll_lookback_minutes", 15)
    since_iso = (
        datetime.now(timezone.utc) - timedelta(minutes=lookback)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Valinnainen: ohita klipit jotka ovat tätä vanhempia (tuntia).
    # 0 tai puuttuva = ei rajaa.
    # Montako klippiä tekijältä ennen ⭐-merkkiä. 0 = ei merkkiä.
    star_threshold = config.get("frequent_clipper_min_clips", 10) or 0

    max_age_h = config.get("max_clip_age_hours", 0) or 0
    max_age_min = max_age_h * 60 if max_age_h > 0 else None

    def too_old(clip):
        if max_age_min is None:
            return False
        age = age_minutes(clip.get("created_at"))
        return age is not None and age > max_age_min

    first_run = not state["twitch"] and not state["kick"]
    if first_run:
        print("Ensimmäinen ajo: merkitään nykyiset klipit nähdyiksi ilman ilmoituksia.")

    found = 0

    # --- Twitch ---
    twitch_channels = [c.lower().strip() for c in config.get("twitch_channels", [])]
    if twitch_channels:
        token = get_twitch_token()
        if not token:
            detail = TOKEN_ERROR[0] if TOKEN_ERROR else "tunnukset puuttuvat"
            record("twitch", False, f"token: {detail}")
        else:
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
                    count = bump_creator(creators, clip.get("creator_name"))
                    if not first_run and not too_old(clip):
                        send_telegram(
                            format_twitch_message(
                                channel, clip, count, star_threshold
                            )
                        )
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
            count = bump_creator(creators, kick_creator(clip))
            if not first_run and not too_old(clip):
                send_telegram(
                    format_kick_message(channel, clip, count, star_threshold)
                )
                found += 1
            seen.append(str(clip.get("id")))
        state["kick"][channel] = prune(seen)

    check_health(state, config)

    save_json(STATE_PATH, state)
    save_json(CREATORS_PATH, creators)

    if first_run:
        print("Perustila tallennettu. Seuraavasta ajosta alkaen tulee ilmoituksia.")
    elif found:
        print(f"{found} uutta klippiä, ilmoitukset lähetetty.")
    else:
        print("Ei uusia klippejä.")


if __name__ == "__main__":
    main()
