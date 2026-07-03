$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Ensure log directory exists
$logDir = "output\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

$logFile = "$logDir\daily_$(Get-Date -Format 'yyyyMMdd').log"
$dateTag = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

# Run daily update, capture exit code
$python = "C:\Users\Jayron\AppData\Local\Programs\Python\Python314\python.exe"
$output = & $python -c "from tools.auto_trader import daily_update; print(daily_update())" 2>&1
$exitCode = $LASTEXITCODE

# Always write log — even on failure
@"
=== 大A日更 $dateTag ===
Exit code: $exitCode

$output
"@ | Out-File -FilePath $logFile -Encoding utf8

# Non-zero exit = visible failure in Task Scheduler
if ($exitCode -ne 0) { exit 1 }
