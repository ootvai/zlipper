"""Lataa Kick-klipin, rajaa sen pystyvideoksi ja lähettää Telegramiin.

Ajetaan GitHub Actionsissa repository_dispatch-tapahtumasta, jonka
Cloudflare Worker laukaisee kun Telegramissa vastataan klippi-ilmoitukseen
numerolla 1 tai 2.

Lähteenä on klipin oma toistolahde: kick.com/api/v2/clips/{id} kertoo
HLS-soittolistan, jota selaimen soitin käyttää. Se on julkinen, ei vaadi
tunnistautumista, ja siinä on täysi laatu ilman Kickin mainoskorttia.
Sivuston Download-nappi sen sijaan liittää klipin perään 1,4 sekunnin
KICK-kortin, joten sitä ei käytetä.

Ympäristömuuttujat:
  CLIP_URL             https://kick.com/<kanava>/clips/<clip_id>
  MODEL                1 = zoomattu, 2 = koko kuva, 3 = alkuperäinen
  TELEGRAM_BOT_TOKEN   (GitHub secret)
  TELEGRAM_CHAT_ID     (GitHub secret)
  REQUEST_MESSAGE_ID   valinnainen: viesti johon vastataan
  MARKUP_MESSAGE_ID    valinnainen: viesti, jonka napit päivitetään
"""

import json
import os
import re
import subprocess
import sys
import traceback

import requests

import telegram
from telegram import escape_html

CONFIG_PATH = "config.json"
WORK_DIR = "work"

# Klipin tiedot, mukaan lukien soittimen HLS-lähde. Julkinen endpoint:
# tunnistautumista ei tarvita.
KICK_CLIP_API = "https://kick.com/api/v2/clips/{clip_id}"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Bot API:n yläraja lähetettävälle tiedostolle.
TELEGRAM_MAX_BYTES = 50 * 1024 * 1024

# Budjetti johon enkoodaus tähtää. Rajaa pienempi, koska mp4-kontti vie
# oman osansa eikä x264 osu kattoon tarkalleen.
TELEGRAM_BUDGET_MB = 45
AUDIO_KBPS = 160
# Alaraja, ettei erittäin pitkä klippi mene mössöksi. Jos tämä ei riitä
# mahtumaan, fit_under_limit kiristää crf:ää erikseen.
MIN_VIDEO_BPS = 900_000

MODEL_NAMES = {"1": "zoomattu", "2": "koko kuva", "3": "alkuperäinen"}

DEFAULT_CROP = {
    "width": 1080,
    "height": 1920,
    "zoom_height_pct": 67,
    "blur_sigma": 40,
}


def update_request_buttons(markup):
    """Vaihtaa alkuperäisen klippi-ilmoituksen napit.

    Worker kertoo erikseen minkä viestin napit saa vaihtaa — se ei ole sama
    kuin REQUEST_MESSAGE_ID, koska numerovastauksessa vastataan viestiin
    jossa ei ole nappeja lainkaan. Tyhjä arvo tarkoittaa ettei nappeja ole.
    Epäonnistuminen ei ole vika — viesti voi olla yli 48 h vanha — joten
    paluuarvoa ei tarkisteta.
    """
    message_id = os.environ.get("MARKUP_MESSAGE_ID", "").strip()
    if not message_id:
        return
    telegram.edit_reply_markup(message_id, markup)


class ProcessError(Exception):
    """Vika, joka kannattaa kertoa Telegramiin selkokielisenä."""


def log(msg):
    print(msg, flush=True)


def load_crop_settings():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, ValueError):
        config = {}
    crop = dict(DEFAULT_CROP)
    crop.update(config.get("crop") or {})
    return crop


def parse_clip_url(url):
    """Poimii kanavan ja klippi-id:n Kick-klippilinkistä."""
    m = re.search(r"kick\.com/([^/\s]+)/clips/([^/?\s]+)", url or "")
    if not m:
        raise ProcessError(f"En tunnistanut Kick-klippilinkkiä: {url!r}")
    return m.group(1), m.group(2)


# ---------------------------------------------------------------------------
# LATAUS
# ---------------------------------------------------------------------------

def kick_headers():
    return {"Accept": "application/json", "User-Agent": BROWSER_UA}


def find_url(payload, depth=0):
    """Etsii videon osoitteen vastauksesta."""
    if depth > 4:
        return None
    if isinstance(payload, str):
        return payload if payload.startswith("http") else None
    if isinstance(payload, dict):
        for key in ("video_url", "clip_url", "url", "src"):
            value = payload.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
        for key in ("clip", "data", "result"):
            if key in payload:
                found = find_url(payload[key], depth + 1)
                if found:
                    return found
    return None


def resolve_source(clip_id):
    """Hakee klipin toistolahteen Kickin julkisesta APIsta.

    Palauttaa HLS-soittolistan (playlist.m3u8) — saman jota selaimen
    soitin kayttaa.

    Aiemmin tassa kaytettiin osoitetta
    POST web.kick.com/api/v1/clips/{id}/download, mutta se osoittautui
    huonommaksi joka mittarilla: se on asynkroninen jobi jota pitaa
    pollata, sen nopeusrajoitus laukesi kahdesta pyynnosta puolessa
    minuutissa, se vaatii istuntotokenin joka vanhenee, sen bitrate oli
    matalampi (4.2 vs 6.1 Mbps) ja se liittaa klipin perään 1,4 sekunnin
    KICK-mainoskortin. Toistolahteessa ei ole naista mitaan.
    """
    url = KICK_CLIP_API.format(clip_id=clip_id)
    try:
        r = requests.get(url, headers=kick_headers(), timeout=30)
    except requests.RequestException as e:
        raise ProcessError(f"Kickin klippihaku ei mennyt lapi: {e}")

    if r.status_code == 404:
        raise ProcessError(f"Kick ei tunne klippia {clip_id} (HTTP 404).")
    if r.status_code != 200:
        raise ProcessError(
            f"Kickin klippihaku palautti HTTP {r.status_code}: {r.text[:200]}"
        )

    try:
        payload = r.json()
    except ValueError:
        raise ProcessError("Kickin vastaus ei ollut JSONia: " + r.text[:200])

    found = find_url(payload)
    if not found:
        log(f"Tuntematon vastausmuoto: {json.dumps(payload)[:800]}")
        raise ProcessError(
            "Klipin tiedoista ei loytynyt toisto-osoitetta — APIn muoto on "
            "ilmeisesti muuttunut. Katso Actions-loki."
        )
    return found


def fetch_source(source_url, dest):
    """Hakee lahteen levylle sellaisenaan.

    -c copy: virrat siirretaan pakkaamatta uudelleen, joten laatu sailyy
    tarkalleen ja haku kestaa sekunteja. Rajaus tekee ainoan enkoodauksen.
    """
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-user_agent", BROWSER_UA,
        "-i", source_url,
        "-c", "copy",
        dest,
    ]
    log(" ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ProcessError("Lahteen haku epaonnistui: " + (result.stderr or "")[-400:])

    size = os.path.getsize(dest) if os.path.exists(dest) else 0
    if size < 10000:
        raise ProcessError(f"Haettu tiedosto on liian pieni ({size} tavua).")
    log(f"Haettu {size / 1024 / 1024:.1f} MB -> {dest}")
    return dest


# ---------------------------------------------------------------------------
# RAJAUS
# ---------------------------------------------------------------------------

def build_filter(model, crop):
    """Rakentaa ffmpeg-suodinketjun.

    Molemmissa malleissa tausta on saman klipin oma kuva: skaalattuna
    täyttämään pystyruutu, keskeltä rajattuna ja sumennettuna. Kirkkautta
    ei säädetä mihinkään suuntaan, joten reunat seuraavat klipin omaa
    valaistusta.

    Malli 1 (zoomattu): pääkuva skaalataan peittämään leveys x osuus
    korkeudesta ja rajataan keskeltä, eli kuvaan mennään sisään.
    Malli 2 (koko kuva): pääkuva mahtuu ruutuun kokonaisena, mitään ei
    leikata pois.
    """
    w = int(crop["width"])
    h = int(crop["height"])
    sigma = float(crop["blur_sigma"])

    # Tausta sumennetaan joka tapauksessa, joten skaalaimella ei ole
    # sille merkitystä. Pääkuvassa on: lanczos säilyttää yksityiskohdan
    # ylöspäin skaalatessa selvästi oletusta paremmin.
    background = (
        f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},gblur=sigma={sigma}[bgb]"
    )

    if model == "1":
        pct = float(crop["zoom_height_pct"]) / 100.0
        fg_h = int(h * pct)
        fg_h -= fg_h % 2  # x264 vaatii parillisen korkeuden
        foreground = (
            f"[fg]scale={w}:{fg_h}:force_original_aspect_ratio=increase"
            f":flags=lanczos,crop={w}:{fg_h}[fgs]"
        )
    else:
        foreground = (
            f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease"
            f":flags=lanczos[fgs]"
        )

    return (
        f"[0:v]split=2[bg][fg];{background};{foreground};"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]"
    )


def probe_duration(path):
    """Klipin kesto sekunteina, tai None jos ffprobe ei kerro."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True,
        )
        return float(result.stdout.strip())
    except (ValueError, OSError):
        return None


def bitrate_cap(duration):
    """Suurin videobitrate jolla klippi mahtuu budjettiin.

    Telegramin raja on 50 MB. Budjetti on tarkoituksella pienempi, koska
    mp4-kontti ja äänivirta vievät oman osansa eikä x264 osu kattoon
    tarkalleen.

    Paluuarvo on katto, ei tavoite: lyhyet klipit enkoodautuvat crf:n
    ehdoilla selvästi tämän alle ja pysyvät terävinä. Vain pitkät klipit
    törmäävät kattoon ja puristuvat mahtumaan.
    """
    if not duration or duration <= 0:
        return None
    total_bits = TELEGRAM_BUDGET_MB * 1024 * 1024 * 8
    video_bits = total_bits - (AUDIO_KBPS * 1000 * duration)
    bps = int(video_bits / duration)
    return max(bps, MIN_VIDEO_BPS)


def run_ffmpeg(src, dest, model, crop, crf=20, cap=None):
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", src,
        "-filter_complex", build_filter(model, crop),
        "-map", "[v]",
        "-map", "0:a?",
        "-c:v", "libx264",
        # slow pakkaa tehokkaammin kuin veryfast; 23 s klippi enkoodautui
        # 7 sekunnissa, joten nopeus ei ole pullonkaula.
        "-preset", "slow",
        "-crf", str(crf),
    ]
    if cap:
        cmd += ["-maxrate", str(cap), "-bufsize", str(cap * 2)]
    cmd += [
        "-c:a", "aac",
        "-b:a", f"{AUDIO_KBPS}k",
        "-movflags", "+faststart",
        dest,
    ]
    log(" ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ProcessError("ffmpeg epäonnistui: " + (result.stderr or "")[-400:])
    return dest


def fit_under_limit(src, dest, model, crop):
    """Enkoodaa niin että tulos mahtuu Telegramin rajaan."""
    duration = probe_duration(src)
    cap = bitrate_cap(duration)
    if cap:
        log(f"Kesto {duration:.1f} s -> bitraten katto {cap / 1e6:.1f} Mbps")

    run_ffmpeg(src, dest, model, crop, cap=cap)
    size = os.path.getsize(dest)
    log(f"Rajattu tiedosto {size / 1024 / 1024:.1f} MB")
    if size <= TELEGRAM_MAX_BYTES:
        return dest, size

    # Kattolaskenta pitäisi estää tämän, mutta jos x264 ylittää arvion,
    # kiristetään portaittain eikä romahdeta kerralla.
    for crf in (23, 26):
        log(f"Yli rajan — uusi yritys crf {crf}.")
        run_ffmpeg(src, dest, model, crop, crf=crf, cap=cap)
        size = os.path.getsize(dest)
        log(f"crf {crf}: {size / 1024 / 1024:.1f} MB")
        if size <= TELEGRAM_MAX_BYTES:
            break
    return dest, size


# ---------------------------------------------------------------------------
# MALLI 3: ALKUPERÄINEN SELLAISENAAN
# ---------------------------------------------------------------------------

def send_original(channel, clip_id, clip_url, reply_to):
    """Lähettää klipin ilman rajausta ja ilman uudelleenpakkausta.

    fetch_source muxaa HLS-lähteen MP4:ksi -c copy -kytkimellä, joten
    kuva ja ääni ovat tarkalleen samat kuin Kickin omassa soittimessa.
    Vesileimaa ei ole: se ja klipin perään liitetty KICK-mainoskortti
    tulivat vanhasta latausendpointista, ei toistolähteestä.

    Lähetys tehdään dokumenttina eikä videona, jotta Telegram ei käsittele
    tiedostoa mitenkään. Se ei siis toistu chatissa suoraan, vaan pitää
    ladata — mutta ladattu tiedosto on tavu tavulta se mikä lähetettiin.
    """
    # Tiedostonimi näkyy Telegramissa ja latauskansiossa, joten se
    # rakennetaan kanavasta ja klipistä eikä työnimestä.
    dest = os.path.join(WORK_DIR, f"{channel}-{clip_id}.mp4")
    fetch_source(resolve_source(clip_id), dest)
    size = os.path.getsize(dest)
    megat = size / 1024 / 1024

    caption = (
        f"⬇️ <b>Alkuperäinen · ei rajausta, ei pakkausta</b>\n"
        f"Kick · {escape_html(channel)}\n"
        f"{clip_url}"
    )

    if size > TELEGRAM_MAX_BYTES:
        # Ei pakata pienemmäksi: se olisi juuri se mitä tällä mallilla
        # yritetään välttää.
        telegram.send_message(
            caption
            + f"\n\n⚠️ Tiedosto on {megat:.0f} MB, eikä Telegram ota "
            "vastaan yli 50 MB:tä. Rajattu versio mahtuu aina, tai hae tämä "
            "Actions-ajon artifaktista (tallessa 7 vrk)."
        )
        raise ProcessError(
            f"Alkuperäinen on {megat:.0f} MB eikä mahdu Telegramin rajaan."
        )

    status = telegram.send_document(
        dest, caption=caption, reply_to_message_id=reply_to or None
    )
    if status != telegram.SENT:
        raise ProcessError(
            f"Tiedoston lähetys Telegramiin epäonnistui ({status})."
        )

    update_request_buttons(
        telegram.status_keyboard("✅ Alkuperäinen lähetetty")
    )
    log(f"Valmis — {megat:.1f} MB dokumenttina.")


# ---------------------------------------------------------------------------

def main():
    clip_url = os.environ.get("CLIP_URL", "").strip()
    model = os.environ.get("MODEL", "").strip()
    reply_to = os.environ.get("REQUEST_MESSAGE_ID", "").strip()

    if model not in MODEL_NAMES:
        raise ProcessError(f"Tuntematon malli {model!r} (odotin 1, 2 tai 3).")

    channel, clip_id = parse_clip_url(clip_url)
    os.makedirs(WORK_DIR, exist_ok=True)

    log(f"Klippi {clip_id} kanavalta {channel}, malli {model}")

    if model == "3":
        send_original(channel, clip_id, clip_url, reply_to)
        return

    crop = load_crop_settings()
    raw = os.path.join(WORK_DIR, f"{clip_id}.src.mp4")
    out = os.path.join(WORK_DIR, f"{clip_id}-malli{model}.mp4")

    fetch_source(resolve_source(clip_id), raw)
    out, size = fit_under_limit(raw, out, model, crop)

    caption = (
        f"✂️ <b>Rajattu · malli {model} ({MODEL_NAMES[model]})</b>\n"
        f"Kick · {escape_html(channel)}\n"
        f"{clip_url}"
    )

    if size > TELEGRAM_MAX_BYTES:
        telegram.send_message(
            caption
            + "\n\n⚠️ Tiedosto on yhä yli 50 MB, eikä Telegram ota sitä "
            "vastaan. Katso Actions-lokista talteen jäänyt versio."
        )
        raise ProcessError("Rajattu video ei mahtunut Telegramin rajaan.")

    status = telegram.send_video(
        out,
        caption=caption,
        reply_to_message_id=reply_to or None,
    )
    if status != telegram.SENT:
        raise ProcessError(f"Videon lähetys Telegramiin epäonnistui ({status}).")

    update_request_buttons(
        telegram.status_keyboard(f"✅ Rajattu: {MODEL_NAMES[model]}")
    )
    log("Valmis.")


if __name__ == "__main__":
    try:
        main()
    except ProcessError as e:
        log(f"VIRHE: {e}")
        # Kerrotaan vika Telegramiin, ettei putki jää hiljaa jumiin.
        telegram.send_message(
            f"❌ <b>Rajaus epäonnistui</b>\n{escape_html(str(e))}"
        )
        update_request_buttons(telegram.crop_keyboard())
        sys.exit(1)
    except Exception as e:
        # Odottamaton poikkeus ohitti aiemmin ilmoituksen kokonaan: puuttuva
        # ffmpeg nosti FileNotFoundErrorin, eikä Telegramiin tullut mitään.
        # Nyt kaikki viat kerrotaan, myös ne joita ei osattu ennakoida.
        log(f"ODOTTAMATON VIRHE: {type(e).__name__}: {e}")
        traceback.print_exc()
        telegram.send_message(
            f"❌ <b>Rajaus kaatui odottamattomaan virheeseen</b>\n"
            f"{escape_html(type(e).__name__)}: {escape_html(str(e))}\n"
            f"Tarkemmat tiedot Actions-lokissa."
        )
        update_request_buttons(telegram.crop_keyboard())
        sys.exit(1)
