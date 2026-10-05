# Runs the Natiqo collection end to end against a disposable SQLite database.
#
# Two things make this more than "newman run":
#
# 1. It owns the server. Starting uvicorn here, waiting for /ready, and stopping it
#    afterwards means a run cannot silently talk to whatever else happens to be
#    listening on the port.
# 2. Password reset is a second pass. The token only ever leaves the system through
#    the mail provider, so there is no endpoint that hands it back. Rather than
#    adding a debug endpoint, mint_reset_token.py calls the real forgot_password use
#    case and picks the token up from the delivery boundary.

param(
    [string]$BaseUrl = "http://127.0.0.1:8099",
    [string]$Email = "",
    [switch]$KeepServer
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$env:APP_ENV = "development"
$env:AI_ENABLED = "false"
$env:DATABASE_URL = "sqlite+aiosqlite:///./postman/pm_test.db"

# A fresh account per run, not a fresh database file: the server holds the SQLite
# handle open, so deleting the file underneath it yields a run that passes against a
# database nothing is actually reading. A unique email gets the same clean state
# without fighting the process for the file.
if (-not $Email) {
    $Email = "pm.{0}@example.com" -f (Get-Date -Format "yyyyMMddHHmmss")
}
Write-Host "==> account: $Email"

$port = ([uri]$BaseUrl).Port
$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($listener -and -not $KeepServer) {
    throw "port $port is already in use by PID $($listener[0].OwningProcess); stop it or pass -KeepServer to reuse it"
}

Push-Location $root
$newman = "./postman/node_modules/.bin/newman.cmd"
$collection = "./postman/Natiqo.postman_collection.json"
$environment = "./postman/dev.postman_environment.json"
$tokenFile = "./postman/reset_token.json"
$server = $null

function Stop-Server {
    if ($script:server -and -not $KeepServer) {
        Write-Host "==> stopping server"
        # .Id, not the object: Start-Process -PassThru gives a Process here.
        Stop-Process -Id $script:server.Id -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-Newman([string[]]$Folders, [hashtable]$Vars) {
    $newmanArgs = @("run", $collection, "-e", $environment, "--reporters", "cli", "--bail", "--color", "off")
    foreach ($f in $Folders) { $newmanArgs += @("--folder", $f) }
    # --env-var, not --global-var: the collection declares no variables of its own, so
    # the environment is what resolves, which is what makes a human run in Postman and
    # this script agree.
    foreach ($k in $Vars.Keys) { $newmanArgs += @("--env-var", "$k=$($Vars[$k])") }
    Write-Host "==> newman: $($Folders -join ', ')"
    & $newman @newmanArgs | Out-Host
    return $LASTEXITCODE
}

try {
    # Migrate before the server starts. Health and the error envelope pass against an
    # empty database, so a missing migration surfaces much later as a 503 on the first
    # write instead of a clear failure here.
    Write-Host "==> migrating"
    uv run alembic upgrade head | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "alembic upgrade failed" }

    if (-not $listener) {
        Write-Host "==> starting server on $BaseUrl"
        # The venv's uvicorn directly, not `uv run uvicorn`: uv spawns uvicorn as a
        # child, so stopping uv leaves the real server holding the port and the next run
        # dies on EADDRINUSE.
        $script:server = Start-Process -FilePath "$root\.venv\Scripts\uvicorn.exe" `
            -ArgumentList @("app.main:app", "--host", "127.0.0.1", "--port", $port, "--log-level", "warning") `
            -WorkingDirectory $root -WindowStyle Hidden `
            -RedirectStandardOutput "$root\postman\server.log" `
            -RedirectStandardError "$root\postman\server.err.log" `
            -PassThru

        # Poll rather than sleep-and-hope, and fail loudly: a connection-refused report
        # from newman says nothing about why the server did not come up.
        $up = $false
        foreach ($attempt in 1..30) {
            Start-Sleep -Seconds 1
            try {
                $probe = Invoke-WebRequest -Uri "$BaseUrl/ready" -UseBasicParsing -TimeoutSec 5
                if ($probe.StatusCode -eq 200) { $up = $true; break }
            } catch { }
        }
        if (-not $up) {
            Get-Content "$root\postman\server.err.log" -Tail 20 | Write-Host
            throw "server never became ready; see postman/server.err.log"
        }
        Write-Host "==> server ready"
    }

    $code = Invoke-Newman -Folders @("Health", "Auth", "Me") -Vars @{ email = $Email; baseUrl = $BaseUrl }
    if ($code -ne 0) { exit $code }

    Write-Host "==> minting reset token (stands in for the mail provider)"
    Remove-Item -Force $tokenFile -ErrorAction SilentlyContinue
    uv run python ./postman/mint_reset_token.py $Email
    if ($LASTEXITCODE -ne 0) { throw "could not mint a reset token for $Email" }

    $token = (Get-Content $tokenFile -Raw | ConvertFrom-Json).token
    $code = Invoke-Newman -Folders @("Password reset (needs a real token)") -Vars @{ resetToken = $token; email = $Email; baseUrl = $BaseUrl }
    if ($code -ne 0) { exit $code }

    Write-Host "`n==> all endpoints passed"
    exit 0
}
finally {
    Stop-Server
    Pop-Location
}
