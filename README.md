# Klippivahti

Seuraa Twitch- ja Kick-kanavia ja lähettää **Telegram-viestin** heti kun
uusi klippi ilmestyy. Pyörii ilmaiseksi GitHub Actionsissa — ei omaa
palvelinta, kone saa olla kiinni.

## Käyttöönotto

Viisi vaihetta, noin 15 minuuttia.

### 1. Vie tiedostot GitHubiin

Luo uusi **privaatti** repo ja lataa tämän kansion sisältö sinne
(GitHubissa: "Add file" → "Upload files"). Muista myös `.github`-kansio
— se on piilotettu, joten helpoin tapa on raahata koko kansio kerralla.

### 2. Twitch-tunnukset

1. https://dev.twitch.tv/console/apps → kirjaudu
2. "Register Your Application"
   - Name: `Klippivahti` (tai mikä tahansa)
   - OAuth Redirect URLs: `https://localhost`
   - Category: `Application Integration`
3. Kopioi **Client ID**
4. "New Secret" → kopioi **Client Secret** (näkyy vain kerran)

### 3. Telegram-botti

1. Avaa Telegramissa **@BotFather**
2. `/newbot` → anna botille nimi ja käyttäjänimi
3. Kopioi saamasi **token** (muotoa `1234567890:AAF...`)
4. **Lähetä botillesi jokin viesti** (esim. `moi`) — muuten se ei saa
   lähettää sinulle mitään
5. Selvitä chat_id: avaa selaimessa
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   (korvaa `<TOKEN>` omallasi). Etsi vastauksesta `"chat":{"id":123456789`
   — se luku on **chat_id**

> Jos haluat ilmoitukset ryhmään yksityisviestin sijaan: lisää botti
> ryhmään, kirjoita ryhmään jotain, ja hae chat_id samalla tavalla.
> Ryhmän id on negatiivinen luku.

### 4. Lisää salaisuudet GitHubiin

Repossa: **Settings → Secrets and variables → Actions → New repository secret**

| Nimi | Mistä |
|---|---|
| `TWITCH_CLIENT_ID` | vaihe 2 |
| `TWITCH_CLIENT_SECRET` | vaihe 2 |
| `TELEGRAM_BOT_TOKEN` | vaihe 3 |
| `TELEGRAM_CHAT_ID` | vaihe 3 |

### 5. Testaa

Repon **Actions**-välilehti → "Klippivahti" → **Run workflow**.

Ensimmäinen ajo ei lähetä ilmoituksia — se merkitsee kaikki nykyiset
klipit nähdyiksi, jottei postilaatikko täyty vanhoista. Ilmoituksia
alkaa tulla toisesta ajosta lähtien.

Jos jokin menee pieleen, Actions-loki kertoo mikä (esim. "kanavaa X ei
löytynyt — tarkista nimi").

## Kanavien muokkaus

Muokkaa `config.json`-tiedostoa suoraan GitHubissa. Ei tarvitse koskea
koodiin.

```json
{
  "twitch_channels": ["skotti"],
  "kick_channels": ["pullis", "pact", "ogumtv"],
  "poll_lookback_minutes": 15
}
```

Käytä **URL-nimeä**, ei näyttönimeä: `kick.com/pullis` → `pullis`.

## Säädöt

| Mitä | Missä |
|---|---|
| Tarkistusväli | `.github/workflows/check-clips.yml`, rivi `cron: "*/10 * * * *"` |
| Kuinka kauas taaksepäin Twitchistä haetaan | `config.json`, `poll_lookback_minutes` |

Alle 5 min väliä ei kannata laittaa — GitHubin cron ei ole tarkka.

## Nollaus

Jos haluat aloittaa puhtaalta pöydältä, tyhjennä `state.json`:

```json
{ "twitch": {}, "kick": {} }
```

Seuraava ajo merkitsee nykytilan uudeksi lähtökohdaksi.

## Tunnetut rajoitukset

- **Kick.** Kickin virallisessa APIssa ei ole klippi-endpointtia, joten
  skripti käyttää dokumentoimatonta osoitetta. Se toimii nyt, mutta voi
  hajota ilman varoitusta. Silloin Actions-lokiin tulee HTTP-virhe eikä
  Kick-ilmoituksia tule — Twitch jatkaa normaalisti.
- **Ajastuksen tarkkuus.** GitHubin cron voi viivästyä ruuhkassa. Klipit
  tulevat kyllä perille, mutta ei sekunnin tarkkuudella.
