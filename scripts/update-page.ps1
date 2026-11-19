# Push the freshly built page to the TV and reload the kiosk. No APK rebuild.
param(
    [string]$Adb = "C:\platform-tools\adb.exe",
    [string]$Device = "192.0.2.10:5555",
    [string]$Page = (Join-Path $PSScriptRoot "..\dist\index.html")
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $Page)) { throw "$Page missing - run: python tools\build_html.py" }

& $Adb connect $Device | Out-Null
$state = (& $Adb -s $Device get-state) 2>$null
if ($state -ne "device") { throw "device $Device is not available (state=$state)" }

$remote = "/sdcard/Android/data/tv.weather.kiosk/files"
& $Adb -s $Device shell "mkdir -p $remote"

# Keep the page that is currently running as the known-good fallback. If the new
# one never boots, MainActivity retries index.html.bak and then the packaged
# asset, so a bad push costs about 90 s instead of a dead kiosk.
& $Adb -s $Device shell "cd $remote && if [ -f index.html ]; then cp -f index.html index.html.bak; fi"

& $Adb -s $Device push $Page "$remote/index.html"

# A truncated push would be exactly the failure the fallback exists to hide, so
# verify the byte count before restarting the app.
$localLen = (Get-Item $Page).Length
$remoteLen = ((& $Adb -s $Device shell "stat -c %s $remote/index.html") | Out-String).Trim()
if ($remoteLen -ne "$localLen") {
    throw "push incomplete: local=$localLen bytes, device=$remoteLen bytes"
}

# Restart so the intercept picks up the new file.
& $Adb -s $Device shell am force-stop tv.weather.kiosk
& $Adb -s $Device shell am start -n tv.weather.kiosk/.MainActivity | Out-Null

Write-Host "page updated on $Device ($localLen bytes)" -ForegroundColor Green
