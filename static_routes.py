"""
static_routes.py — favicon, app icons, PWA manifest and service worker.

Tiny static responses split out of main.py to keep it lean. No app state; just
bytes and a couple of small documents. URLs unchanged.
"""
from fastapi import APIRouter
from fastapi.responses import Response, JSONResponse

from app_icons import FAVICON_ICO as _FAVICON_ICO, ICON_180 as _ICON_180

router = APIRouter()


@router.get("/favicon.ico")
def _favicon():
    return Response(content=_FAVICON_ICO, media_type="image/x-icon",
                    headers={"Cache-Control": "public, max-age=86400"})

@router.get("/icon-180.png")
def _icon180():
    return Response(content=_ICON_180, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})

@router.get("/apple-touch-icon.png")
@router.get("/apple-touch-icon-precomposed.png")
def _apple_icon():
    return Response(content=_ICON_180, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


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
    """Minimal service worker: caches the app shell so it opens instantly and
    works offline, but NEVER caches /api or websocket traffic (live data stays
    fresh). Bump CACHE to invalidate after a shell change."""
    js = """
const CACHE = "ll-shell-v1";
self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.add("/")).catch(()=>{}));
  self.skipWaiting();
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks =>
    Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  if (u.pathname.startsWith("/api/") || u.pathname.startsWith("/ws")) return;
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request).then(r => r || caches.match("/")))
  );
});
"""
    return Response(content=js, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})


