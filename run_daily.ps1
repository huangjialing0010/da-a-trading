Set-Location "D:\大A"
$logDir = "output\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory $logDir -Force | Out-Null }
$logFile = "$logDir\daily_$(Get-Date -Format 'yyyyMMdd').log"
python -c "from tools.auto_trader import daily_update; print(daily_update())" 2>&1 | Out-File -FilePath $logFile -Encoding utf8
