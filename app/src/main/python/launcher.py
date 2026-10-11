"""Starts the web app inside the Android app (called from MainActivity)."""
import os
import secrets
import threading

_state = {}


class _Guard:
    """Only this app's WebView may talk to the local server: it opens /?k=<token> once and gets a cookie."""

    def __init__(self, app, token):
        self.app, self.token = app, token

    def __call__(self, environ, start_response):
        if f"k={self.token}" in environ.get("QUERY_STRING", "").split("&"):
            start_response("302 Found", [("Location", "/"),
                                         ("Set-Cookie", f"intel_k={self.token}; Path=/; HttpOnly; SameSite=Strict")])
            return [b""]
        cookies = [c.strip() for c in environ.get("HTTP_COOKIE", "").split(";")]
        if f"intel_k={self.token}" in cookies:
            return self.app(environ, start_response)
        start_response("403 Forbidden", [("Content-Type", "text/plain")])
        return [b"Forbidden"]


def start(data_dir):
    """Returns 'port:token'. Safe to call more than once."""
    if "info" in _state:
        return _state["info"]
    os.environ["INTEL_DATA"] = data_dir
    os.environ.setdefault("ADMIN_PASSWORD", "admin")        # first-run login is admin / admin: change it in Settings
    from intel import app as A
    A.setup()
    token = secrets.token_urlsafe(24)
    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", 0, _Guard(A.app, token), threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _state["info"] = f"{srv.server_port}:{token}"
    return _state["info"]
