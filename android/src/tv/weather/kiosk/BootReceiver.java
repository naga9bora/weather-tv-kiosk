package tv.weather.kiosk;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * Brings the kiosk back up after a reboot or an app update.
 *
 * Android 10 blocks activity starts from the background, so this only works once
 * the app holds the SYSTEM_ALERT_WINDOW app-op. scripts/install.ps1 sets that.
 */
public class BootReceiver extends BroadcastReceiver {

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent == null ? null : intent.getAction();
        if (!Intent.ACTION_BOOT_COMPLETED.equals(action)
                && !Intent.ACTION_LOCKED_BOOT_COMPLETED.equals(action)
                && !Intent.ACTION_MY_PACKAGE_REPLACED.equals(action)) {
            return;
        }
        // Keep Wi-Fi awake: the radio's power-save mode drops the adb TCP
        // connection even when wifi_sleep_policy=2. Best-effort; needs root.
        try {
            Runtime.getRuntime().exec(new String[]{"su", "0", "sh", "-c",
                    "iw dev wlan0 set power_save off"});
        } catch (Exception ignored) { }
        Intent launch = new Intent(context, MainActivity.class);
        launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                | Intent.FLAG_ACTIVITY_CLEAR_TOP
                | Intent.FLAG_ACTIVITY_EXCLUDE_FROM_RECENTS);
        try {
            context.startActivity(launch);
        } catch (Exception ignored) {
            // If the platform refuses, the install script's fallback service starts it.
        }
    }
}
