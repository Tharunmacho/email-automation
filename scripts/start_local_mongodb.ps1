$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$mongoRoot = Join-Path $projectRoot "data\mongodb-portable"
$mongod = Join-Path $mongoRoot "server\MongoDB\Server\8.3\bin\mongod.exe"
$dbPath = Join-Path $mongoRoot "db"
$logPath = Join-Path $mongoRoot "mongod.log"

$listener = Get-NetTCPConnection -LocalPort 27017 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    Write-Output "MongoDB is already listening on 127.0.0.1:27017."
    exit 0
}

if (-not (Test-Path -LiteralPath $mongod -PathType Leaf)) {
    throw "Portable MongoDB was not found at $mongod"
}

New-Item -ItemType Directory -Path $dbPath -Force | Out-Null
$process = Start-Process -FilePath $mongod -ArgumentList @(
    "--dbpath", $dbPath,
    "--bind_ip", "127.0.0.1",
    "--port", "27017",
    "--logpath", $logPath,
    "--logappend"
) -WindowStyle Hidden -PassThru

for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Milliseconds 250
    if (Get-NetTCPConnection -LocalPort 27017 -State Listen -ErrorAction SilentlyContinue) {
        Write-Output "MongoDB started on 127.0.0.1:27017 (PID $($process.Id))."
        exit 0
    }
    if (-not (Get-Process -Id $process.Id -ErrorAction SilentlyContinue)) {
        break
    }
}

if (Test-Path -LiteralPath $logPath) {
    Get-Content -LiteralPath $logPath -Tail 20
}
throw "MongoDB did not start. See $logPath"
