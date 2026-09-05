# One-command bootstrap for Windows (PowerShell).
#   .\start.ps1          start everything
#   .\start.ps1 stop     stop
#   .\start.ps1 logs     follow logs
#   .\start.ps1 scan     ingest videos
#   .\start.ps1 seed     load the example accounts/themes
param([string]$Command = "up")

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Invoke-Compose {
    param([Parameter(ValueFromRemainingArguments = $true)]$Args)
    & docker compose @Args
    if ($LASTEXITCODE -ne 0) { throw "docker compose failed" }
}

function New-RandomKey {
    # Fernet requires url-safe base64 of exactly 32 bytes.
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    [Convert]::ToBase64String($bytes).Replace('+', '-').Replace('/', '_')
}

function New-RandomPassword {
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    ($bytes | ForEach-Object { $_.ToString("x2") }) -join ""
}

function Set-EnvValue {
    param([string]$Key, [string]$Value)
    $lines = Get-Content .env
    if ($lines -match "^$Key=.+") { return }   # already set; never overwrite
    if ($lines -match "^$Key=") {
        $lines = $lines | ForEach-Object {
            if ($_ -match "^$Key=") { "$Key=$Value" } else { $_ }
        }
        Set-Content .env $lines
    } else {
        Add-Content .env "$Key=$Value"
    }
}

try { docker info | Out-Null } catch {
    Write-Error "Docker is not running. Start Docker Desktop and try again."
    exit 1
}

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "Created .env from the template."
}
Set-EnvValue "APP_ENCRYPTION_KEY" (New-RandomKey)
Set-EnvValue "POSTGRES_PASSWORD" (New-RandomPassword)
Set-EnvValue "ADMIN_API_TOKEN" (New-RandomPassword)
New-Item -ItemType Directory -Force -Path "videos\to_publish" | Out-Null

switch ($Command) {
    { $_ -in "stop", "down" } { Invoke-Compose down }
    "logs" { Invoke-Compose logs -f scheduler worker bot }
    "scan" { Invoke-Compose run --rm worker python -m src.manage scan }
    "seed" { Invoke-Compose run --rm worker python -m scripts.seed_example }
    "tick" { Invoke-Compose run --rm worker python -m src.manage tick }
    "list" { Invoke-Compose run --rm worker python -m src.manage list }
    "reset" {
        if (Select-String -Path .env -Pattern '^DATABASE_URL=.+' -Quiet) {
            Write-Error "DATABASE_URL points at a managed database. Refusing to wipe it."
            exit 1
        }
        $confirm = Read-Host "This deletes ALL publication history. Type YES"
        if ($confirm -ne "YES") { Write-Host "Cancelled."; exit 1 }
        Invoke-Compose down -v
        Invoke-Compose run --rm migrate
    }
    default {
        Invoke-Compose build
        # A managed database (Neon, RDS, ...) means no local Postgres container.
        if (Select-String -Path .env -Pattern '^DATABASE_URL=.+' -Quiet) {
            Write-Host "Using the managed database from DATABASE_URL."
            Invoke-Compose up -d redis
        } else {
            Invoke-Compose up -d postgres redis
        }
        Invoke-Compose run --rm migrate
        Invoke-Compose up -d scheduler worker bot api
        Write-Host ""
        Write-Host "Running. Next:"
        Write-Host "  1. Fill TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env (see SETUP.md)"
        Write-Host "  2. .\start.ps1 seed        # example accounts and themes"
        Write-Host "  3. .\start.ps1 scan        # ingest videos\to_publish"
        Write-Host "  4. Send /status to your Telegram bot"
        Write-Host ""
        Write-Host "Logs: .\start.ps1 logs"
    }
}
