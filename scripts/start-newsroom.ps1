# Starts the Newsroom on http://127.0.0.1:42040 (hidden), using the Video Studio fork's Python (Flask, requests, Playwright).
# Output: F:\videos\newsroom\newsroom.stdout.log / .stderr.log; PID in F:\videos\newsroom\newsroom.pid
$ErrorActionPreference = 'Stop'
$Root = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$Py = 'F:\code\video-studio-test\app\env\Scripts\python.exe'
if (-not (Test-Path $Py)) { throw "Python not found at $Py" }
$Data = 'F:\videos\newsroom'
New-Item -ItemType Directory -Force $Data | Out-Null
if (Get-NetTCPConnection -LocalPort 42040 -State Listen -ErrorAction SilentlyContinue) { throw 'Port 42040 is already in use.' }
$env:PORT = '42040'
$p = Start-Process -FilePath $Py -ArgumentList '-u', 'app.py' -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
     -RedirectStandardOutput "$Data\newsroom.stdout.log" -RedirectStandardError "$Data\newsroom.stderr.log"
$p.Id | Set-Content -LiteralPath "$Data\newsroom.pid"
Write-Output "Newsroom started: PID $($p.Id), http://127.0.0.1:42040"
