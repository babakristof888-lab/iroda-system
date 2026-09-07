<#
.SYNOPSIS
    Az Iroda RFID gateway telepítése Windows 10-re, NSSM szolgáltatásként.

.DESCRIPTION
    Amit csinál:
      1. ellenőrzi a rendszergazdai jogot és a Python 3.11+ meglétét,
      2. létrehozza a venv-et a gateway\.venv alatt, és telepíti a függőségeket,
      3. létrehozza a C:\ProgramData\IrodaGateway\logs mappát,
      4. letölti az NSSM-et, ha nincs, és regisztrálja a RfidGateway szolgáltatást,
      5. figyelmeztet, ha nincs .env fájl.

    Meglévő fájlt nem töröl. Ha a szolgáltatás már létezik, csak a beállításait
    frissíti.

.EXAMPLE
    Jobb gomb a PowerShell ikonon -> Futtatás rendszergazdaként, majd:
      cd C:\iroda-system\gateway
      Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
      .\install_windows.ps1
#>

[CmdletBinding()]
param(
    [string]$ServiceName = "RfidGateway",
    [string]$DataDir     = "C:\ProgramData\IrodaGateway"
)

$ErrorActionPreference = "Stop"

$GatewayDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Definition }
$VenvDir    = Join-Path $GatewayDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$LogDir     = Join-Path $DataDir "logs"
$NssmDir    = Join-Path $GatewayDir "nssm"
$NssmUrl    = "https://nssm.cc/release/nssm-2.24.zip"

function Write-Step  ([string]$Text) { Write-Host ""; Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok    ([string]$Text) { Write-Host "    [OK]   $Text" -ForegroundColor Green }
function Write-Warn2 ([string]$Text) { Write-Host "    [!]    $Text" -ForegroundColor Yellow }
function Write-Fail  ([string]$Text) { Write-Host "    [HIBA] $Text" -ForegroundColor Red }

# --------------------------------------------------------------------------
# 1. Rendszergazdai jog
# --------------------------------------------------------------------------
Write-Step "Rendszergazdai jog ellenőrzése"
$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Fail "Ez a script rendszergazdai jogot igényel."
    Write-Host ""
    Write-Host "    Zárd be ezt az ablakot, majd: Start menu -> ,,PowerShell'' -> jobb gomb ->"
    Write-Host "    ,,Futtatás rendszergazdaként'', és indítsd újra a scriptet."
    exit 1
}
Write-Ok "Rendszergazdaként futsz."

# --------------------------------------------------------------------------
# 2. Python 3.11+
# --------------------------------------------------------------------------
Write-Step "Python keresése"
$python = $null
foreach ($candidate in @("py -3.11", "py -3", "python")) {
    $parts  = $candidate.Split(" ")
    $exe    = $parts[0]
    $pyArgs = if ($parts.Length -gt 1) { @($parts[1..($parts.Length - 1)]) } else { @() }
    try {
        $version = & $exe @pyArgs "-c" "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    } catch {
        continue
    }
    if ($LASTEXITCODE -ne 0 -or -not $version) { continue }

    try { $parsed = [Version]$version } catch { continue }
    if ($parsed -ge [Version]"3.11") {
        $python = @{ Exe = $exe; PyArgs = $pyArgs; Version = $version }
        break
    }
    Write-Warn2 "Talált Python $version - ez túl régi, 3.11 vagy újabb kell."
}

if (-not $python) {
    Write-Fail "Nem találtam Python 3.11 vagy újabb verziót."
    Write-Host ""
    Write-Host "    Töltsd le innen:  https://www.python.org/downloads/windows/"
    Write-Host "    A telepítőben MINDKETTŐT pipáld be:"
    Write-Host "      - ,,Install for all users'' (a szolgáltatás nem a te fiókoddal fut)"
    Write-Host "      - ,,Add python.exe to PATH''"
    Write-Host "    Utána indítsd újra ezt a scriptet."
    exit 1
}
Write-Ok "Python $($python.Version) megvan."

# --------------------------------------------------------------------------
# 3. Virtuális környezet
# --------------------------------------------------------------------------
Write-Step "Virtuális környezet (.venv)"
if (Test-Path $VenvPython) {
    Write-Ok "Már létezik: $VenvDir"
} else {
    $pyArgs = $python.PyArgs
    & $python.Exe @pyArgs "-m" "venv" $VenvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPython)) {
        Write-Fail "A venv létrehozása nem sikerült."
        exit 1
    }
    Write-Ok "Létrehozva: $VenvDir"
}

Write-Step "Függőségek telepítése"
& $VenvPython "-m" "pip" "install" "--upgrade" "pip" "--quiet"
& $VenvPython "-m" "pip" "install" "-r" (Join-Path $GatewayDir "requirements.txt") "--quiet"
if ($LASTEXITCODE -ne 0) {
    Write-Fail "A pip install nem sikerült. Van internetkapcsolat?"
    exit 1
}
Write-Ok "pyserial, requests, python-dotenv telepítve."

# --------------------------------------------------------------------------
# 4. Adatkönyvtár
# --------------------------------------------------------------------------
Write-Step "Adatkönyvtár: $DataDir"
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
Write-Ok "Megvan: $LogDir"
Write-Warn2 "Ne felejtsd el felvenni a Windows Defender kizárások közé: $DataDir"

# --------------------------------------------------------------------------
# 5. .env
# --------------------------------------------------------------------------
Write-Step ".env fájl"
$EnvFile = Join-Path $GatewayDir ".env"
if (Test-Path $EnvFile) {
    Write-Ok "Megvan: $EnvFile"
    $apiKeyLine = Select-String -Path $EnvFile -Pattern "^\s*GATEWAY_API_KEY\s*=\s*\S" -Quiet
    if (-not $apiKeyLine) {
        Write-Warn2 "A GATEWAY_API_KEY üresnek látszik. Enélkül a szerver 401-et ad."
    }
} else {
    Write-Warn2 "Nincs .env fájl! A szolgáltatás elindul, de nem tud feltölteni."
    Write-Host "    Csináld meg most:"
    Write-Host "      copy `"$GatewayDir\.env.example`" `"$EnvFile`""
    Write-Host "      notepad `"$EnvFile`""
    Write-Host "    A GATEWAY_API_KEY értékének egyeznie kell a Railway-en beállítottal."
}

# --------------------------------------------------------------------------
# 6. NSSM
# --------------------------------------------------------------------------
Write-Step "NSSM keresése"
$nssm = $null
$existing = Get-Command nssm.exe -ErrorAction SilentlyContinue
if ($existing) {
    $nssm = $existing.Source
    Write-Ok "A PATH-ban megvan: $nssm"
} else {
    $local = Get-ChildItem -Path $NssmDir -Filter "nssm.exe" -Recurse -ErrorAction SilentlyContinue |
             Where-Object { $_.FullName -match "win64" } | Select-Object -First 1
    if ($local) {
        $nssm = $local.FullName
        Write-Ok "Korábban letöltve: $nssm"
    }
}

if (-not $nssm) {
    Write-Host "    Letöltöm az NSSM-et: $NssmUrl"
    $zipPath = Join-Path $env:TEMP "nssm.zip"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $NssmUrl -OutFile $zipPath -UseBasicParsing
        New-Item -ItemType Directory -Path $NssmDir -Force | Out-Null
        Expand-Archive -Path $zipPath -DestinationPath $NssmDir -Force
        Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
        $local = Get-ChildItem -Path $NssmDir -Filter "nssm.exe" -Recurse |
                 Where-Object { $_.FullName -match "win64" } | Select-Object -First 1
        if ($local) { $nssm = $local.FullName }
    } catch {
        Write-Fail "Az NSSM letöltése nem sikerült: $($_.Exception.Message)"
    }
}

if (-not $nssm) {
    Write-Fail "Nincs NSSM, a szolgáltatást nem tudom regisztrálni."
    Write-Host ""
    Write-Host "    Töltsd le kézzel a https://nssm.cc/download címről, csomagold ki, és"
    Write-Host "    másold a win64\nssm.exe fájlt ide: $NssmDir"
    Write-Host "    Utána futtasd újra ezt a scriptet."
    exit 1
}
Write-Ok "NSSM: $nssm"

# --------------------------------------------------------------------------
# 7. A szolgáltatás regisztrálása
# --------------------------------------------------------------------------
Write-Step "A(z) $ServiceName szolgáltatás beállítása"
$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($service) {
    Write-Warn2 "A szolgáltatás már létezik, csak a beállításait frissítem."
    if ($service.Status -eq "Running") {
        & $nssm stop $ServiceName | Out-Null
        Start-Sleep -Seconds 2
    }
} else {
    & $nssm install $ServiceName $VenvPython
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Az nssm install nem sikerült."
        exit 1
    }
    Write-Ok "Szolgáltatás létrehozva."
}

& $nssm set $ServiceName Application      $VenvPython        | Out-Null
& $nssm set $ServiceName AppParameters    "-u main.py"       | Out-Null
& $nssm set $ServiceName AppDirectory     $GatewayDir        | Out-Null
& $nssm set $ServiceName AppStdout        (Join-Path $LogDir "out.log") | Out-Null
& $nssm set $ServiceName AppStderr        (Join-Path $LogDir "err.log") | Out-Null
& $nssm set $ServiceName Start            SERVICE_AUTO_START | Out-Null
& $nssm set $ServiceName AppRestartDelay  5000               | Out-Null

# Leállításkor Ctrl+C-t küld, és ad 8 másodpercet a tiszta zárásra (soros port,
# adatbázis). Enélkül az NSSM 1,5 másodperc után erőszakosan lő.
& $nssm set $ServiceName AppStopMethodSkip    0    | Out-Null
& $nssm set $ServiceName AppStopMethodConsole 8000 | Out-Null
& $nssm set $ServiceName DisplayName      "Iroda RFID gateway" | Out-Null
& $nssm set $ServiceName Description      "RFID bélyegzések és környezeti mérések továbbítása a szerverre." | Out-Null

# Az NSSM saját naplóforgatása: 10 MB felett új fájlt kezd.
& $nssm set $ServiceName AppRotateFiles   1        | Out-Null
& $nssm set $ServiceName AppRotateOnline  1        | Out-Null
& $nssm set $ServiceName AppRotateBytes   10485760 | Out-Null
Write-Ok "Beállítások elmentve."

Write-Step "Indítás"
& $nssm start $ServiceName | Out-Null
Start-Sleep -Seconds 3
$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($service -and $service.Status -eq "Running") {
    Write-Ok "A szolgáltatás fut."
} else {
    Write-Warn2 "A szolgáltatás nem indult el. Nézd meg: $LogDir\err.log"
}

# --------------------------------------------------------------------------
Write-Host ""
Write-Host "======================================================================"
Write-Host " Kész."
Write-Host "======================================================================"
Write-Host " Napló       : $LogDir\gateway.log"
Write-Host " Diagnosztika: $VenvPython check.py"
Write-Host " Újraindítás : nssm restart $ServiceName"
Write-Host " Leállítás   : nssm stop $ServiceName"
Write-Host ""
Write-Host " Ne felejtsd el a README-windows.md ,,Windows beállítások'' fejezetét:"
Write-Host " USB selective suspend, powercfg /h off, alvás soha, fix COM port szám,"
Write-Host " Defender kizárás, BIOS Restore on AC Power Loss."
Write-Host "======================================================================"
