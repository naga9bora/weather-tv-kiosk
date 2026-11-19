package tv.weather.kiosk;

import android.app.Activity;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.widget.TextView;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/**
 * The weather kiosk screen: live clock with seconds, the MET forecast for the configured district,
 * the active warning and the JAS air-pollutant reading. Weather + JAS come from the native readers
 * (Weather, Haze); everything else is view.
 */
public class MainActivity extends Activity {

    private static final String TAG = "TvHome.Main";
    public static final String MET_LOCATION = "Kuala Lumpur";
    public static final String PLACE_NAME = "Kuala Lumpur";
    private static final String HAZE_STATION = "Kuala Lumpur";
    private static final long REFRESH_MS = 10L * 60L * 1000L;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable tick = new Runnable() {
        @Override public void run() {
            renderClock();
            handler.postDelayed(this, 1000L);
        }
    };
    private final Runnable scheduler = new Runnable() {
        @Override public void run() {
            refresh();
            handler.postDelayed(this, REFRESH_MS);
        }
    };

    private TextView clock, date, temp, cond, feels, minT, maxT, when, morning, afternoon, night, warn, haze;

    @Override
    protected void onCreate(android.os.Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        clock = (TextView) findViewById(R.id.clock);
        date = (TextView) findViewById(R.id.date);
        temp = (TextView) findViewById(R.id.temp);
        cond = (TextView) findViewById(R.id.cond);
        feels = (TextView) findViewById(R.id.feels);
        minT = (TextView) findViewById(R.id.min_temp);
        maxT = (TextView) findViewById(R.id.max_temp);
        when = (TextView) findViewById(R.id.when);
        morning = (TextView) findViewById(R.id.morning);
        afternoon = (TextView) findViewById(R.id.afternoon);
        night = (TextView) findViewById(R.id.night);
        warn = (TextView) findViewById(R.id.warn);
        haze = (TextView) findViewById(R.id.haze);
        handler.post(tick);
        handler.post(scheduler);
        refresh();
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacks(tick);
        handler.removeCallbacks(scheduler);
        super.onDestroy();
    }

    private void refresh() {
        new Thread(new Runnable() {
            @Override
            public void run() {
                final Weather.Today t = Weather.today(MainActivity.this);
                final List<String> places = Weather.places(MET_LOCATION, PLACE_NAME);
                final Weather.Alert alert = Weather.active(MainActivity.this, places);
                final Haze.Reading hr = Haze.reading(MainActivity.this, HAZE_STATION);
                final Weather.Today ft = t;
                final Weather.Alert fa = alert;
                final Haze.Reading fr = hr;
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        render(ft, fa, fr);
                    }
                });
            }
        }).start();
    }

    private void render(final Weather.Today t, final Weather.Alert alert, final Haze.Reading hr) {
        if (t == null) {
            temp.setText("--°");
            cond.setText("No forecast");
            feels.setText("");
            minT.setText("");
            maxT.setText("");
            when.setText("");
            morning.setText("");
            afternoon.setText("");
            night.setText("");
        } else {
            temp.setText(String.format(Locale.US, "%.0f°", t.max));
            cond.setText(t.summary);
            feels.setText(String.format(Locale.US, "Min %.0f° · Max %.0f°", t.min, t.max));
            minT.setText(String.format(Locale.US, "min %.0f°", t.min));
            maxT.setText(String.format(Locale.US, "max %.0f°", t.max));
            when.setText(t.when == null ? "" : t.when);
            morning.setText(t.morning == null ? "" : t.morning);
            afternoon.setText(t.afternoon == null ? "" : t.afternoon);
            night.setText(t.night == null ? "" : t.night);
        }

        if (alert == null) {
            warn.setVisibility(TextView.GONE);
        } else {
            warn.setVisibility(TextView.VISIBLE);
            warn.setText(alert.title);
        }

        if (hr == null) {
            haze.setVisibility(TextView.GONE);
        } else {
            int band = Haze.band(hr.api);
            String bandName = Haze.BANDS[band];
            haze.setVisibility(TextView.VISIBLE);
            haze.setText(HAZE_STATION + " · IPU " + hr.api + " · " + bandName
                    + (hr.nearest ? " (nearest)" : ""));
        }
    }

    private void renderClock() {
        Date now = new Date();
        clock.setText(new SimpleDateFormat("HH:mm:ss", Locale.US).format(now));
        date.setText(new SimpleDateFormat("EEE, d MMM yyyy", Locale.US).format(now));
    }
}
