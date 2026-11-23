<#
    EDLIZ Assistant - one-click setup for Windows.

    Run it by double-clicking Setup.cmd in the folder above this one, which
    launches this script with the execution policy bypassed.

    What it does:
      1. finds (or offers to install) Python 3.11+
      2. creates .venv and installs the app into it
      3. writes a .env pointing at a model provider - either an API you
         already hold, or a local Ollama install it can set up for you
      4. builds the searchable index from the OCR text in data/text/
      5. starts the server and opens the browser

    It is written for Windows PowerShell 5.1, which ships with Windows, so no
    extra PowerShell modules are needed. Deliberately plain: no fancy syntax,
    and every step reports what it is doing and stops with a readable message
    if it cannot continue.
#>

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- helpers ---

function Say($message) { Write-Host $message }
function Step($n, $message) { Write-Host ""; Write-Host "[$n] $message" -ForegroundColor Cyan }
function Ok($message)   { Write-Host "    $message" -ForegroundColor Green }
function Warn($message) { Write-Host "    $message" -ForegroundColor Yellow }

function Pause-And-Exit($code) {
    Write-Host ""
    Read-Host "Press Enter to close this window"
    exit $code
}

$repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $repo

Write-Host ""
Write-Host "  EDLIZ Assistant - setup" -ForegroundColor White
Write-Host "  Zimbabwe Essential Medicines List, digitised" -ForegroundColor DarkGray
Write-Host ""

# ---------------------------------------------------------------- python ----

Step 1 "Looking for Python"

$python = $null
$candidates = @(
    @{ Exe = 'py';    Args = @('-3') },
    @{ Exe = 'python'; Args = @() }
)

foreach ($c in $candidates) {
    $found = Get-Command $c.Exe -ErrorAction SilentlyContinue
    if (-not $found) { continue }
    try {
        $version = & $c.Exe @($c.Args + @('-c', 'import sys; print("%d.%d" % sys.version_info[:2])')) 2>$null
        if (-not $version) { continue }
        $parts = $version.ToString().Trim().Split('.')
        $major = [int]$parts[0]
        $minor = [int]$parts[1]
        if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 11)) {
            $python = $c
            break
        }
        Warn "Python $version is too old (need 3.11 or newer)"
    } catch {
        continue
    }
}

if (-not $python) {
    Warn "Python 3.11 or newer was not found."
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        $answer = Read-Host "    Install Python automatically now? (y/N)"
        if ($answer -match '^(y|Y)') {
            Say "    Installing Python 3.12 - this takes a few minutes..."
            & winget install --id Python.Python.3.12 -e --source winget `
                --accept-source-agreements --accept-package-agreements
            Warn "Python was installed. Close this window and double-click Setup.cmd again."
        } else {
            Start-Process "https://www.python.org/downloads/"
            Warn "Install Python from the page that just opened (tick 'Add python.exe to PATH'),"
            Warn "then close this window and double-click Setup.cmd again."
        }
    } else {
        Start-Process "https://www.python.org/downloads/"
        Warn "Install Python from the page that just opened (tick 'Add python.exe to PATH'),"
        Warn "then close this window and double-click Setup.cmd again."
    }
    Pause-And-Exit 1
}
Ok "Python found"

# ------------------------------------------------------------------ venv ----

Step 2 "Setting up the Python environment"

# Windows puts the interpreter in .venv\Scripts; other layouts use .venv/bin.
# Accept either so the script also works if the folder was prepared elsewhere.
$venvPython = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    $nixPython = Join-Path $repo ".venv/bin/python"
    if (Test-Path $nixPython) { $venvPython = $nixPython }
}

if (Test-Path $venvPython) {
    Ok "Environment already exists"
} else {
    & $python.Exe @($python.Args + @('-m', 'venv', '.venv'))
    if (-not (Test-Path $venvPython)) {
        $nixPython = Join-Path $repo ".venv/bin/python"
        if (Test-Path $nixPython) { $venvPython = $nixPython }
    }
    if (-not (Test-Path $venvPython)) { throw "Could not create .venv" }
    Ok "Environment created"
}

Step 3 "Installing the app (this downloads a few packages)"

& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -e .
if ($LASTEXITCODE -ne 0) { throw "Installing the app failed" }
Ok "Installed"

# ------------------------------------------------------------------ model ---

$envPath = Join-Path $repo ".env"

if (Test-Path $envPath) {
    Step 4 "Using the existing .env file"
    Ok "Found .env - skipping model setup"
} else {
    Step 4 "Choosing the AI model"

    Say "    The app needs a language model to write its answers."
    Say "    You can either use an API key you already have, or run a"
    Say "    free model on this laptop (about 1.5 GB to download)."
    Say ""
    $choice = Read-Host "    Do you have an API key for an AI service? (y/N, Enter means No)"

    if ($choice -match '^(y|Y)') {
        $baseUrl = Read-Host "    Base URL (Enter for https://caimex.econetai.co.zw/v1)"
        if (-not $baseUrl) { $baseUrl = "https://caimex.econetai.co.zw/v1" }
        $apiKey = Read-Host "    API key"
        $chatModel = Read-Host "    Chat model (Enter for deepseek-v4-flash)"
        if (-not $chatModel) { $chatModel = "deepseek-v4-flash" }

        $lines = @(
            "CAIMEX_BASE_URL=$baseUrl",
            "CAIMEX_API_KEY=$apiKey",
            "EDLIZ_CHAT_MODEL=$chatModel",
            "EDLIZ_EMBED_MODEL=bge-m3",
            "EDLIZ_EDITION=9th edition (2025)",
            "EDLIZ_TEXT_DIR=data/text/edliz-2025",
            "EDLIZ_WEB_SEARCH=1"
        )
        Set-Content -Path $envPath -Value $lines -Encoding ASCII
        Ok "Saved .env"
    } else {
        Say ""
        Say "    Setting up Ollama, a free model that runs on this laptop."

        # A command can exist and still not run: uninstalling Ollama leaves a
        # dangling link behind, and Get-Command reports it as an application
        # without checking the target. So a candidate must answer --version.
        $ollamaCmd = $null
        $candidates = @()
        $onPath = Get-Command ollama -ErrorAction SilentlyContinue
        if ($onPath) { $candidates += $onPath.Source }
        $candidates += (Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe")

        foreach ($candidate in $candidates) {
            if (-not $candidate) { continue }
            if (-not (Test-Path $candidate)) { continue }
            try {
                & $candidate --version *> $null
                if ($LASTEXITCODE -eq 0) { $ollamaCmd = $candidate; break }
            } catch {
                continue
            }
        }

        if (-not $ollamaCmd) {
            Warn "Ollama is not installed."
            $answer = Read-Host "    Open the Ollama download page now? (Y/n)"
            if ($answer -notmatch '^(n|N)') {
                Start-Process "https://ollama.com/download/windows"
            }
            Warn "Install Ollama, then close this window and double-click Setup.cmd again."
            Pause-And-Exit 1
        }

        Say ""
        Say "    Downloading the embedding model (about 1.2 GB)..."
        & $ollamaCmd pull bge-m3
        if ($LASTEXITCODE -ne 0) {
            Warn "Could not download bge-m3. Open Ollama from the Start menu, then run Setup again."
            Pause-And-Exit 1
        }

        Say ""
        Say "    Downloading the chat model (about 1.4 GB)..."
        & $ollamaCmd pull qwen3:1.7b
        if ($LASTEXITCODE -ne 0) {
            Warn "Could not download qwen3:1.7b. Open Ollama from the Start menu, then run Setup again."
            Pause-And-Exit 1
        }

        Say ""
        Say "    Checking that Ollama is running..."
        $apiUp = $false
        for ($i = 0; $i -lt 10; $i++) {
            try {
                $null = Invoke-WebRequest -Uri "http://127.0.0.1:11434/api/tags" -UseBasicParsing -TimeoutSec 2
                $apiUp = $true
                break
            } catch {
                Start-Sleep -Seconds 1
            }
        }
        if (-not $apiUp) {
            Warn "Ollama is installed but not running."
            Warn "Open Ollama from the Start menu (it sits in the tray), then double-click Setup.cmd again."
            Pause-And-Exit 1
        }
        Ok "Ollama is answering"

        $lines = @(
            "CAIMEX_BASE_URL=http://127.0.0.1:11434/v1",
            "CAIMEX_API_KEY=ollama",
            "EDLIZ_CHAT_MODEL=qwen3:1.7b",
            "EDLIZ_EMBED_MODEL=bge-m3",
            "EDLIZ_EDITION=9th edition (2025)",
            "EDLIZ_TEXT_DIR=data/text/edliz-2025",
            "EDLIZ_WEB_SEARCH=1"
        )
        Set-Content -Path $envPath -Value $lines -Encoding ASCII
        Ok "Saved .env for local use"
    }
}

# ------------------------------------------------------------------ index ---

Step 5 "Building the search index (about 30 seconds)"

& $venvPython -m app.ingest --text data/text/edliz-2025 --edition "9th edition (2025)"
if ($LASTEXITCODE -ne 0) {
    Warn "The index could not be built. Check that your API key or Ollama is working,"
    Warn "then run Setup.cmd again."
    Pause-And-Exit 1
}
Ok "Index built"

# ------------------------------------------------------------------ start ---

Step 6 "Starting the app"

# 8077 by default; EDLIZ_SETUP_PORT exists so a second copy can run alongside
$port = if ($env:EDLIZ_SETUP_PORT) { [int]$env:EDLIZ_SETUP_PORT } else { 8077 }
$url = "http://127.0.0.1:$port"

# Left visible on purpose: the window shows the server log, and closing it
# stops the app. (Also keeps this line working outside Windows.)
Start-Process -FilePath $venvPython `
    -ArgumentList @('-m', 'uvicorn', 'app.server:app', '--port', "$port") `
    -WorkingDirectory $repo

Say "    Waiting for the server..."
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $response = Invoke-WebRequest -Uri "$url/api/status" -UseBasicParsing -TimeoutSec 2
        if ($response.StatusCode -eq 200) { $ready = $true; break }
    } catch {
        # not up yet
    }
}

if ($ready) {
    Ok "Running at $url"
    if (-not $env:EDLIZ_SETUP_NO_OPEN) { Start-Process $url }
} else {
    Warn "The server did not answer in time. Try running Setup.cmd again."
}

Write-Host ""
Write-Host "  Tips" -ForegroundColor White
Write-Host "   - 'The list' tab: search a medicine or a condition, press / to jump there"
Write-Host "   - Click any page number to see the text the answer came from"
Write-Host "   - Close the small server window to stop the app"
Write-Host ""

Pause-And-Exit 0
