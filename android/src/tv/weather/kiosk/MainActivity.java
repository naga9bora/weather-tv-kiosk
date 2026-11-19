package tv.weather.kiosk;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Log;
import android.view.KeyEvent;
import android.view.View;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.KeyStore;
import java.security.cert.CertificateFactory;
import java.security.cert.X509Certificate;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.TimeZone;

import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManager;
import javax.net.ssl.TrustManagerFactory;
import javax.net.ssl.X509TrustManager;

/**
 * Fullscreen kiosk that renders the weather dashboard.
 *
 * The page is served from a fake origin (http://weather.local/) so ordinary
 * cross-origin fetch() calls to api.data.gov.my / Open-Meteo work while the
 * document itself never touches the network. Content is read from, in order:
 *
 *   1. <external-files-dir>/index.html   (adb push for instant updates)
 *   2. the packaged asset                (survives a wipe of external storage)
 */
public class MainActivity extends Activity {

    private static final String TAG = "WeatherKiosk";
    private static final String HOST = "weather.local";
    private static final String ORIGIN = "http://" + HOST + "/";
    private static final long RELOAD_MS = 6L * 60 * 60 * 1000;   // 6 hours
    private static final long BEAT_CHECK_MS = 60L * 1000;        // check every minute
    private static final long BEAT_STALE_MS = 4L * 60 * 1000;    // page beats every 30 s
    private static final long BOOT_GRACE_MS = 90L * 1000;        // first beat deadline

    // The page on external storage can be replaced by an adb push at any time.
    // If it does not boot, fall back to index.html.bak, then to the packaged
    // asset, and remember the bad copy so the 6-hourly reload does not retry it.
    private static final String PREFS = "kiosk";
    private static final String PREF_BAD_PAGE = "badPageFp";

    // Bridge between the WebView page and the host Activity.
    // The page can call window.AndroidInterface.requestExit() to ask the kiosk
    // to go back to the Android home screen.
    public interface AndroidInterface {
        void requestExit();
        /** Page liveness ping; the shell rebuilds the WebView if it stops. */
        void beat();
    }

    private static final String ANDROID_INTERFACE_NAME = "AndroidInterface";

    private final Handler ui = new Handler(Looper.getMainLooper());
    private WebView web;
    private volatile long lastBeatMs = 0;
    private volatile long bootAtMs = 0;
    private volatile boolean beatSeen = false;
    private volatile String loadedFrom = "?";   // primary | backup | asset
    // Foreground guard: if the vendor launcher (com.gathin.tvhome) pulls Settings
    // to the front, bring the kiosk back after 2 min unless in maintenance mode.
    private volatile long lastResumedMs = 0;
    private volatile long maintenanceUntilMs = 0;
    private int menuPressCount = 0;
    private long menuPressStartMs = 0;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);

        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        goImmersive();

        if (Build.VERSION.SDK_INT >= 19) {
            WebView.setWebContentsDebuggingEnabled(true);
        }

        web = buildWebView();
        installBridge(web);
        setContentView(web);
        loadPage();   // starts the boot/liveness watchdog clock

        // Long-running kiosks slowly leak; a periodic rebuild keeps it stable.
        ui.postDelayed(new Runnable() {
            @Override
            public void run() {
                Log.i(TAG, "scheduled reload");
                recreateWebView();
                ui.postDelayed(this, RELOAD_MS);
            }
        }, RELOAD_MS);

        // If the page stops pinging (hung JS, dead timers), rebuild it instead of
        // leaving a frozen frame on a lobby wall for hours. A page that never
        // boots at all is treated as a bad push: mark it and fall back.
        ui.postDelayed(new Runnable() {
            @Override
            public void run() {
                long now = SystemClock.elapsedRealtime();
                if (lastBeatMs > 0 && now - lastBeatMs > BEAT_STALE_MS) {
                    Log.w(TAG, "page heartbeat lost (" + (now - lastBeatMs) + " ms), recreating");
                    beatSeen = true;   // fall through to the plain rebuild, not the boot fallback
                    recreateWebView();
                } else if (!beatSeen && bootAtMs > 0 && now - bootAtMs > BOOT_GRACE_MS) {
                    Log.w(TAG, "page from " + loadedFrom + " never booted, falling back");
                    markPrimaryBad("no heartbeat after load");
                    recreateWebView();
                }
                ui.postDelayed(this, BEAT_CHECK_MS);
            }
        }, BEAT_CHECK_MS);

        // If the vendor launcher (com.gathin.tvhome) pulls Settings to the front,
        // re-assert the kiosk after 2 min so the lobby display recovers on its own.
        // Three quick presses of MENU within 2 s toggles a 10-min maintenance
        // window so a technician can work without being fought.
        ui.postDelayed(new Runnable() {
            @Override
            public void run() {
                long now = SystemClock.elapsedRealtime();
                if (lastResumedMs > 0 && (now - lastResumedMs) > 120000L
                        && now > maintenanceUntilMs) {
                    Log.i(TAG, "foreground guard: re-asserting kiosk");
                    try {
                        Intent i = new Intent(MainActivity.this, MainActivity.class);
                        i.addFlags(Intent.FLAG_ACTIVITY_REORDER_TO_FRONT
                                | Intent.FLAG_ACTIVITY_NEW_TASK
                                | Intent.FLAG_ACTIVITY_CLEAR_TOP);
                        startActivity(i);
                    } catch (Exception e) {
                        Log.w(TAG, "foreground guard failed: " + e);
                    }
                }
                ui.postDelayed(this, 30000L);
            }
        }, 30000L);
    }

    private WebView buildWebView() {
        WebView v = new WebView(this);
        v.setBackgroundColor(Color.parseColor("#06070d"));

        WebSettings s = v.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setSupportZoom(false);
        s.setLoadWithOverviewMode(false);
        s.setUseWideViewPort(false);

        v.setWebViewClient(new WebViewClient() {
            @Override
            public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
                // A dead renderer leaves a blank screen; rebuild the WebView instead.
                Log.w(TAG, "render process gone, recreating");
                recreateWebView();
                return true;
            }

            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                Uri u = request.getUrl();
                if (u != null && HOST.equals(u.getHost())) {
                    String path = u.getPath();
                    if (path == null) path = "/";
                    if ("/api/haze".equals(path)) {
                        // Native proxy to JAS/APIMS (eqms.doe.gov.my only allows its own origin,
                        // so the WebView page can't call it directly). Cached for 30 min.
                        return serveHaze();
                    }
                    if (path.startsWith("/api/rainviewer/")) {
                        return serveRainviewer(path, u);
                    }
                    if ("/api/nowcast".equals(path) || "/api/metar".equals(path)) {
                        return serveWeather(path, u);
                    }
                    if (path.startsWith("/api/owm/")) {
                        return serveWeather(path, u);
                    }
                    if (path.startsWith("/api/radar/")) {
                        // MET radar GIF, proxied so the page gets it from this origin.
                        String rest = path.substring("/api/radar/".length());
                        if (rest.endsWith("/meta")) {
                            // When the frame currently on disk was published upstream.
                            String region = rest.substring(0, rest.length() - "/meta".length());
                            if (region.endsWith("/")) region = region.substring(0, region.length() - 1);
                            return serveRadarMeta(region);
                        }
                        return serveRadar(rest);
                    }
                    if ("/".equals(path) || "/index.html".equals(path)) {
                        byte[] body = loadIndex();
                        if (body != null) {
                            Map<String, String> h = new HashMap<>();
                            h.put("Content-Type", "text/html; charset=utf-8");
                            h.put("Cache-Control", "no-store, max-age=0");
                            h.put("Access-Control-Allow-Origin", "*");
                            return new WebResourceResponse("text/html", "utf-8", 200, "OK", h,
                                    new ByteArrayInputStream(body));
                        }
                    }
                    return new WebResourceResponse("text/plain", "utf-8", 404, "Not Found",
                            new HashMap<String, String>(),
                            new ByteArrayInputStream(new byte[0]));
                }
                return null;
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                // Never let the dashboard navigate away from the kiosk page.
                Uri u = request.getUrl();
                return u == null || !HOST.equals(u.getHost());
            }
        });
        return v;
    }

    private void recreateWebView() {
        if (web != null) {
            setContentView(new View(this));
            web.destroy();
            web = null;
        }
        web = buildWebView();
        installBridge(web);
        setContentView(web);
        loadPage();
    }

    /** One copy of the JS bridge, shared by onCreate and recreateWebView. */
    private void installBridge(WebView v) {
        v.addJavascriptInterface(new AndroidInterface() {
            @Override
            @JavascriptInterface
            public void requestExit() {
                moveTaskToBack(true);
            }

            @Override
            @JavascriptInterface
            public void beat() {
                lastBeatMs = SystemClock.elapsedRealtime();
                if (!beatSeen) {
                    beatSeen = true;
                    Log.i(TAG, "page alive from " + loadedFrom);
                }
            }
        }, ANDROID_INTERFACE_NAME);
    }

    /** Loads the kiosk page and restarts the boot watchdog clock. */
    private void loadPage() {
        bootAtMs = SystemClock.elapsedRealtime();
        beatSeen = false;
        loadedFrom = "?";
        web.loadUrl(ORIGIN);
        lastBeatMs = bootAtMs;
    }

    /**
     * Reads index.html from external storage, falling back to the last known
     * good copy and then to the packaged asset. A page that fails to boot is
     * recorded by fingerprint, so a broken push costs 90 s once rather than
     * 90 s on every 6-hourly reload; pushing a new file changes the
     * fingerprint and re-enables the primary copy automatically.
     */
    private byte[] loadIndex() {
        File dir = null;
        try {
            dir = getExternalFilesDir(null);
        } catch (Exception ignored) {
        }
        File primary = dir == null ? null : new File(dir, "index.html");
        File backup = dir == null ? null : new File(dir, "index.html.bak");

        if (primary != null && primary.isFile()) {
            if (isKnownBad(primary)) {
                Log.w(TAG, "primary page marked unbootable, using fallback");
            } else {
                byte[] body = readFile(primary);
                if (body != null) {
                    loadedFrom = "primary";
                    return body;
                }
            }
        }
        if (backup != null && backup.isFile()) {
            byte[] body = readFile(backup);
            if (body != null) {
                loadedFrom = "backup";
                return body;
            }
        }
        try (InputStream in = getAssets().open("index.html")) {
            loadedFrom = "asset";
            return readAll(in);
        } catch (IOException e) {
            Log.e(TAG, "asset index.html missing", e);
            return null;
        }
    }

    private static byte[] readFile(File f) {
        try (InputStream in = new FileInputStream(f)) {
            return readAll(in);
        } catch (IOException e) {
            Log.w(TAG, "page unreadable: " + f.getName() + ": " + e);
            return null;
        }
    }

    private static String fingerprint(File f) {
        return f.length() + ":" + f.lastModified();
    }

    private boolean isKnownBad(File f) {
        SharedPreferences p = getSharedPreferences(PREFS, MODE_PRIVATE);
        String bad = p.getString(PREF_BAD_PAGE, "");
        return !bad.isEmpty() && bad.equals(fingerprint(f));
    }

    private void markPrimaryBad(String why) {
        File dir = null;
        try {
            dir = getExternalFilesDir(null);
        } catch (Exception ignored) {
        }
        File primary = dir == null ? null : new File(dir, "index.html");
        if (primary == null || !primary.isFile()) return;
        getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                .putString(PREF_BAD_PAGE, fingerprint(primary)).apply();
        Log.w(TAG, "primary page marked unbootable (" + why + "): " + fingerprint(primary));
    }

    private static byte[] readAll(InputStream in) throws IOException {
        ByteArrayOutputStream out = new ByteArrayOutputStream(96 * 1024);
        byte[] buf = new byte[16384];
        int n;
        while ((n = in.read(buf)) > 0) {
            out.write(buf, 0, n);
        }
        return out.toByteArray();
    }

    /* Proxy RainViewer API + tiles so the page (http://weather.local) can load
     * them same-origin without mixed-content issues on the device WebView. */
    private WebResourceResponse serveRainviewer(String path, Uri uri) {
        Map<String, String> h = new HashMap<>();
        h.put("Access-Control-Allow-Origin", "*");
        try {
            if ("/api/rainviewer/maps".equals(path)) {
                byte[] body = fetchUrlBytes("https://api.rainviewer.com/public/weather-maps.json", 20000);
                h.put("Content-Type", "application/json; charset=utf-8");
                h.put("Cache-Control", "no-store, max-age=0");
                return new WebResourceResponse("application/json", "utf-8", 200, "OK", h,
                        new ByteArrayInputStream(body));
            } else if ("/api/rainviewer/tile".equals(path)) {
                String frame = uri.getQueryParameter("f");
                String z = uri.getQueryParameter("z");
                String x = uri.getQueryParameter("x");
                String y = uri.getQueryParameter("y");
                if (frame == null || !frame.startsWith("/v2/radar/") || frame.contains("..")
                        || !isDigits(z) || !isDigits(x) || !isDigits(y)) {
                    h.put("Content-Type", "text/plain; charset=utf-8");
                    return new WebResourceResponse("text/plain", "utf-8", 400, "Bad Request", h,
                            new ByteArrayInputStream("bad tile".getBytes()));
                }
                String url = "https://tilecache.rainviewer.com" + frame + "/256/" + z + "/" + x + "/" + y + "/2/1_1.png";
                byte[] body = fetchUrlBytes(url, 20000);
                h.put("Content-Type", "image/png");
                h.put("Cache-Control", "public, max-age=300");
                return new WebResourceResponse("image/png", null, 200, "OK", h,
                        new ByteArrayInputStream(body));
            }
        } catch (Exception e) {
            Log.w(TAG, "rainviewer proxy failed: " + e);
            h.put("Content-Type", "text/plain; charset=utf-8");
            return new WebResourceResponse("text/plain", "utf-8", 502, "Bad Gateway", h,
                    new ByteArrayInputStream("rainviewer unavailable".getBytes()));
        }
        h.put("Content-Type", "text/plain; charset=utf-8");
        return new WebResourceResponse("text/plain", "utf-8", 404, "Not Found", h,
                new ByteArrayInputStream(new byte[0]));
    }

    private static boolean isDigits(String s) {
        if (s == null || s.isEmpty()) return false;
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c < '0' || c > '9') return false;
        }
        return true;
    }

    private byte[] fetchUrlBytes(String url, int timeoutMs) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(10000);
        c.setReadTimeout(timeoutMs);
        c.setRequestMethod("GET");
        c.setRequestProperty("User-Agent", "Mozilla/5.0");
        c.setRequestProperty("Accept", "*/*");
        try {
            if (c.getResponseCode() != 200) throw new IOException("http " + c.getResponseCode());
            return readAll(c.getInputStream());
        } finally {
            c.disconnect();
        }
    }

    /* Tomorrow.io (free tier) 60-minute precipitation nowcast and real NOAA
     * METAR observations. Both are proxied: the Tomorrow key must not live in
     * the page, and aviationweather.gov sends no CORS headers. VATSIM's METAR
     * feed is simulated flight-sim weather, so it is deliberately not used.
     * OpenWeatherMap is proxied the same way (see OWM_* below). */
    private static final String TOMORROW_KEY = "REDACTED_TOMORROW_KEY";
    private static final String TOMORROW_URL =
            "https://api.tomorrow.io/v4/weather/forecast?location=%s,%s&timesteps=1m"
            + "&units=metric&fields=precipitationProbability,rainIntensity,weatherCode"
            + "&apikey=" + TOMORROW_KEY;
    private static final String METAR_URL =
            "https://aviationweather.gov/api/data/metar?ids=%s&format=raw&hours=2";

    /* OpenWeatherMap, proxied for the same reason as Tomorrow.io: the key must
     * not ship inside the page, which lives in plain text on /sdcard. */
    private static final String OWM_KEY = "REDACTED_OWM_KEY";
    private static final String OWM_CURRENT_URL =
            "https://api.openweathermap.org/data/2.5/weather?lat=%.4f&lon=%.4f"
            + "&units=metric&appid=" + OWM_KEY;
    private static final String OWM_FORECAST_URL =
            "https://api.openweathermap.org/data/2.5/forecast?lat=%.4f&lon=%.4f"
            + "&units=metric&appid=" + OWM_KEY;

    /** Validated lat/lon query params, or null when missing/invalid. */
    private static double[] latLon(Uri uri) {
        String lat = uri.getQueryParameter("lat");
        String lon = uri.getQueryParameter("lon");
        if (lat == null || lon == null) return null;
        try {
            double la = Double.parseDouble(lat);
            double lo = Double.parseDouble(lon);
            if (la < -90 || la > 90 || lo < -180 || lo > 180) return null;
            return new double[]{la, lo};
        } catch (NumberFormatException e) {
            return null;
        }
    }

    private WebResourceResponse serveWeather(String path, Uri uri) {
        Map<String, String> h = new HashMap<>();
        h.put("Access-Control-Allow-Origin", "*");
        try {
            String url, type;
            if ("/api/nowcast".equals(path)) {
                double[] ll = latLon(uri);
                if (ll == null) return badRequest(h, "bad lat/lon");
                // Locale.US keeps the decimal point as '.' on this device.
                url = String.format(java.util.Locale.US, TOMORROW_URL,
                        String.format(java.util.Locale.US, "%.4f", ll[0]),
                        String.format(java.util.Locale.US, "%.4f", ll[1]));
                type = "application/json";
            } else if (path.startsWith("/api/owm/")) {
                double[] ll = latLon(uri);
                if (ll == null) return badRequest(h, "bad lat/lon");
                String fmt = "/api/owm/forecast".equals(path) ? OWM_FORECAST_URL : OWM_CURRENT_URL;
                url = String.format(java.util.Locale.US, fmt, ll[0], ll[1]);
                type = "application/json";
            } else {
                String ids = uri.getQueryParameter("ids");
                if (ids != null) ids = ids.toUpperCase(java.util.Locale.US);
                if (ids == null || ids.isEmpty() || ids.length() > 40
                        || !ids.matches("[A-Z0-9,]+")) {
                    return badRequest(h, "bad ids");
                }
                url = String.format(METAR_URL, ids);
                type = "text/plain";
            }
            byte[] body = fetchUrlBytes(url, 20000);
            h.put("Content-Type", type + "; charset=utf-8");
            h.put("Cache-Control", "public, max-age=120");
            return new WebResourceResponse(type, "utf-8", 200, "OK", h,
                    new ByteArrayInputStream(body));
        } catch (Exception e) {
            Log.w(TAG, "weather proxy failed: " + e);
            h.put("Content-Type", "text/plain; charset=utf-8");
            return new WebResourceResponse("text/plain", "utf-8", 502, "Bad Gateway", h,
                    new ByteArrayInputStream("upstream unavailable".getBytes()));
        }
    }

    private static WebResourceResponse badRequest(Map<String, String> h, String msg) {
        h.put("Content-Type", "text/plain; charset=utf-8");
        return new WebResourceResponse("text/plain", "utf-8", 400, "Bad Request", h,
                new ByteArrayInputStream(msg.getBytes()));
    }

    /* Native proxy to JAS/APIMS for the kiosk page. The upstream only allows
     * its own web-app origin, so the WebView page cannot read it directly.
     * Result is cached for 30 minutes; if the network fails, a stale cache is
     * served instead of nothing. */
    private static final String HAZE_URL = "https://eqms.doe.gov.my/api3/publicportalapims/mapcaqm";
    private static final long HAZE_TTL_MS = 30 * 60 * 1000L;   // 30 minutes

    private WebResourceResponse serveHaze() {
        byte[] body = loadHaze();
        Map<String, String> h = new HashMap<>();
        h.put("Cache-Control", "no-store, max-age=0");
        h.put("Content-Type", "application/json; charset=utf-8");
        h.put("Access-Control-Allow-Origin", "*");
        if (body == null) {
            byte[] err = "{\"error\":\"upstream unavailable\"}".getBytes();
            return new WebResourceResponse("application/json", "utf-8", 502, "Bad Gateway", h,
                    new ByteArrayInputStream(err));
        }
        return new WebResourceResponse("application/json", "utf-8", 200, "OK", h,
                new ByteArrayInputStream(body));
    }

    private byte[] loadHaze() {
        File cache = new File(getFilesDir(), "apims.json");
        long now = System.currentTimeMillis();

        // Fresh cache? serve it
        if (cache.isFile() && now - cache.lastModified() < HAZE_TTL_MS) {
            byte[] fresh = readCache(cache);
            if (fresh != null) return fresh;
        }

        // Refresh from upstream
        try {
            HttpURLConnection c = (HttpURLConnection) new URL(HAZE_URL + "?_ts=" + now).openConnection();
            c.setConnectTimeout(15_000);
            c.setReadTimeout(20_000);
            c.setRequestMethod("GET");
            c.setRequestProperty("User-Agent", "Mozilla/5.0");
            c.setRequestProperty("Accept", "application/json");
            c.setRequestProperty("Referer", "https://eqms.doe.gov.my/APIMS/main");
            if (c.getResponseCode() == 200) {
                byte[] body = readAll(c.getInputStream());
                if (body.length > 0) {
                    writeCacheAtomic(cache, body);
                    return body;
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "haze fetch failed: " + e);
        }

        // Upstream failed: serve stale cache if we have one
        return readCache(cache);
    }

    private byte[] readCache(File cache) {
        try (InputStream in = new FileInputStream(cache)) {
            byte[] body = readAll(in);
            if (body.length > 0) return body;
        } catch (IOException e) {
            Log.w(TAG, "cache unreadable: " + cache.getName() + ": " + e);
        }
        return null;
    }

    private static void writeCacheAtomic(File cache, byte[] body) {
        try {
            File tmp = new File(cache.getPath() + ".tmp");
            FileOutputStream out = new FileOutputStream(tmp);
            try {
                out.write(body);
            } finally {
                out.close();
            }
            if (!tmp.renameTo(cache)) {
                tmp.delete();
            }
        } catch (IOException e) {
            Log.w(TAG, "cache write failed: " + cache.getName() + ": " + e);
        }
    }

    /* Native proxy for the MET Malaysia rain-radar GIFs.
     *
     * met.gov.my's TLS chain ends at GlobalSign Root R46, which this Android 10
     * box does not ship, so the WebView refuses the image (-202). The page also
     * cannot fetch cross-origin, so it asks this origin instead:
     *
     *     /api/radar/<region>        region = my | sem | east
     *     /api/radar/<region>/meta   {"updated": <epoch millis>}
     *
     * /meta reports the upstream Last-Modified of the frame this proxy is
     * serving, recorded in a sidecar next to the cached image. The scan time is
     * painted into the bitmap and cannot be read back, so this is what keeps
     * the panel's "Dikemas kini" label tied to the frame on screen.
     *
     * We fetch upstream over a composite trust store (bundled R46 + the system
     * roots), cache the GIF on disk for a few minutes, and serve it same-origin.
     * No hostname or chain verification is disabled. */
    private static final String RADAR_BASE = "https://www.met.gov.my/data/";
    private static final long RADAR_TTL_MS = 5 * 60 * 1000L;   // 5 minutes
    private static final Map<String, String> RADAR_REGIONS = new HashMap<>();

    static {
        RADAR_REGIONS.put("my", "radar_malaysia.gif");
        RADAR_REGIONS.put("sem", "radar_peninsular.gif");
        RADAR_REGIONS.put("east", "radar_east.gif");
    }

    private WebResourceResponse serveRadar(String region) {
        String file = RADAR_REGIONS.get(region);
        Map<String, String> h = new HashMap<>();
        h.put("Cache-Control", "no-store, max-age=0");
        h.put("Access-Control-Allow-Origin", "*");
        if (file == null) {
            h.put("Content-Type", "text/plain; charset=utf-8");
            return new WebResourceResponse("text/plain", "utf-8", 404, "Not Found", h,
                    new ByteArrayInputStream("unknown radar region".getBytes()));
        }

        byte[] body = loadRadar(region, file);
        if (body == null) {
            h.put("Content-Type", "text/plain; charset=utf-8");
            return new WebResourceResponse("text/plain", "utf-8", 502, "Bad Gateway", h,
                    new ByteArrayInputStream("radar unavailable".getBytes()));
        }
        // MET serves these as .gif but the payload is a PNG snapshot, so pick the
        // MIME type from the magic bytes rather than from the filename.
        String mime = isPng(body) ? "image/png" : "image/gif";
        h.put("Content-Type", mime);
        return new WebResourceResponse(mime, null, 200, "OK", h,
                new ByteArrayInputStream(body));
    }

    private static boolean isPng(byte[] b) {
        return b.length > 8 && (b[0] & 0xFF) == 0x89 && b[1] == 'P' && b[2] == 'N' && b[3] == 'G';
    }

    private WebResourceResponse serveRadarMeta(String region) {
        Map<String, String> h = new HashMap<>();
        h.put("Cache-Control", "no-store, max-age=0");
        h.put("Access-Control-Allow-Origin", "*");
        if (!RADAR_REGIONS.containsKey(region)) {
            h.put("Content-Type", "text/plain; charset=utf-8");
            return new WebResourceResponse("text/plain", "utf-8", 404, "Not Found", h,
                    new ByteArrayInputStream(("unknown radar region(meta):" + region).getBytes()));
        }
        long updated = readRadarStamp(region);
        if (updated <= 0) {
            // Older installs have the image cached but no sidecar: the cache
            // file's mtime is when we fetched it, i.e. within a minute of the
            // upstream publication.
            File cache = radarCacheFile(region);
            if (cache.isFile()) updated = cache.lastModified();
        }
        String json = "{\"region\":\"" + region + "\",\"updated\":" + updated + "}";
        h.put("Content-Type", "application/json; charset=utf-8");
        return new WebResourceResponse("application/json", "utf-8", 200, "OK", h,
                new ByteArrayInputStream(json.getBytes()));
    }

    private File radarCacheFile(String region) {
        return new File(getFilesDir(), "radar_" + region + ".gif");
    }

    private File radarStampFile(String region) {
        return new File(getFilesDir(), "radar_" + region + ".time");
    }

    /** Upstream Last-Modified of the cached frame as epoch millis, or 0. */
    private long readRadarStamp(String region) {
        File f = radarStampFile(region);
        if (!f.isFile()) return 0L;
        try (InputStream in = new FileInputStream(f)) {
            String s = new String(readAll(in), "UTF-8").trim();
            return Long.parseLong(s);
        } catch (Exception e) {
            Log.w(TAG, "radar stamp unreadable: " + f.getName() + ": " + e);
            return 0L;
        }
    }

    private static void writeTextAtomic(File f, String text) {
        try {
            File tmp = new File(f.getPath() + ".tmp");
            FileOutputStream out = new FileOutputStream(tmp);
            try {
                out.write(text.getBytes("UTF-8"));
            } finally {
                out.close();
            }
            if (!tmp.renameTo(f)) {
                tmp.delete();
            }
        } catch (IOException e) {
            Log.w(TAG, "stamp write failed: " + f.getName() + ": " + e);
        }
    }

    /** Parses an HTTP date (e.g. "Mon, 16 Nov 2026 08:11:05 GMT") to millis. */
    private static long parseHttpDate(String httpDate) {
        if (httpDate == null) return 0L;
        try {
            SimpleDateFormat f = new SimpleDateFormat("EEE, dd MMM yyyy HH:mm:ss zzz", Locale.US);
            f.setTimeZone(TimeZone.getTimeZone("GMT"));
            Date d = f.parse(httpDate.trim());
            return d == null ? 0L : d.getTime();
        } catch (Exception e) {
            Log.w(TAG, "unparsable Last-Modified: " + httpDate);
            return 0L;
        }
    }

    private byte[] loadRadar(String region, String file) {
        File cache = radarCacheFile(region);
        long now = System.currentTimeMillis();

        if (cache.isFile() && now - cache.lastModified() < RADAR_TTL_MS) {
            byte[] fresh = readCache(cache);
            if (fresh != null) return fresh;
        }

        try {
            HttpsURLConnection c = (HttpsURLConnection) new URL(RADAR_BASE + file + "?_ts=" + now)
                    .openConnection();
            c.setSSLSocketFactory(radarSocketFactory());
            c.setConnectTimeout(15_000);
            c.setReadTimeout(25_000);
            c.setRequestMethod("GET");
            c.setRequestProperty("User-Agent",
                    "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 (KHTML, like Gecko) "
                            + "Chrome/74 Mobile Safari/537.36");
            c.setRequestProperty("Accept", "image/gif,image/*,*/*");
            c.setRequestProperty("Referer", "https://www.met.gov.my/");
            if (c.getResponseCode() == 200) {
                byte[] body = readAll(c.getInputStream());
                if (body.length > 0) {
                    long published = parseHttpDate(c.getHeaderField("Last-Modified"));
                    writeCacheAtomic(cache, body);
                    writeTextAtomic(radarStampFile(region),
                            Long.toString(published > 0 ? published : now));
                    return body;
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "radar fetch failed (" + region + "): " + e);
        }

        // Upstream failed: serve stale cache if we have one
        return readCache(cache);
    }

    private javax.net.ssl.SSLSocketFactory radarSocketFactoryCache;

    /** TLS socket factory that trusts the bundled GlobalSign Root R46 in addition
     *  to the system roots (which is all the device ships). */
    private synchronized javax.net.ssl.SSLSocketFactory radarSocketFactory()
            throws Exception {
        if (radarSocketFactoryCache != null) return radarSocketFactoryCache;

        TrustManagerFactory system = TrustManagerFactory.getInstance("X509");
        system.init((KeyStore) null);

        KeyStore merged = KeyStore.getInstance(KeyStore.getDefaultType());
        merged.load(null, null);
        int added = 0;
        for (TrustManager tm : system.getTrustManagers()) {
            if (tm instanceof X509TrustManager) {
                for (X509Certificate cert : ((X509TrustManager) tm).getAcceptedIssuers()) {
                    merged.setCertificateEntry("sys" + (added++), cert);
                }
            }
        }
        // The one root this device is missing: GlobalSign Root R46.
        try (InputStream in = getAssets().open("groot-r46.pem")) {
            X509Certificate r46 = (X509Certificate) CertificateFactory
                    .getInstance("X.509").generateCertificate(in);
            merged.setCertificateEntry("groot-r46", r46);
        } catch (Exception e) {
            Log.w(TAG, "groot-r46.pem missing, radar may fail TLS: " + e);
        }

        TrustManagerFactory tmf = TrustManagerFactory.getInstance("X509");
        tmf.init(merged);
        SSLContext ctx = SSLContext.getInstance("TLS");
        ctx.init(null, tmf.getTrustManagers(), null);
        radarSocketFactoryCache = ctx.getSocketFactory();
        return radarSocketFactoryCache;
    }

    private void goImmersive() {
        getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY);
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) {
            goImmersive();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        lastResumedMs = SystemClock.elapsedRealtime();
        goImmersive();
        if (web != null) {
            web.onResume();
            web.resumeTimers();
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (web != null) {
            web.onPause();
        }
    }

    @Override
    protected void onDestroy() {
        ui.removeCallbacksAndMessages(null);
        if (web != null) {
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }

    /** Allow BACK to exit the kiosk to home, so the user can get back to the Android
     *  home screen if needed.  The page itself still has no back‑stack, so pressing
     *  BACK again will simply go to the home screen. */
    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        // Maintenance escape: 3 quick presses of MENU within 2 s toggles a
        // 10-min window where the foreground guard stands down, so a technician
        // can use Settings without being pulled back.
        if (keyCode == KeyEvent.KEYCODE_MENU) {
            long now = SystemClock.elapsedRealtime();
            if (now - menuPressStartMs > 2000L) {
                menuPressCount = 0;
                menuPressStartMs = now;
            }
            menuPressCount++;
            if (menuPressCount >= 3) {
                menuPressCount = 0;
                if (now > maintenanceUntilMs) {
                    maintenanceUntilMs = now + 10L * 60 * 1000;
                    Log.i(TAG, "maintenance mode ON for 10 min");
                } else {
                    maintenanceUntilMs = 0;
                    Log.i(TAG, "maintenance mode OFF");
                }
                return true;
            }
        }
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            // Let the system handle it – this will home the activity on most devices.
            return super.onKeyDown(keyCode, event);
        }
        return super.onKeyDown(keyCode, event);
    }
}
