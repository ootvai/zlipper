# zlipper

Seuraa Twitch- ja Kick-kanavia ja ilmoittaa **Telegramiin** heti kun uusi
klippi ilmestyy. Kick-klipin voi lisäksi ladata ja rajata pystyvideoksi
vastaamalla ilmoitukseen numerolla. Kaikki pyörii GitHub Actionsissa —
omaa palvelinta ei tarvita, kone saa olla kiinni.

```
Kick / Twitch  ──►  klippivahti (cron 10 min)  ──►  Telegram-ilmoitus
                                                          │
                                             vastaat "1" tai "2"
                                                          ▼
                                            Cloudflare Worker (webhook)
                                                          │
                                              repository_dispatch
                                                          ▼
                                     Rajaa klippi -workflow (lataus + ffmpeg)
                                                          │
                                                          ▼
                                          rajattu video Telegramiin
```

## Osat

| Tiedosto | Tehtävä |
|---|---|
| `check_clips.py` | Klippivahti. Ajetaan cronilla 10 min välein. |
| `process_clip.py` | Lataa Kick-klipin ja rajaa sen ffmpegillä. |
| `telegram.py` | Yhteinen Telegram-lähetys uudelleenyrityksineen. |
| `worker/worker.js` | Cloudflare Worker: Telegram-vastaus → GitHub-dispatch. |
| `config.json` | Seurattavat kanavat ja säädöt. |
| `state.json` | Nähdyt klipit. Workflow committaa itse. |
| `creators.json` | Klippaajakohtaiset laskurit. |

## Käyttöönotto

### 1. GitHub-secretit

**Settings → Secrets and variables → Actions**

| Nimi | Mistä |
|---|---|
| `TWITCH_CLIENT_ID` | https://dev.twitch.tv/console/apps |
| `TWITCH_CLIENT_SECRET` | sama |
| `TELEGRAM_BOT_TOKEN` | @BotFather → `/newbot` |
| `TELEGRAM_CHAT_ID` | `https://api.telegram.org/bot<TOKEN>/getUpdates` |
| `KICK_AUTH_TOKEN` | Kickin istuntotoken, tarvitaan vain rajausputkeen |

`KICK_AUTH_TOKEN` löytyy kirjautuneena kick.comilta selaimen
kehitystyökaluista (Network → mikä tahansa API-kutsu →
`Authorization: Bearer …`). Se vanhenee aikanaan; kun rajaus alkaa
vastata "Kick hylkäsi tunnistautumisen", token on päivitettävä.

### 2. Klippivahti

Toimii heti kun secretit ovat paikallaan. Ensimmäinen ajo merkitsee
nykyiset klipit nähdyiksi lähettämättä ilmoituksia.

Testaa: **Actions → Klippivahti → Run workflow**.

### 3. Rajausputki (valinnainen)

Vaatii Cloudflare-tilin ja `wrangler`-työkalun.

```bash
cd worker
wrangler secret put TELEGRAM_BOT_TOKEN
wrangler secret put TELEGRAM_WEBHOOK_SECRET
wrangler secret put GITHUB_TOKEN
wrangler deploy
```

- `TELEGRAM_WEBHOOK_SECRET` on itse keksitty satunnainen merkkijono.
- `GITHUB_TOKEN` on henkilökohtainen access token, jolla on tähän repoon
  oikeus **Contents: read & write** (fine-grained) tai `repo` (classic).
- Aseta `ALLOWED_CHAT_ID` tiedostoon `wrangler.toml` — sama luku kuin
  `TELEGRAM_CHAT_ID`.

Rekisteröi webhook Telegramille:

```bash
curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" -d "url=https://zlipper-webhook.<tili>.workers.dev" -d "secret_token=<TELEGRAM_WEBHOOK_SECRET>"
```

Sen jälkeen: vastaa mihin tahansa Kick-klippi-ilmoitukseen numerolla
**1** (zoomattu) tai **2** (koko kuva).

> `repository_dispatch` ajaa workflown aina default-branchista. Putki ei
> siis toimi ennen kuin `process-clip.yml` on mainissa.

## Rajausmallit

Molemmissa tausta on saman klipin oma kuva, skaalattuna täyttämään
pystyruutu, keskeltä rajattuna ja sumennettuna. Kirkkautta ei säädetä
mihinkään suuntaan, joten reunat seuraavat klipin omaa valaistusta.
Tekstityksiä, logoja tai muita kerroksia ei lisätä kumpaankaan.

- **Malli 1 — zoomattu.** Pääkuva skaalataan peittämään 67 % ruudun
  korkeudesta ja rajataan keskeltä. Kuvaan mennään sisään, reunat
  jäävät pois.
- **Malli 2 — koko kuva.** Pääkuva mahtuu ruutuun kokonaisena, mitään ei
  leikata pois. Sumennettua reunaa on enemmän.

Mitat ja sumennus säädetään `config.json`-tiedoston `crop`-lohkosta.

## Kanavien muokkaus

Muokkaa `config.json`-tiedostoa suoraan GitHubissa. Käytä URL-nimeä, ei
näyttönimeä: `kick.com/pullis` → `pullis`.

## Säädöt

| Avain | Merkitys |
|---|---|
| `poll_lookback_minutes` | Kuinka kauas taaksepäin Twitchistä haetaan |
| `max_clip_age_hours` | Tätä vanhemmista klipeistä ei ilmoiteta |
| `frequent_clipper_min_clips` | Montako klippiä ennen ⭐-merkkiä |
| `health_alert_after_failures` | Peräkkäisiä epäonnistumisia ennen hälytystä |
| `heartbeat_hours` | Elonmerkin väli |
| `silent_channel_days` | Milloin hiljaisesta kanavasta huomautetaan |
| `crop` | Rajauksen mitat, zoom-osuus ja sumennus |

Tarkistusväli on `.github/workflows/check-clips.yml`, rivi
`cron: "*/10 * * * *"`. Alle 5 min ei kannata laittaa — GitHubin cron ei
ole tarkka.

## Nollaus

Tyhjennä `state.json`:

```json
{ "twitch": {}, "kick": {} }
```

Seuraava ajo merkitsee nykytilan uudeksi lähtökohdaksi. Klippaajien
laskurit säilyvät, koska ne ovat erillisessä `creators.json`-tiedostossa.

## Tunnetut rajoitukset

- **Kickin API.** Sekä klippilistaus että lataus käyttävät
  dokumentoimattomia endpointteja. Ne toimivat nyt, mutta voivat hajota
  ilman varoitusta. Silloin Actions-lokiin tulee HTTP-virhe ja
  klippivahti lähettää terveyshälytyksen; Twitch jatkaa normaalisti.
- **Vain Kick rajataan.** Twitch-klipeille ei ole vastaavaa
  lataus-endpointtia. Numerovastaus Twitch-ilmoitukseen kertoo tämän
  suoraan sen sijaan että jäisi hiljaa jumiin.
- **Telegramin 50 MB raja.** Bot API ei ota vastaan tätä suurempaa
  tiedostoa. Rajaus yrittää tiukempaa pakkausta kerran; jos sekään ei
  riitä, video jää vain Actions-artifaktiksi.
- **Ajastuksen tarkkuus.** GitHubin cron voi viivästyä ruuhkassa.
  Klipit tulevat kyllä perille, mutta ei sekunnin tarkkuudella.
