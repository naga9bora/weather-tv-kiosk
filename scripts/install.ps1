# Install / reinstall the kiosk on the TV and make it autostart.
param(
    [string]$Adb = "C:\platform-tools\adb.exe",
    [string]$Device = "192.0.2.10:5555",
    [string]$Apk = (Join-Path $PSScriptRoot "..\dist\weather-kiosk.apk")
)

$ErrorActionPreference = "Stop"

& $Adb connect $Device | Out-Null
$state = (& $Adb -s $Device get-state) 2>$null
if ($state -ne "device") { throw "device $Device is not available (state=$state)" }

& $Adb -s $Device install -r $Apk
if ($LASTEXITCODE -ne 0) { throw "install failed" }

# Android 10 blocks activity starts from BOOT_COMPLETED unless this app-op is held.
& $Adb -s $Device shell appops set tv.weather.kiosk SYSTEM_ALERT_WINDOW allow

& $Adb -s $Device shell am force-stop tv.weather.kiosk
& $Adb -s $Device shell am start -n tv.weather.kiosk/.MainActivity

Write-Host "installed and launched on $Device" -ForegroundColor Green
