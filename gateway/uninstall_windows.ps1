<#
.SYNOPSIS
    Az Iroda RFID gateway szolgáltatás leállítása és eltávolítása.

.DESCRIPTION
    A szolgáltatást eltávolítja, az adatokat (C:\ProgramData\IrodaGateway)
    alapból NEM törli - csak rákérdez. Ott van a puffer adatbázis, amiben még
    lehetnek fel nem töltött bélyegzések.

.EXAMPLE
    PowerShell rendszergazdaként:
      cd C:\iroda-system\gateway
      .\uninstall_windows.ps1
#>

[CmdletBinding()]
param(
    [string]$ServiceName = "RfidGateway",
    [string]$DataDir     = "C:\ProgramData\IrodaGateway"
)

$ErrorActionPreference = "Stop"

$GatewayDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Definition }
$NssmDir    = Join-Path $GatewayDir "nssm"

function Write-Step  ([string]$Text) { Write-Host ""; Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok    ([string]$Text) { Write-Host "    [OK]   $Text" -ForegroundColor Green }
function Write-Warn2 ([string]$Text) { Write-Host "    [!]    $Text" -ForegroundColor Yellow }
function Write-Fail  ([string]$Text) { Write-Host "    [HIBA] $Text" -ForegroundColor Red }

Write-Step "Rendszergazdai jog ellenőrzése"
$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Fail "Ez a script rendszergazdai jogot igényel (PowerShell -> Futtatás rendszergazdaként)."
    exit 1
}
Write-Ok "Rendszergazdaként futsz."

# --------------------------------------------------------------------------
Write-Step "NSSM keresése"
$nssm = $null
$existing = Get-Command nssm.exe -ErrorAction SilentlyContinue
if ($existing) {
    $nssm = $existing.Source
} else {
    $local = Get-ChildItem -Path $NssmDir -Filter "nssm.exe" -Recurse -ErrorAction SilentlyContinue |
             Where-Object { $_.FullName -match "win64" } | Select-Object -First 1
    if ($local) { $nssm = $local.FullName }
}

# --------------------------------------------------------------------------
Write-Step "A(z) $ServiceName szolgáltatás eltávolítása"
$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $service) {
    Write-Ok "Nincs ilyen szolgáltatás, nincs mit eltávolítani."
} elseif (-not $nssm) {
    Write-Fail "A szolgáltatás létezik, de NSSM-et nem találok."
    Write-Host "    Kézzel:  sc.exe stop $ServiceName   majd   sc.exe delete $ServiceName"
    exit 1
} else {
    if ($service.Status -eq "Running") {
        Write-Host "    Leállítom..."
        & $nssm stop $ServiceName | Out-Null
        Start-Sleep -Seconds 3
    }
    & $nssm remove $ServiceName confirm | Out-Null
    Start-Sleep -Seconds 1
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        Write-Warn2 "A szolgáltatás még látszik. Néha újraindítás után tűnik el."
    } else {
        Write-Ok "A szolgáltatás eltávolítva."
    }
}

# --------------------------------------------------------------------------
Write-Step "Adatok: $DataDir"
if (-not (Test-Path $DataDir)) {
    Write-Ok "Nincs ilyen mappa, nincs mit törölni."
    exit 0
}

$dbPath = Join-Path $DataDir "queue.db"
if (Test-Path $dbPath) {
    $size = "{0:N0} kB" -f ((Get-Item $dbPath).Length / 1KB)
    Write-Host "    A puffer adatbázis megvan: $dbPath ($size)"
    Write-Host "    Ebben még lehetnek fel nem töltött bélyegzések."
} else {
    Write-Host "    Nincs puffer adatbázis."
}

Write-Host ""
Write-Warn2 "Az adatokat alapból MEGTARTOM."
$answer = Read-Host "Töröljem a $DataDir mappát a naplókkal és a pufferrel együtt? (igen/nem)"
if ($answer -eq "igen") {
    $second = Read-Host "Biztos? Ez visszavonhatatlan. Írd be még egyszer: igen"
    if ($second -eq "igen") {
        Remove-Item -Path $DataDir -Recurse -Force
        Write-Ok "Törölve: $DataDir"
    } else {
        Write-Ok "Nem töröltem semmit."
    }
} else {
    Write-Ok "Nem töröltem semmit. A mappa a helyén maradt: $DataDir"
}

Write-Host ""
Write-Host "Kész. A gateway mappa (.venv, .env) érintetlen maradt."
