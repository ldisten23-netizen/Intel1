package za.intel.app;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.print.PrintAttributes;
import android.print.PrintDocumentAdapter;
import android.print.PrintManager;
import android.view.View;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.TextView;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

public class MainActivity extends Activity {
    private static final int FILE_CHOOSER = 1;
    private WebView web;
    private TextView status;
    private ValueCallback<Uri[]> pendingChooser;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        status = new TextView(this);
        status.setText("Starting...");
        status.setTextSize(18f);
        status.setPadding(48, 96, 48, 48);
        setContentView(status);

        // Start the bundled web app off the main thread, then show it in a WebView.
        new Thread(() -> {
            try {
                if (!Python.isStarted()) {
                    Python.start(new AndroidPlatform(this));
                }
                PyObject launcher = Python.getInstance().getModule("launcher");
                String info = launcher.callAttr("start", getFilesDir().getAbsolutePath()).toString();
                String[] parts = info.split(":", 2);
                runOnUiThread(() -> showWeb("http://127.0.0.1:" + parts[0] + "/?k=" + parts[1]));
            } catch (Throwable t) {
                runOnUiThread(() -> status.setText("Could not start: " + t));
            }
        }).start();
    }

    private void showWeb(String url) {
        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(true);
        web.addJavascriptInterface(new PrintBridge(), "AndroidPrint");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                // stay inside the app: only the local server is ever opened
                return !"127.0.0.1".equals(r.getUrl().getHost());
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView w, ValueCallback<Uri[]> cb, FileChooserParams p) {
                if (pendingChooser != null) pendingChooser.onReceiveValue(null);
                pendingChooser = cb;
                Intent i = new Intent(Intent.ACTION_GET_CONTENT);
                i.addCategory(Intent.CATEGORY_OPENABLE);
                i.setType("image/*");
                try {
                    startActivityForResult(Intent.createChooser(i, "Choose photo"), FILE_CHOOSER);
                } catch (Exception e) {
                    pendingChooser = null;
                    cb.onReceiveValue(null);
                    return false;
                }
                return true;
            }
        });
        setContentView(web);
        web.loadUrl(url);
    }

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        super.onActivityResult(req, res, data);
        if (req == FILE_CHOOSER && pendingChooser != null) {
            Uri[] result = null;
            if (res == RESULT_OK && data != null && data.getData() != null) {
                result = new Uri[]{data.getData()};
            }
            pendingChooser.onReceiveValue(result);
            pendingChooser = null;
        }
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }

    /** window.AndroidPrint.print(title): opens Android's print dialog ("Save as PDF" is one of its printers). */
    private class PrintBridge {
        @JavascriptInterface
        public void print(final String title) {
            runOnUiThread(() -> {
                PrintManager pm = (PrintManager) getSystemService(PRINT_SERVICE);
                String job = (title == null || title.isEmpty()) ? "Report" : title;
                PrintDocumentAdapter adapter = web.createPrintDocumentAdapter(job);
                pm.print(job, adapter, new PrintAttributes.Builder().build());
            });
        }
    }
                              }
