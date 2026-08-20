<#
    Pystyttaa Cloudflare Workerin ja rekisteroi Telegram-webhookin.

    Aja tama worker-kansiossa:   .\setup.ps1

    Skripti kysyy tunnukset itse. Niita ei kirjoiteta levylle, ei
    komentoriville eika lokiin — ne kulkevat vain putkessa wranglerille
    ja Telegramin APIlle.
#>

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$wrangler = @("--yes", "wrangler@latest")

function Read-Secret($prompt) {
    $secure = Read-Host -Prompt $prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Put-Secret($name, $value) {
    if ([string]::IsNullOrWhiteSpace($value)) { throw "$name jai tyhjaksi." }
    Write-Host "  -> $name"
    $value | npx @wrangler secret put $name
    if ($LASTEXITCODE -ne 0) { throw "wrangler secret put $name epaonnistui." }
}

# --- 0. Esitarkistukset ---------------------------------------------------

Write-Host "`n[0/4] Tarkistetaan tyokalut" -ForegroundColor Cyan
node --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Node.js puuttuu." }

npx @wrangler whoami
if ($LASTEXITCODE -ne 0) {
    Write-Host "`nEt ole kirjautunut Cloudflareen. Selain aukeaa nyt." -ForegroundColor Yellow
    npx @wrangler login
    if ($LASTEXITCODE -ne 0) { throw "Cloudflare-kirjautuminen epaonnistui." }
}

# --- 1. Salaisuudet -------------------------------------------------------

Write-Host "`n[1/4] Salaisuudet" -ForegroundColor Cyan
Write-Host "Telegram-botin token (@BotFather antoi, muotoa 1234567890:AAF...)"
$botToken = Read-Secret "TELEGRAM_BOT_TOKEN"

Write-Host "GitHub PAT, oikeus Contents: read & write repoon ootvai/zlipper"
$githubToken = Read-Secret "GITHUB_TOKEN"

Write-Host "Telegram-chatin id (sama luku kuin GitHubin TELEGRAM_CHAT_ID)"
$chatId = (Read-Host "ALLOWED_CHAT_ID").Trim()
if ($chatId -notmatch '^-?\d+$') { throw "Chat id ei nayta luvulta: $chatId" }

# Webhook-secret arvotaan tassa, jotta sita ei tarvitse keksia eika
# valittaa mistaan. Telegram hyvaksyy A-Z a-z 0-9 _ -
$bytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$webhookSecret = ([BitConverter]::ToString($bytes) -replace '-', '').ToLower()

Write-Host "`nViedaan salaisuudet Workeriin:"
Put-Secret "TELEGRAM_BOT_TOKEN"      $botToken
Put-Secret "GITHUB_TOKEN"            $githubToken
Put-Secret "ALLOWED_CHAT_ID"         $chatId
Put-Secret "TELEGRAM_WEBHOOK_SECRET" $webhookSecret

# --- 2. Deploy ------------------------------------------------------------

Write-Host "`n[2/4] Deployataan Worker" -ForegroundColor Cyan
# Ei 2>&1: PowerShell 5.1 kaaria natiivin stderrin ErrorRecordeiksi, jolloin
# wranglerin normaali edistymistuloste nayttaisi virheelta.
$deployOutput = npx @wrangler deploy
if ($LASTEXITCODE -ne 0) { throw "wrangler deploy epaonnistui." }
$deployOutput | ForEach-Object { Write-Host $_ }

$match = [regex]::Match(($deployOutput -join "`n"), 'https://[A-Za-z0-9.\-]+\.workers\.dev')
if ($match.Success) {
    $workerUrl = $match.Value
    Write-Host "`nWorkerin osoite: $workerUrl" -ForegroundColor Green
} else {
    Write-Host "`nEn loytanyt osoitetta deployn tulosteesta." -ForegroundColor Yellow
    $workerUrl = (Read-Host "Syota Workerin osoite (https://...workers.dev)").Trim()
}
if ($workerUrl -notmatch '^https://') { throw "Kelvoton Worker-osoite." }

# --- 3. Webhook -----------------------------------------------------------

Write-Host "`n[3/4] Rekisteroidaan webhook Telegramille" -ForegroundColor Cyan
$response = Invoke-RestMethod -Method Post `
    -Uri "https://api.telegram.org/bot$botToken/setWebhook" `
    -Body @{
        url = $workerUrl
        secret_token = $webhookSecret
        allowed_updates = '["message"]'
    }
if (-not $response.ok) { throw "setWebhook epaonnistui: $($response.description)" }
Write-Host "  $($response.description)" -ForegroundColor Green

# --- 4. Varmistus ---------------------------------------------------------

Write-Host "`n[4/4] Tarkistetaan tila" -ForegroundColor Cyan
$info = Invoke-RestMethod -Uri "https://api.telegram.org/bot$botToken/getWebhookInfo"
Write-Host "  url:                   $($info.result.url)"
Write-Host "  odottavia paivityksia: $($info.result.pending_update_count)"
if ($info.result.last_error_message) {
    Write-Host "  viimeisin virhe:       $($info.result.last_error_message)" -ForegroundColor Yellow
}

$botToken = $null
$githubToken = $null
$webhookSecret = $null

Write-Host "`nValmis. Vastaa Telegramissa johonkin Kick-klippi-ilmoitukseen" -ForegroundColor Green
Write-Host "numerolla 1 tai 2 ja katso lahteeko rajaus kayntiin." -ForegroundColor Green
Write-Host "Muista etta PR:n on oltava mergattuna mainiin, muuten GitHub ei"
Write-Host "loyda process-clip.yml-workflowta."
