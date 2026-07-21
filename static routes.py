"""
static_routes.py — favicon, app icons, PWA manifest and service worker.

Tiny static responses split out of main.py to keep it lean. No app state; just
bytes and a couple of small documents. URLs unchanged.
"""
from fastapi import APIRouter
from fastapi.responses import Response, JSONResponse, FileResponse

from app_icons import FAVICON_ICO as _FAVICON_ICO, ICON_180 as _ICON_180
try:
    from app_icons import OG_IMAGE as _OG_IMAGE
except Exception:
    _OG_IMAGE = b""

router = APIRouter()


@router.get("/app.css")
def _app_css():
    # Stylesheet split out of index.html to keep that file downloadable on the
    # phone-based GitHub workflow. Cached by the browser; bump ?v= in index.html
    # to bust it after CSS changes.
    return FileResponse("app.css", media_type="text/css", headers={
        "Cache-Control": "no-cache, must-revalidate"})


@router.get("/ll-enhance.js")
def _ll_enhance_js():
    # Enhancement JS (favorites/My Board, accent picker, Calibration view, Team
    # profiles) split out of index.html to keep that file downloadable on the
    # phone-based GitHub workflow. Loads after the main inline script, so it
    # shares its globals. Bump ?v= in index.html to bust the browser cache.
    return FileResponse("ll-enhance.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache, must-revalidate"})


@router.get("/ll-stocks.js")
def _ll_stocks_js():
    # Stocks feature (paper-traded model signals). Fully removable: delete this
    # route, the file, its <script> tag and the Markets menu item.
    return FileResponse("ll-stocks.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache, must-revalidate"})


@router.get("/favicon.ico")
def _favicon():
    return Response(content=_FAVICON_ICO, media_type="image/x-icon",
                    headers={"Cache-Control": "no-cache, must-revalidate"})

@router.get("/og-image.png")
def _og_image():
    return Response(content=_OG_IMAGE, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/icon-180.png")
def _icon180():
    return Response(content=_ICON_180, media_type="image/png",
                    headers={"Cache-Control": "no-cache, must-revalidate"})

@router.get("/apple-touch-icon.png")
@router.get("/apple-touch-icon-precomposed.png")
def _apple_icon():
    return Response(content=_ICON_180, media_type="image/png",
                    headers={"Cache-Control": "no-cache, must-revalidate"})


@router.get("/manifest.json")
def _manifest():
    """PWA manifest so Line Logic installs to the home screen and launches
    full-screen like a native app."""
    return JSONResponse({
        "name": "Line Logic", "short_name": "Line Logic",
        "description": "Multi-sport prediction & betting analytics.",
        "start_url": "/", "scope": "/", "display": "standalone",
        "background_color": "#0e1014", "theme_color": "#0e1014",
        "orientation": "portrait",
        "icons": [
            {"src": "/icon-180.png", "sizes": "180x180", "type": "image/png"},
            {"src": "/icon-180.png", "sizes": "192x192", "type": "image/png",
             "purpose": "any maskable"},
            {"src": "/icon-180.png", "sizes": "512x512", "type": "image/png",
             "purpose": "any maskable"},
        ],
    }, headers={"Cache-Control": "public, max-age=3600"})


@router.get("/sw.js")
def _service_worker():
    """Service worker: caches the app shell so it opens instantly and works
    offline, but NEVER caches /api or websocket traffic (live data stays fresh).
    Bump CACHE to invalidate after a shell change.

    THE BUG THIS FIXES (v4 -> v5): the old fetch handler fell back to
    caches.match("/") for EVERY failed request, not just page loads. A transient
    failure on /ll-enhance.js or /app.css therefore returned index.html to a
    <script> or <link> tag — the browser parsed "<!DOCTYPE html>" as JavaScript
    and threw "SyntaxError: Unexpected token '<'" at line 1, and the page
    rendered with no styling at all. Two rules now prevent that:

      1. The shell fallback applies ONLY to navigations. Every other request
         falls back to a cached copy of ITS OWN url, or fails honestly. A failed
         script load is recoverable on reload; HTML parsed as JS is not.
      2. Successful responses are written back to the cache, so the offline
         shell tracks the deployed build instead of freezing at whatever was
         current when this CACHE version first installed. That staleness is why
         old builds kept reappearing after a deploy.
    """
    js = """
const CACHE = "ll-shell-v5";

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(["/"])).catch(()=>{}));
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks =>
    Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});

function put(key, res) {
  // cache a copy without blocking the response the page is waiting on
  if (!res || !res.ok || res.type === "opaque") return;
  const copy = res.clone();
  caches.open(CACHE).then(c => c.put(key, copy)).catch(()=>{});
}

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;

  let u;
  try { u = new URL(req.url); } catch (_) { return; }
  if (u.origin !== self.location.origin) return;              // fonts, CDNs
  if (u.pathname.startsWith("/api/") || u.pathname.startsWith("/ws")) return;

  const accept = req.headers.get("accept") || "";
  const isNav = req.mode === "navigate" || accept.includes("text/html");

  if (isNav) {
    // Page loads: network first, refresh the stored shell, fall back to it.
    e.respondWith(
      fetch(req).then(r => { put("/", r); return r; })
                .catch(() => caches.match("/").then(r => r || Response.error()))
    );
    return;
  }

  // Everything else (js, css, icons): network first, and on failure fall back
  // ONLY to a cached copy of this exact url. Never the HTML shell.
  e.respondWith(
    fetch(req).then(r => { put(req, r); return r; })
              .catch(() => caches.match(req).then(r => r || Response.error()))
  );
});
"""
    return Response(content=js, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})
