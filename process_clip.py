"""Lataa Kick-klipin, rajaa sen pystyvideoksi ja lähettää Telegramiin.

Ajetaan GitHub Actionsissa repository_dispatch-tapahtumasta, jonka
Cloudflare Worker laukaisee kun Telegramissa vastataan klippi-ilmoitukseen
numerolla 1 tai 2.

Lataus käyttää Kickin API v1 -endpointtia
(POST https://web.kick.com/api/v1/clips/{clip_id}/download), joka palauttaa
puhtaan MP4:n. Kickin sivun oma Download-nappi lisää nykyään vesileiman,
joten sitä ei käytetä. Endpoint on dokumentoimaton ja voi muuttua ilman
varoitusta — siksi vastauksesta etsitään osoite useasta eri kentästä ja
tuntematon vastaus lokitetaan sellaisenaan.

Ympäristömuuttujat:
  CLIP_URL             https://kick.com/<kanava>/clips/<clip_id>
  MODEL                1 = zoomattu, 2 = koko kuva
  KICK_AUTH_TOKEN      Kickin istuntotoken (GitHub secret)
  TELEGRAM_BOT_TOKEN   (GitHub secret)
  TELEGRAM_CHAT_ID     (GitHub secret)
  REQUEST_MESSAGE_ID   valinnainen: viesti johon vastataan
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

import requests

import telegram
from telegram import escape_html

CONFIG_PATH = "config.json"
WORK_DIR = "work"

KICK_DOWNLOAD_URL = "https://web.kick.com/api/v1/clips/{clip_id}/download"

# POST yllä olevaan osoitteeseen ei palauta MP4:ää vaan käynnistää työn:
# 201 {"data":{"job_id":"...","status":"pending"},"message":"success"}
# Valmis osoite haetaan erikseen. Kickin API on dokumentoimaton eikä
# tilaosoite näkynyt selaimesta kaapatussa pyynnössä, joten vaihtoehtoja
# kokeillaan järjestyksessä ja ensimmäinen vastaava jää käyttöön.
#
# Kaksi ensimmäistä eivät oleta uutta polkua lainkaan: sama osoite GETillä,
# ja saman POSTin toisto — moni tämäntyylinen API palauttaa valmiin
# osoitteen kun työ on ehtinyt valmistua.
KICK_JOB_STATUS_URLS = [
    ("GET", "https://web.kick.com/api/v1/clips/{clip_id}/download"),
    ("POST", "https://web.kick.com/api/v1/clips/{clip_id}/download"),
    ("GET", "https://web.kick.com/api/v1/clips/{clip_id}/download/{job_id}"),
    ("GET", "https://web.kick.com/api/v1/clips/{clip_id}/download/status"),
    ("GET", "https://web.kick.com/api/v1/download-jobs/{job_id}"),
    ("GET", "https://web.kick.com/api/v1/jobs/{job_id}"),
]

# Kickin nopeusrajoitus on tiukka — kaksi pyyntöä puolessa minuutissa
# riitti laukaisemaan RATE_LIMIT_EXCEEDED.
KICK_RATE_LIMIT_WAIT_S = 15
KICK_MAX_ATTEMPTS = 5
KICK_JOB_POLL_S = 6
KICK_JOB_TIMEOUT_S = 300

# Bot API:n yläraja lähetettävälle tiedostolle.
TELEGRAM_MAX_BYTES = 50 * 1024 * 1024

MODEL_NAMES = {"1": "zoomattu", "2": "koko kuva"}

DEFAULT_CROP = {
    "width": 1080,
    "height": 1920,
    "zoom_height_pct": 67,
    "blur_sigma": 40,
}


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

def kick_headers(token):
    """Otsakkeet selaimen oikean pyynnön mukaan.

    Bearer-token ja evästeen session_token ovat sama arvo; selain
    lähettää molemmat, joten tehdään samoin. Evästeessä arvo on
    URL-koodattuna.
    """
    return {
        "Authorization": f"Bearer {token}",
        "Cookie": f"session_token={urllib.parse.quote(token, safe='')}",
        "Accept": "application/json",
        "Accept-Language": "fi-FI,fi;q=0.9,en-US;q=0.8,en;q=0.7",
        "Content-Type": "application/json",
        "Origin": "https://kick.com",
        "Referer": "https://kick.com/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    }


def find_url(payload, depth=0):
    """Etsii MP4-osoitteen tuntemattoman muotoisesta vastauksesta."""
    if depth > 4:
        return None
    if isinstance(payload, str):
        return payload if payload.startswith("http") else None
    if isinstance(payload, dict):
        for key in (
            "url",
            "download_url",
            "downloadUrl",
            "signed_url",
            "signedUrl",
            "video_url",
            "src",
        ):
            value = payload.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
        for key in ("data", "clip", "result", "download"):
            if key in payload:
                found = find_url(payload[key], depth + 1)
                if found:
                    return found
    return None


def find_job_id(payload, depth=0):
    """Etsii latausjobin tunnisteen vastauksesta."""
    if depth > 4 or not isinstance(payload, dict):
        return None
    for key in ("job_id", "jobId", "id", "uuid"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("data", "job", "result", "download"):
        if key in payload:
            found = find_job_id(payload[key], depth + 1)
            if found:
                return found
    return None


def find_status(payload, depth=0):
    if depth > 4 or not isinstance(payload, dict):
        return None
    value = payload.get("status") or payload.get("state")
    if isinstance(value, str):
        return value.lower()
    for key in ("data", "job", "result", "download"):
        if key in payload:
            found = find_status(payload[key], depth + 1)
            if found:
                return found
    return None


def kick_request(method, url, token, **kwargs):
    """Kutsuu Kickia ja kunnioittaa nopeusrajoitusta.

    Kickin raja on tiukka: kaksi pyyntöä puolen minuutin sisällä riitti
    laukaisemaan RATE_LIMIT_EXCEEDED. 429 ei siis ole poikkeustila vaan
    odotettavissa, joten sitä odotetaan pois eikä kaaduta.
    """
    backoff = KICK_RATE_LIMIT_WAIT_S
    last = None

    for attempt in range(1, KICK_MAX_ATTEMPTS + 1):
        try:
            r = requests.request(
                method, url, headers=kick_headers(token), timeout=60, **kwargs
            )
        except requests.RequestException as e:
            raise ProcessError(f"Kick-kutsu ei mennyt läpi: {e}")

        if r.status_code == 429:
            last = r
            if attempt == KICK_MAX_ATTEMPTS:
                break
            wait = backoff
            retry_after = r.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = max(wait, float(retry_after))
                except ValueError:
                    pass
            log(f"Kick 429 (yritys {attempt}) — odotetaan {wait:.0f} s.")
            time.sleep(wait)
            backoff = min(backoff * 2, 60)
            continue

        return r

    raise ProcessError(
        "Kickin nopeusrajoitus ei hellittänyt "
        f"{KICK_MAX_ATTEMPTS} yrityksellä. Odota hetki ja vastaa uudelleen."
        + (f" Viimeisin vastaus: {last.text[:150]}" if last is not None else "")
    )


def check_common_errors_code(code):
    if code in (401, 403):
        raise ProcessError(
            f"Kick hylkäsi tunnistautumisen (HTTP {code}). "
            "KICK_AUTH_TOKEN on todennäköisesti vanhentunut — päivitä "
            "GitHub-secret."
        )


def check_common_errors(r, clip_id):
    check_common_errors_code(r.status_code)
    if r.status_code == 404:
        raise ProcessError(f"Kick ei tunne kohdetta {clip_id} (HTTP 404).")


def request_download_url(clip_id, token):
    """Käynnistää latausjobin ja palauttaa valmiin MP4-osoitteen.

    Endpoint on asynkroninen: POST vastaa 201:llä ja antaa job_id:n sekä
    tilan "pending". Varsinainen osoite haetaan erikseen, kun työ on
    valmistunut.
    """
    url = KICK_DOWNLOAD_URL.format(clip_id=clip_id)
    # Runko on tyhjä JSON-objekti: selaimen pyynnössä Content-Length on 2.
    r = kick_request("POST", url, token, json={})
    check_common_errors(r, clip_id)

    if r.status_code not in (200, 201, 202):
        raise ProcessError(
            f"Kickin latauskutsu palautti HTTP {r.status_code}: {r.text[:200]}"
        )

    try:
        payload = r.json()
    except ValueError:
        raise ProcessError("Kickin vastaus ei ollut JSONia: " + r.text[:200])

    # Jos osoite tulee heti, ei tarvitse kysellä perään.
    found = find_url(payload)
    if found:
        return found

    job_id = find_job_id(payload)
    if not job_id:
        log(f"Tuntematon vastausmuoto: {json.dumps(payload)[:800]}")
        raise ProcessError(
            "Kickin vastauksessa ei ollut latauslinkkiä eikä job_id:tä — "
            "endpointin muoto on ilmeisesti muuttunut. Katso Actions-loki."
        )

    log(f"Latausjobi {job_id} kaynnistetty, odotetaan valmistumista.")
    return await_job(clip_id, job_id, token)


def probe(method, url, token):
    """Kokeilee yhtä tilaosoitetta. Palauttaa (statuskoodi, payload|None)."""
    kwargs = {"json": {}} if method == "POST" else {}
    r = kick_request(method, url, token, **kwargs)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, None


def await_job(clip_id, job_id, token):
    """Kyselee jobin tilaa kunnes MP4-osoite on saatavilla."""
    deadline = time.monotonic() + KICK_JOB_TIMEOUT_S
    candidates = [(m, u.format(clip_id=clip_id, job_id=job_id))
                  for m, u in KICK_JOB_STATUS_URLS]
    working = None
    last_payload = None
    # Kootaan jokaisen kokeilun lopputulos: jos mikaan ei osu, tama kertoo
    # seuraavalle korjaajalle enemman kuin pelkka "ei loytynyt". Esim. 405
    # tarkoittaisi etta polku on oikea mutta metodi vaara.
    attempts = []

    while time.monotonic() < deadline:
        time.sleep(KICK_JOB_POLL_S)

        for method, url in ([working] if working else candidates):
            code, payload = probe(method, url, token)
            path = url.replace("https://web.kick.com/api/v1", "")

            if not working:
                note = f"{method} {path} -> {code}"
                log("  kokeilu: " + note)
                attempts.append(note)

            if code in (401, 403):
                check_common_errors_code(code)
            if code >= 300 or payload is None:
                continue

            if not working:
                working = (method, url)
                log(f"Jobin tila luetaan: {method} {path}")
            last_payload = payload

            found = find_url(payload)
            if found:
                return found

            status = find_status(payload)
            log(f"Jobi {job_id}: {status or 'tila tuntematon'}")
            if status in ("failed", "error", "cancelled"):
                raise ProcessError(f"Kickin latausjobi epäonnistui: {status}")
            break

        if not working:
            log("Yksikaan tilaosoite ei vastannut:")
            for note in attempts:
                log("  " + note)
            raise ProcessError(
                "En löytänyt osoitetta josta latausjobin tilan voi lukea.\n"
                + "\n".join(attempts)
                + "\n\nKatso DevToolsista mihin Kick kyselee POSTin jälkeen."
            )

    if last_payload is not None:
        log(f"Viimeisin tila: {json.dumps(last_payload)[:800]}")
    raise ProcessError(
        f"Kickin latausjobi ei valmistunut {KICK_JOB_TIMEOUT_S} sekunnissa."
    )


def download(url, dest):
    try:
        with requests.get(url, stream=True, timeout=180) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if chunk:
                        f.write(chunk)
    except requests.RequestException as e:
        raise ProcessError(f"Klipin lataus epäonnistui: {e}")

    size = os.path.getsize(dest)
    if size < 10000:
        raise ProcessError(f"Ladattu tiedosto on liian pieni ({size} tavua).")
    log(f"Ladattu {size / 1024 / 1024:.1f} MB -> {dest}")
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

    background = (
        f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},gblur=sigma={sigma}[bgb]"
    )

    if model == "1":
        pct = float(crop["zoom_height_pct"]) / 100.0
        fg_h = int(h * pct)
        fg_h -= fg_h % 2  # x264 vaatii parillisen korkeuden
        foreground = (
            f"[fg]scale={w}:{fg_h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{fg_h}[fgs]"
        )
    else:
        foreground = (
            f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[fgs]"
        )

    return (
        f"[0:v]split=2[bg][fg];{background};{foreground};"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]"
    )


def run_ffmpeg(src, dest, model, crop, crf=20):
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", src,
        "-filter_complex", build_filter(model, crop),
        "-map", "[v]",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", str(crf),
        "-c:a", "aac",
        "-b:a", "160k",
        "-movflags", "+faststart",
        dest,
    ]
    log(" ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ProcessError("ffmpeg epäonnistui: " + (result.stderr or "")[-400:])
    return dest


def fit_under_limit(src, dest, model, crop):
    """Enkoodaa uudelleen, jos tulos ei mahdu Telegramin 50 MB rajaan."""
    run_ffmpeg(src, dest, model, crop)
    size = os.path.getsize(dest)
    log(f"Rajattu tiedosto {size / 1024 / 1024:.1f} MB")
    if size <= TELEGRAM_MAX_BYTES:
        return dest, size

    log("Yli 50 MB — enkoodataan uudelleen tiukemmalla laadulla.")
    run_ffmpeg(src, dest, model, crop, crf=27)
    size = os.path.getsize(dest)
    log(f"Toinen yritys {size / 1024 / 1024:.1f} MB")
    return dest, size


# ---------------------------------------------------------------------------

def main():
    clip_url = os.environ.get("CLIP_URL", "").strip()
    model = os.environ.get("MODEL", "").strip()
    kick_token = os.environ.get("KICK_AUTH_TOKEN", "").strip()
    reply_to = os.environ.get("REQUEST_MESSAGE_ID", "").strip()

    if model not in MODEL_NAMES:
        raise ProcessError(f"Tuntematon rajausmalli {model!r} (odotin 1 tai 2).")
    if not kick_token:
        raise ProcessError("KICK_AUTH_TOKEN puuttuu GitHub-secreteistä.")

    channel, clip_id = parse_clip_url(clip_url)
    crop = load_crop_settings()
    os.makedirs(WORK_DIR, exist_ok=True)
    raw = os.path.join(WORK_DIR, f"{clip_id}.src.mp4")
    out = os.path.join(WORK_DIR, f"{clip_id}-malli{model}.mp4")

    log(f"Klippi {clip_id} kanavalta {channel}, malli {model}")

    download(request_download_url(clip_id, kick_token), raw)
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
        sys.exit(1)
