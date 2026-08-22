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

import telegram
from telegram import escape_html

CONFIG_PATH = "config.json"
STATE_PATH = "state.json"
CREATORS_PATH = "creators.json"

TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "")
# Telegram-tunnukset luetaan telegram-moduulissa.

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
    # Ei %-d/%-m: ne ovat glibc-laajennoksia eivätkä toimi Windowsilla,
    # mikä esti skriptin ajamisen paikallisesti testiksi.
    stamp = f"{local.day}.{local.month}. klo {local:%H:%M}"

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
    """Huoltoilmoitus (terveys, heartbeat, hiljainen kanava).

    Näille riittää paras yritys: jos yksi heartbeat jää väliin, seuraava
    tulee joka tapauksessa. Klippi-ilmoitukset sen sijaan käyttävät
    telegram.send_messageä suoraan, koska niiden lopputulos ratkaisee
    merkitäänkö klippi nähdyksi.
    """
    return telegram.send_message(text)


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


def format_twitch_message(channel, clip, count=0, tiers=(0, 0)):
    return build_message(
        title=clip.get("title"),
        source=f"🟣 Twitch · {escape_html(channel)}",
        duration=clip.get("duration"),
        views=clip.get("view_count", 0),
        creator=clip.get("creator_name"),
        count=count,
        tiers=tiers,
        created_at=clip.get("created_at"),
        link=clip["url"],
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


def format_kick_message(channel, clip, count=0, tiers=(0, 0)):
    clip_id = clip.get("id")

    # HUOM: käytetään AINA rakennettua klippisivun URL:ia, ei clip_url-kenttää.
    # clip_url näytti osoittavan suoraan CDN-videotiedostoon, jolloin
    # Telegram lataa/avaa videon heti napautettaessa sen sijaan että
    # näyttäisi normaalin linkkiesikatselun. kick.com/<kanava>/clips/<id>
    # on aina tavallinen klippisivu, josta voi itse ladata halutessaan.
    #
    # Linkki on myös se, mistä Worker lukee klipin kun nappia painetaan,
    # joten sen on pysyttävä viestissä omalla rivillään.
    link = f"https://kick.com/{channel}/clips/{clip_id}"

    return build_message(
        title=clip.get("title"),
        source=f"🟢 Kick · {escape_html(channel)}",
        duration=clip.get("duration"),
        views=clip.get("views", clip.get("view_count", 0)),
        creator=kick_creator(clip),
        count=count,
        tiers=tiers,
        created_at=clip.get("created_at"),
        link=link,
    )


# ---------------------------------------------------------------------------

def creator_key(name):
    if not name:
        return ""
    return str(name).strip().lower()


def peek_creator(creators, name):
    """Mikä tekijän lukema OLISI tämän klipin jälkeen — ei muuta tilaa.

    Viesti muotoillaan ennen lähetystä, mutta laskuri saa kasvaa vasta kun
    klippi oikeasti merkitään nähdyksi. Muuten epäonnistunut lähetys ja sen
    uusinta laskisivat saman klipin kahteen kertaan.
    """
    key = creator_key(name)
    if not key:
        return 0
    return creators.get(key, 0) + 1


def bump_creator(creators, name):
    """Kasvattaa tekijän klippilaskuria ja palauttaa uuden lukeman.

    Laskurit ovat omassa tiedostossaan (creators.json), jotta state.json:n
    nollaus ei hävitä kertynyttä klippaajahistoriaa.
    """
    key = creator_key(name)
    if not key:
        return 0
    count = creators.get(key, 0) + 1
    creators[key] = count
    return count


def clipper_tiers(config):
    """(tuttu, luotettava) — montako klippiä kumpikin porras vaatii."""
    return (
        config.get("clipper_known_min_clips", 5) or 0,
        config.get("clipper_trusted_min_clips", 25) or 0,
    )


def clipper_line(name, count, tiers):
    """Rivi klippaajasta, tai tyhjä jos tekijä ei vielä kerro mitään.

    Aiemmin jokainen ilmoitus näytti "Klippasi: joku (1 klippiä)", mikä oli
    pelkkää täytettä: kertaluontoinen klippaaja on tuntematon nimi. Nyt
    maininta tulee vasta kun klippejä on kertynyt sen verran, että nimi
    todella ennustaa jotain klipin laadusta.
    """
    if not name:
        return ""
    known, trusted = tiers
    if trusted > 0 and count >= trusted:
        label = "⭐ Luotettava klippaaja"
    elif known > 0 and count >= known:
        label = "✂️ Tuttu klippaaja"
    else:
        return ""
    return f"{label}: {escape_html(name)} · {count} klippiä"


def format_duration(raw):
    """'42 s' tai '1:23'. Tyhjä jos kestoa ei tiedetä."""
    try:
        secs = int(round(float(raw)))
    except (TypeError, ValueError):
        return ""
    if secs <= 0:
        return ""
    if secs < 60:
        return f"{secs} s"
    return f"{secs // 60}:{secs % 60:02d}"


def build_message(title, source, duration, views, creator, count, tiers,
                  created_at, link):
    """Ilmoituksen runko, sama molemmille alustoille.

    Otsikko on ainoa lihavoitu asia ja ensimmäisenä: Telegram ei tunne
    fonttikokoja, joten järjestys ja ainoa <b> ovat kaikki mitä on
    käytettävissä. Aiemmin kanavanimi oli yhtä vahva kuin otsikko, jolloin
    silmä osui ensin kanavaan eikä siihen mitä klipissä tapahtuu.
    """
    meta = [source]
    dur = format_duration(duration)
    if dur:
        meta.append(dur)
    meta.append(f"{views} katselua")

    lines = [
        f"<b>{escape_html(title or 'Uusi klippi')}</b>",
        " · ".join(meta),
    ]
    clipper = clipper_line(creator, count, tiers)
    if clipper:
        lines.append(clipper)
    lines.append(f"🕒 {format_time(created_at)}")
    lines.append(link)
    return "\n".join(lines)


def heartbeat(state, config, found_now):
    """Lähettää säännöllisen elonmerkin Telegramiin.

    Tarkoitus: nähdä että putki on pystyssä myös hiljaisina jaksoina.
    Viesti EI tule joka ajosta (niitä on satoja vuorokaudessa) vaan
    valitulla aikavälillä. Jos viesti jää tulematta, ajastus on
    hajonnut — se on juuri se vika jota ei muuten huomaisi.
    """
    hours = config.get("heartbeat_hours", 0) or 0
    hb = state.setdefault("heartbeat", {"last": None, "clips_since": 0})
    hb["clips_since"] = hb.get("clips_since", 0) + found_now

    if hours <= 0:
        return

    now = datetime.now(timezone.utc)
    last = parse_ts(hb.get("last"))

    if last is None:
        # Ensimmäinen kerta: aloitetaan kello, ei lähetetä heti
        hb["last"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        return

    elapsed_h = (now - last).total_seconds() / 3600
    if elapsed_h < hours:
        return

    n = hb.get("clips_since", 0)
    if n == 0:
        summary = "ei uusia klippejä"
    elif n == 1:
        summary = "1 uusi klippi"
    else:
        summary = f"{n} uutta klippiä"

    tila = []
    for platform, name in (("twitch", "Twitch"), ("kick", "Kick")):
        stats = RUN_STATS[platform]
        if stats["attempts"] == 0:
            continue
        ok = stats["errors"] < stats["attempts"]
        tila.append(f"{name} {'✅' if ok else '❌'}")

    send_telegram(
        f"💚 <b>Klippivahti toimii</b>\n"
        f"Viimeisen {int(round(elapsed_h))} h aikana: {summary}\n"
        f"{' · '.join(tila)}"
    )

    hb["last"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    hb["clips_since"] = 0


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


def note_channel_activity(state, platform, channel, had_new):
    """Merkitsee milloin kanavalta viimeksi tuli uusi klippi.

    Uusi kanava alustetaan nykyhetkeen, jotta se ei hälytä heti
    lisäämisen jälkeen.
    """
    silent = state.setdefault("silent", {})
    key = f"{platform}:{channel}"
    entry = silent.setdefault(key, {"last_clip": None, "alerted": False})
    if entry.get("last_clip") is None or had_new:
        entry["last_clip"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        entry["alerted"] = False


def check_silent_channels(state, config):
    """Ilmoittaa kerran jos kanavalta ei ole tullut klippejä pitkään aikaan.

    Ilmoitus lähetetään vain kerran hiljaista jaksoa kohti. Lippu
    nollautuu automaattisesti kun kanavalta tulee taas klippi.
    """
    days = config.get("silent_channel_days", 0) or 0
    if days <= 0:
        return

    silent = state.setdefault("silent", {})

    # Siivotaan configista poistetut kanavat pois tilasta
    active = set()
    for platform, cfg_key in (
        ("twitch", "twitch_channels"),
        ("kick", "kick_channels"),
    ):
        for c in config.get(cfg_key, []):
            active.add(f"{platform}:{c.lower().strip()}")
    for key in [k for k in silent if k not in active]:
        silent.pop(key)

    now = datetime.now(timezone.utc)
    for key, entry in silent.items():
        if entry.get("alerted"):
            continue
        last = parse_ts(entry.get("last_clip"))
        if last is None:
            continue
        if (now - last).total_seconds() / 86400 >= days:
            platform, channel = key.split(":", 1)
            name = "Twitch" if platform == "twitch" else "Kick"
            send_telegram(
                f"🔇 <b>Hiljainen kanava</b>\n"
                f"{name} · {escape_html(channel)}\n"
                f"Ei uusia klippejä {days} vuorokauteen."
            )
            entry["alerted"] = True


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

    # Katselukertaseuranta poistettiin. Siivotaan sen jaanteet pois,
    # etteivat ne jaa state.jsoniin kuolleeksi dataksi.
    if state.pop("tracked", None):
        print("Poistettu vanhat katselukertaseurannan merkinnat.")

    lookback = config.get("poll_lookback_minutes", 15)
    since_iso = (
        datetime.now(timezone.utc) - timedelta(minutes=lookback)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Klippaajan portaat: montako klippiä ennen "tuttu" ja "luotettava".
    tiers = clipper_tiers(config)

    # Valinnainen: ohita klipit jotka ovat tätä vanhempia (tuntia).
    # 0 tai puuttuva = ei rajaa.
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
                    note_channel_activity(state, "twitch", channel, False)
                    continue
                seen = state["twitch"].get(channel, [])
                seen_set = set(seen)
                clips = get_twitch_clips(bid, token, since_iso)
                new = [c for c in clips if c["id"] not in seen_set]
                new.sort(key=lambda c: c.get("created_at", ""))
                for clip in new:
                    name = clip.get("creator_name")
                    if first_run or too_old(clip):
                        bump_creator(creators, name)
                        seen.append(clip["id"])
                        continue
                    count = peek_creator(creators, name)
                    # Twitch-klipeille ei nappeja: rajausputki osaa
                    # toistaiseksi vain Kickin klipit.
                    status = telegram.send_message(
                        format_twitch_message(channel, clip, count, tiers)
                    )
                    if status == telegram.FAILED:
                        # Ei merkitä nähdyksi — klippi yritetään uudelleen
                        # seuraavalla ajolla. max_clip_age_hours katkaisee
                        # kierteen, jos vika ei korjaannu itsestään.
                        print(
                            f"Twitch: {clip['id']} — ilmoitus ei mennyt "
                            f"läpi, uusi yritys seuraavalla ajolla"
                        )
                        continue
                    bump_creator(creators, name)
                    seen.append(clip["id"])
                    if status == telegram.SENT:
                        found += 1
                state["twitch"][channel] = prune(seen)
                note_channel_activity(state, "twitch", channel, bool(new))

    # --- Kick ---
    for channel in [c.lower().strip() for c in config.get("kick_channels", [])]:
        seen = state["kick"].get(channel, [])
        seen_set = set(seen)
        clips = get_kick_clips(channel)
        new = [c for c in clips if str(c.get("id")) not in seen_set]
        new.sort(key=lambda c: c.get("created_at", ""))
        for clip in new:
            name = kick_creator(clip)
            clip_id = str(clip.get("id"))
            if first_run or too_old(clip):
                bump_creator(creators, name)
                seen.append(clip_id)
                continue
            count = peek_creator(creators, name)
            status = telegram.send_message(
                format_kick_message(channel, clip, count, tiers),
                reply_markup=telegram.crop_keyboard(),
            )
            if status == telegram.FAILED:
                print(
                    f"Kick: {clip_id} — ilmoitus ei mennyt läpi, uusi "
                    f"yritys seuraavalla ajolla"
                )
                continue
            bump_creator(creators, name)
            seen.append(clip_id)
            if status == telegram.SENT:
                found += 1
        state["kick"][channel] = prune(seen)
        note_channel_activity(state, "kick", channel, bool(new))

    check_health(state, config)
    check_silent_channels(state, config)
    heartbeat(state, config, found)

    save_json(STATE_PATH, state)
    save_json(CREATORS_PATH, creators)

    if first_run:
        print("Perustila tallennettu. Seuraavasta ajosta alkaen tulee ilmoituksia.")
    elif found:
        print(f"{found} uutta klippiä, ilmoitukset lähetetty.")
    else:
        print("Ei uusia klippejä.")
        # Ajastettu ajo on hiljainen kun mitään ei löydy. Telegramista käsin
        # pyydetty ei voi olla: muuten hiljaisuus voi tarkoittaa yhtä hyvin
        # "ei uusia klippejä" kuin "komento ei mennyt perille".
        if os.environ.get("REPORT_EMPTY") == "1":
            telegram.send_message("Ei uusia klippejä.")


if __name__ == "__main__":
    main()
