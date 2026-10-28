# One-time setup: opens Chrome with the Newsroom's own profile (F:\videos\newsroom\chrome_profile). Sign in to Google, open
# the "Newsroom Inbox" doc once and google.com/alerts once, then CLOSE Chrome. The Newsroom reuses this profile for the doc
# bridge and Google Alerts. (It is separate from the Flow / Gemini profile, so it never collides with a running render.)
$ErrorActionPreference = 'Stop'
$Profile = 'F:\videos\newsroom\chrome_profile'
New-Item -ItemType Directory -Force $Profile | Out-Null
$Chrome = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
            "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Chrome) { throw 'Google Chrome not found.' }
$cfg = Get-Content (Join-Path (Split-Path $PSScriptRoot -Parent) 'config.json') -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json
$doc = if ($cfg -and $cfg.doc_id) { "https://docs.google.com/document/d/$($cfg.doc_id)/edit" } else { 'https://docs.google.com' }
Start-Process -FilePath $Chrome -ArgumentList "--user-data-dir=`"$Profile`"", 'https://accounts.google.com', $doc, 'https://www.google.com/alerts'
Write-Output "Chrome opened with $Profile. Sign in, check the doc and Alerts pages load, then close Chrome."
