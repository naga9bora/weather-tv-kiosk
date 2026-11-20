# Push the freshly built page to the TV and reload the kiosk. No APK rebuild.
# The device address is never stored here: pass -Device or set KIOSK_DEVICE.
param(
    [string]$Adb = "C:\platform-tools\adb.exe",
    [string]$Device = $env:KIOSK_DEVICE,
    [string]$Page = (Join-Path $PSScriptRoot "..\dist\index.html")
)

$ErrorActionPreference = "Stop"

if (-not $Device) { throw "no device: pass -Device <ip:5555> or set `$env:KIOSK_DEVICE" }
if (-not (Test-Path $Page)) { throw "$Page missing - run: python tools\build_html.py" }

& $Adb connect $Device | Out-Null
$state = (& $Adb -s $Device get-state) 2>$null
if ($state -ne "device") { throw "device $Device is not available (state=$state)" }

# Sign the page with the same key the installed APK carries. MainActivity
# refuses an external page without a matching sidecar, so another app writing
# to shared storage cannot inject JavaScript into the kiosk origin.
$keyFile = Join-Path (Split-Path $PSScriptRoot -Parent) "keys.local.json"
if (-not (Test-Path $keyFile)) {
    throw "keys.local.json missing - run python tools\build_apk.py once to generate the page key"
}
$cfg = ([IO.File]::ReadAllText($keyFile, [Text.Encoding]::UTF8) -replace '^\uFEFF', '') | ConvertFrom-Json
$pageKey = [string]$cfg.page
if (-not $pageKey) {
    throw "keys.local.json has no page key - run python tools\build_apk.py once to generate it"
}

$hmac = New-Object System.Security.Cryptography.HMACSHA256
try {
    $hmac.Key = [Text.Encoding]::UTF8.GetBytes($pageKey)
    $hash = $hmac.ComputeHash([IO.File]::ReadAllBytes($Page))
} finally {
    $hmac.Dispose()
}
$sig = -join ($hash | ForEach-Object { $_.ToString("x2") })
$sigFile = Join-Path $env:TEMP ("kiosk-page-" + [guid]::NewGuid().ToString("N") + ".hmac")
[IO.File]::WriteAllText($sigFile, $sig, (New-Object Text.UTF8Encoding($false)))

$remote = "/sdcard/Android/data/tv.weather.kiosk/files"
& $Adb -s $Device shell "mkdir -p $remote"

# Keep the page that is currently running as the known-good fallback. If the new
# one never boots, MainActivity retries index.html.bak and then the packaged
# asset, so a bad push costs about 90 s instead of a dead kiosk.
& $Adb -s $Device shell "cd $remote && if [ -f index.html ]; then cp -f index.html index.html.bak; fi && if [ -f index.html.hmac ]; then cp -f index.html.hmac index.html.bak.hmac; fi"

try {
    & $Adb -s $Device push $Page "$remote/index.html"
    & $Adb -s $Device push $sigFile "$remote/index.html.hmac"
} finally {
    Remove-Item -Force $sigFile -ErrorAction SilentlyContinue
}

# A truncated push would be exactly the failure the fallback exists to hide, so
# verify the byte counts before restarting the app.
$localLen = (Get-Item $Page).Length
$remoteLen = ((& $Adb -s $Device shell "stat -c %s $remote/index.html") | Out-String).Trim()
if ($remoteLen -ne "$localLen") {
    throw "push incomplete: local=$localLen bytes, device=$remoteLen bytes"
}
$remoteSig = ((& $Adb -s $Device shell "cat $remote/index.html.hmac") | Out-String).Trim()
if ($remoteSig -ne $sig) {
    throw "signature sidecar did not arrive intact on the device"
}

# Restart so the intercept picks up the new file.
& $Adb -s $Device shell am force-stop tv.weather.kiosk
& $Adb -s $Device shell am start -n tv.weather.kiosk/.MainActivity | Out-Null

Write-Host "page updated on $Device ($localLen bytes)" -ForegroundColor Green
