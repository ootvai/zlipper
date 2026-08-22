# zlipper

Seuraa Twitch- ja Kick-kanavia ja ilmoittaa **Telegramiin** heti kun uusi
klippi ilmestyy. Kick-klipin voi lisäksi rajata pystyvideoksi tai ladata
sellaisenaan painamalla ilmoituksen nappia. Kaikki pyörii GitHub Actionsissa —
omaa palvelinta ei tarvita, kone saa olla kiinni.

```
Kick / Twitch  ──►  klippivahti (10 min välein)  ──►  Telegram-ilmoitus
                                                          │
                                          painat [Zoomattu]/[Koko kuva]
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
| `check_clips.py` | Klippivahti. Ajetaan 10 min välein. |
| `process_clip.py` | Lataa Kick-klipin ja rajaa sen ffmpegillä. |
| `telegram.py` | Yhteinen Telegram-lähetys uudelleenyrityksineen. |
| `worker/worker.js` | Cloudflare Worker: Telegram-vastaus → GitHub-dispatch **ja** Klippivahdin ajastin. |
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

### 2. Klippivahti

Toimii heti kun secretit ovat paikallaan. Ensimmäinen ajo merkitsee
nykyiset klipit nähdyiksi lähettämättä ilmoituksia.

Testaa: **Actions → Klippivahti → Run workflow**.

### 3. Rajausputki (valinnainen)

Vaatii Cloudflare-tilin. Aja PowerShellissa:

```powershell
cd worker
.\setup.ps1
```

Skripti kirjaa sinut Cloudflareen, kysyy tunnukset, vie ne Workerin
secreteiksi, deployaa ja rekisteröi webhookin. Tunnuksia ei kirjoiteta
levylle eikä komentoriville.

Se kysyy kolme asiaa:

| Kysyy | Mistä |
|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather antoi botin luonnissa |
| `GITHUB_TOKEN` | GitHub PAT, oikeus **Contents: read & write** tähän repoon |
| `ALLOWED_CHAT_ID` | Sama luku kuin GitHubin `TELEGRAM_CHAT_ID` |

`TELEGRAM_WEBHOOK_SECRET` arvotaan automaattisesti — sitä ei tarvitse
keksiä eikä muistaa.

Jos Worker on jo pystyssä ja haluat vain päivittää sen uuteen versioon,
aja `.\update-webhook.ps1`. Se deployaa Workerin ja rekisteröi
webhookin uudelleen; pelkkä `wrangler deploy` ei riitä, koska
`allowed_updates` on Telegramin päässä oleva lista eikä se muutu deployn
mukana. Skripti kysyy vain botin tokenin.

Sen jälkeen: paina Kick-klippi-ilmoituksen nappia.

| Nappi | Mitä tekee |
|---|---|
| **🔍 Zoomattu** | Rajaa 1080x1920 pystyvideoksi, kuvaan mennään sisään |
| **🖼️ Koko kuva** | Rajaa 1080x1920 pystyvideoksi, mitään ei leikata pois |
| **⬇️ Lataa klippi** | Klippi sellaisenaan: ei rajausta, ei uudelleenpakkausta |

Kaksi ensimmäistä tulevat videona. **Lataa klippi** antaa tiedoston
(`sendDocument`), jotta Telegram ei käsittele sitä mitenkään — lataamasi
tiedosto on tavu tavulta se mikä Kickin soittimessa soi. Se ei siis toistu
chatissa suoraan, vaan pitää ladata ensin. Vesileimaa ei ole: se ja klipin
perään liitetty KICK-mainoskortti tulivat vanhasta latausendpointista, ei
toistolähteestä. Napit vaihtuvat tilaksi (`⏳ Rajataan` → `✅ Rajattu`),
joten samaa klippiä ei tule vahingossa ajettua kahdesti; jos rajaus kaatuu,
napit palaavat uutta yritystä varten.

50 MB on Telegramin raja kaikelle. Rajatut versiot mahtuvat aina, koska
bitrate lasketaan kestosta. Ladattavaa klippiä ei pakata pienemmäksi — se olisi
juuri se mitä tällä napilla yritetään välttää — joten jos se ei mahdu,
ilmoitus kertoo koon ja tiedoston saa Actions-ajon artifaktista.

Numerovastaus **1** / **2** / **3** toimii yhä. Sitä tarvitaan ennen nappien
käyttöönottoa lähetetyissä ilmoituksissa ja yli 48 h vanhoissa viesteissä,
joiden sisältöä Telegram ei enää anna napin painalluksen mukana.

Putki päättyy siihen, että rajattu video tulee Telegramiin. Loput
editointi tehdään käsin — mitään ei julkaista automaattisesti.

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

Lähteenä käytetään klipin omaa toistolähdettä (HLS), ei sivuston
Download-nappia. Nappi liittää klipin perään 1,4 sekunnin KICK-kortin ja
tarjoaa matalamman bitraten; toistolähteessä ei ole kumpaakaan eikä se
vaadi tunnistautumista.

## Kanavien muokkaus

Muokkaa `config.json`-tiedostoa suoraan GitHubissa. Käytä URL-nimeä, ei
näyttönimeä: `kick.com/pullis` → `pullis`.

## Säädöt

| Avain | Merkitys |
|---|---|
| `poll_lookback_minutes` | Kuinka kauas taaksepäin Twitchistä haetaan |
| `max_clip_age_hours` | Tätä vanhemmista klipeistä ei ilmoiteta |
| `clipper_known_min_clips` | Montako klippiä ennen "✂️ Tuttu klippaaja" |
| `clipper_trusted_min_clips` | Montako klippiä ennen "⭐ Luotettava klippaaja" |
| `health_alert_after_failures` | Peräkkäisiä epäonnistumisia ennen hälytystä |
| `heartbeat_hours` | Elonmerkin väli |
| `silent_channel_days` | Milloin hiljaisesta kanavasta huomautetaan |
| `crop` | Rajauksen mitat, zoom-osuus ja sumennus |

## Ajastus

Tarkistusväli asetetaan `worker/wrangler.toml`:n `[triggers]`-lohkossa,
**ei** workflow-tiedostossa:

```toml
[triggers]
crons = ["*/10 * * * *"]
```

Muutos astuu voimaan `npx wrangler deploy`llä.

**Miksi ajastin on Cloudflaressa eikä GitHubissa.** GitHubin oma cron ei
laukea luvatusti. Repon koko ajohistoriasta mitattuna (719 ajoa,
26.7.–22.8.2026, cron `*/10`):

| | |
|---|---|
| mediaani ajojen väli | 42,7 min |
| p90 | 109 min |
| pisin katkos | 365 min |
| välejä ≤ 12 min | 0 kpl |

Jonotusaika `created_at` → `run_started_at` oli mediaanilta ja
maksimiltaan 0 s, eli runnereita oli koko ajan vapaana: ajot eivät
viivästyneet ruuhkassa vaan **jäivät kokonaan laukeamatta**. Pudotetut
ajot eivät myöskään kertaudu myöhemmin.

Tällä oli konkreettinen seuraus. `check_clips.py` hakee Twitchistä vain
`poll_lookback_minutes` (60 min) taaksepäin, joten yli tunnin katkoissa
osa Twitch-klipeistä jäi kokonaan näkemättä — mitattuna **24,3 %
kaikesta kuluneesta ajasta oli kattamatta**. Kick kestää katkot, koska
sen haku selaa klippilistaa kursorilla ja vertaa `state.json`iin.

GitHubin `schedule:`-lohko on jätetty päälle varajärjestelmäksi. Se ei
maksa mitään ja pelastaa jos Cloudflare on nurin; päällekkäiset ajot
hoitaa workflown `concurrency`-ryhmä.

## Tarkistus käsin

Telegramiin `/tarkista` (tai `/check`) käynnistää Klippivahdin heti
odottamatta seuraavaa ajastettua ajoa. Käsin pyydetty ajo vastaa myös
silloin kun mitään ei löytynyt, jotta hiljaisuus ei ole monitulkintaista.
Ajastettu ajo pysyy hiljaisena kuten ennenkin.

## Nollaus

Tyhjennä `state.json`:

```json
{ "twitch": {}, "kick": {} }
```

Seuraava ajo merkitsee nykytilan uudeksi lähtökohdaksi. Klippaajien
laskurit säilyvät, koska ne ovat erillisessä `creators.json`-tiedostossa.

## Tunnetut rajoitukset

- **Kickin API.** Sekä klippilistaus että klipin toistolähde käyttävät
  dokumentoimattomia endpointteja. Ne toimivat nyt, mutta voivat hajota
  ilman varoitusta. Silloin Actions-lokiin tulee HTTP-virhe ja
  klippivahti lähettää terveyshälytyksen; Twitch jatkaa normaalisti.
- **Vain Kick rajataan.** Twitch-klipeille ei ole vastaavaa
  lataus-endpointtia. Numerovastaus Twitch-ilmoitukseen kertoo tämän
  suoraan sen sijaan että jäisi hiljaa jumiin.
- **Telegramin 50 MB raja.** Bot API ei ota vastaan tätä suurempaa
  tiedostoa. Rajaus yrittää tiukempaa pakkausta kerran; jos sekään ei
  riitä, video jää vain Actions-artifaktiksi.
- **Ajastuksen tarkkuus.** Cloudflaren cron laukeaa luotettavasti, mutta
  itse ajo käynnistyy ja asentaa riippuvuudet ~50 s ennen kuin ilmoitus
  voi lähteä. Ilmoitus ei siis tule sekunnin tarkkuudella.
