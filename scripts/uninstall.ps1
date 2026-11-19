# Remove the kiosk app and its pushed page from the TV.
param(
    [string]$Adb = "C:\platform-tools\adb.exe",
    [string]$Device = "192.0.2.10:5555"
)

& $Adb connect $Device | Out-Null
& $Adb -s $Device shell am force-stop tv.weather.kiosk
& $Adb -s $Device uninstall tv.weather.kiosk
& $Adb -s $Device shell "rm -rf /sdcard/Android/data/tv.weather.kiosk/files/index.html"
Write-Host "removed from $Device" -ForegroundColor Yellow
