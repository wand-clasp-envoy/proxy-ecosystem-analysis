# Refresh the locally-verified "lite" subscription and publish it.
#
# Why this exists: the GitHub-hosted feed is verified from Azure. From a
# censored network most of those endpoints are unreachable, so the published
# file looks "unusable". This script re-verifies from THIS machine.
#
# Run manually:
#   powershell -NoProfile -File tools\local-refresh.ps1
# Schedule every 3 hours:
#   schtasks /Create /TN "proxy-lite-refresh" /SC HOURLY /MO 3 /TR ^
#     "powershell -NoProfile -ExecutionPolicy Bypass -File D:\github\proxy-ecosystem-analysis\tools\local-refresh.ps1"
#
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force -Path (Join-Path $root "data") | Out-Null
Start-Transcript -Path (Join-Path $root "data\local-refresh.log") -Append | Out-Null

$gist  = "b55b2118b45dbfc87ed8e2690e6ad6a3"
$full  = "https://gist.githubusercontent.com/wand-clasp-envoy/$gist/raw/organised.yaml"

if (-not $env:MIHOMO_BIN) {
    $env:MIHOMO_BIN = Join-Path $env:TEMP "bin\mihomo-windows-amd64-compatible.exe"
}
if (-not (Test-Path $env:MIHOMO_BIN)) {
    throw "mihomo not found at $env:MIHOMO_BIN - download it from MetaCubeX/mihomo releases and set MIHOMO_BIN"
}

# a PAT with `gist` scope is required to update the gist
$env:GH_TOKEN = (gh auth token)
if (-not $env:GH_TOKEN) { throw "gh is not logged in (gh auth login)" }

$started = Get-Date
Write-Host "[$(Get-Date -Format HH:mm:ss)] probing $full from this machine ..."
python -u tools\local_lite.py --url $full --keep 200 --probe-cap 600 --speed-top 30
if ($LASTEXITCODE -ne 0) { throw "local_lite.py failed" }

# publish only if the result is non-trivial (publish.py enforces --min-proxies)
python -u tools\publish.py --gist $gist --file data\local-lite.yaml --min-proxies 20
if ($LASTEXITCODE -ne 0) { throw "publish.py failed" }

Write-Host ("[$(Get-Date -Format HH:mm:ss)] done in {0:N0}s -> local-lite.yaml" -f ((Get-Date) - $started).TotalSeconds)
Write-Host "subscribe: https://gist.githubusercontent.com/wand-clasp-envoy/$gist/raw/local-lite.yaml"
Stop-Transcript | Out-Null
