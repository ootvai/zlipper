<#
    Deployaa Workerin ja rekisteroi Telegram-webhookin uudelleen niin,
    etta myos nappien painallukset (callback_query) tulevat perille.

    Aja tama worker-kansiossa:   .\update-webhook.ps1

    Kysyy vain botin tokenin. GitHub-PATia tai chat idta ei tarvita —
    ne ovat jo Workerissa eika tama koske niihin.

    Taustaksi: setWebhookin allowed_updates on Telegramin paassa oleva
    lista, eika se muutu Workeria deployaamalla. Alkuperainen setup.ps1
    rekisteroi vain ["message"], joten napin painallus ei olisi tullut
    Workerille lainkaan.
#>

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$wrangler = @("--yes", "wrangler@latest")

# --- 1. Deploy ------------------------------------------------------------

Write-Host "`n[1/3] Deployataan Worker" -ForegroundColor Cyan
$deployOutput = npx @wrangler deploy
if ($LASTEXITCODE -ne 0) { throw "wrangler deploy epaonnistui." }
$deployOutput | ForEach-Object { Write-Host $_ }

$match = [regex]::Match(($deployOutput -join "`n"), 'https://[A-Za-z0-9.\-]+\.workers\.dev')
if ($match.Success) {
    $workerUrl = $match.Value
} else {
    $workerUrl = (Read-Host "Syota Workerin osoite (https://...workers.dev)").Trim()
}
if ($workerUrl -notmatch '^https://') { throw "Kelvoton Worker-osoite." }
Write-Host "Workerin osoite: $workerUrl" -ForegroundColor Green

# --- 2. Webhook-secret ----------------------------------------------------

# setWebhook ei osaa muuttaa pelkkaa allowed_updatesia: jos secret_token
# jatetaan pois, se nollautuu ja Worker alkaa vastata 403:lla. Vanhaa
# secretia ei saa Cloudflaresta ulos, joten arvotaan uusi ja asetetaan se
# molempiin paihin samalla kertaa.
Write-Host "`n[2/3] Uusi webhook-secret" -ForegroundColor Cyan

$bytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$webhookSecret = ([BitConverter]::ToString($bytes) -replace '-', '').ToLower()

Write-Host "  -> TELEGRAM_WEBHOOK_SECRET"
$webhookSecret | npx @wrangler secret put TELEGRAM_WEBHOOK_SECRET
if ($LASTEXITCODE -ne 0) { throw "wrangler secret put TELEGRAM_WEBHOOK_SECRET epaonnistui." }

# --- 3. Webhook -----------------------------------------------------------

Write-Host "`n[3/3] Rekisteroidaan webhook" -ForegroundColor Cyan
Write-Host "Telegram-botin token (@BotFather antoi, muotoa 1234567890:AAF...)"
$secure = Read-Host -Prompt "TELEGRAM_BOT_TOKEN" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $botToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}
if ([string]::IsNullOrWhiteSpace($botToken)) { throw "Token jai tyhjaksi." }

$response = Invoke-RestMethod -Method Post `
    -Uri "https://api.telegram.org/bot$botToken/setWebhook" `
    -Body @{
        url = $workerUrl
        secret_token = $webhookSecret
        allowed_updates = '["message","callback_query"]'
    }
if (-not $response.ok) { throw "setWebhook epaonnistui: $($response.description)" }
Write-Host "  $($response.description)" -ForegroundColor Green

$info = Invoke-RestMethod -Uri "https://api.telegram.org/bot$botToken/getWebhookInfo"
Write-Host "  url:                   $($info.result.url)"
Write-Host "  sallitut paivitykset:  $($info.result.allowed_updates -join ', ')"
Write-Host "  odottavia paivityksia: $($info.result.pending_update_count)"
if ($info.result.last_error_message) {
    Write-Host "  viimeisin virhe:       $($info.result.last_error_message)" -ForegroundColor Yellow
}

$botToken = $null
$webhookSecret = $null

Write-Host "`nValmis. Paina jonkin Kick-klippi-ilmoituksen nappia ja katso" -ForegroundColor Green
Write-Host "vaihtuuko se tilaan '(kello) Rajataan'." -ForegroundColor Green
