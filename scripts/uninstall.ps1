# Remove the kiosk app and its pushed page from the TV.
# The device address is never stored here: pass -Device or set KIOSK_DEVICE.
param(
    [string]$Adb = "C:\platform-tools\adb.exe",
    [string]$Device = $env:KIOSK_DEVICE
)

if (-not $Device) { throw "no device: pass -Device <ip:5555> or set `$env:KIOSK_DEVICE" }

& $Adb connect $Device | Out-Null
& $Adb -s $Device shell am force-stop tv.weather.kiosk
& $Adb -s $Device uninstall tv.weather.kiosk
& $Adb -s $Device shell "rm -rf /sdcard/Android/data/tv.weather.kiosk/files/index.html"
Write-Host "removed from $Device" -ForegroundColor Yellow
