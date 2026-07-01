"""Surface / ratings build + diagnostic OPS endpoints, split out of main.py to
keep main.py small enough to edit on a phone. These are admin/ops tools, not
hot-path serving. Shared state lives in main; we reach it via `import main` and
reference main.<name> inside handlers (safe: handlers run long after main has
finished importing). main.py includes this router at the very end."""
import os, io, csv, json, time, datetime as dt
import urllib.request, urllib.error
import threading
from urllib.error import HTTPError
from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse, PlainTextResponse
import main

router = APIRouter()

@router.get("/api/surface/rebuild")
def _surface_rebuild(confirm: str = "", start: int = 2015):
    """Build surface_records.json on Railway via the GitHub Trees+Blobs API
    (api.github.com is reachable here; raw/CDN hosts are not). Blobs come back
    base64-inline in JSON, so nothing redirects to the blocked githubusercontent
    CDN. Saves to the /data volume (survives redeploys) and hot-reloads memory.
    Append ?confirm=yes to run. Optional &start=YYYY (default 2015)."""
    if confirm != "yes":
        return JSONResponse({
            "note": "append ?confirm=yes to run",
            "effect": "fetches ATP+WTA match CSVs via api.github.com, rebuilds "
                      "surface_records.json, saves to /data, reloads in memory",
            "start_year": start})
    import urllib.request as _ur, urllib.error as _ue, base64 as _b64, csv as _csv, io as _io
    try:
        import build_surface_records as _bsr
    except Exception as e:
        return JSONResponse({"error": f"cannot import build_surface_records: {e}"})
    tok = (os.environ.get("DATA_TOKEN") or os.environ.get("GITHUB_DATA_TOKEN")
           or os.environ.get("GH_DATA_TOKEN") or "")

    def _api(url, accept="application/vnd.github+json"):
        h = {"User-Agent": "linelogic-surface/1.0", "Accept": accept}
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        req = _ur.Request(url, headers=h)
        with _ur.urlopen(req, timeout=90) as r:
            return r.read()

    end = dt.date.today().year
    store: dict = {}
    report = {"auth": "bearer-token" if tok else "anonymous(60/hr)",
              "start": start, "end": end, "repos": {}, "errors": []}
    if tok:
        try:
            rl = json.loads(_api("https://api.github.com/rate_limit"))
            lim = rl.get("resources", {}).get("core", {}).get("limit", 0)
            report["token_check"] = ("VALID (authenticated, limit %d/hr)" % lim
                                     if lim >= 5000 else
                                     "NOT APPLIED (limit %d \u2014 token missing/invalid)" % lim)
        except Exception as e:
            report["token_check"] = f"could not verify: {e}"
    for repo, pre in (("tennis_atp", "atp_matches_"), ("tennis_wta", "wta_matches_")):
        try:
            tree = json.loads(_api(
                f"https://api.github.com/repos/JeffSackmann/{repo}/git/trees/master?recursive=1"))
            wanted = []
            for t in tree.get("tree", []):
                p = t.get("path", "")
                if p.startswith(pre) and p.endswith(".csv"):
                    yr = p[len(pre):-4]
                    if yr.isdigit() and start <= int(yr) <= end:
                        wanted.append((p, t["sha"]))
            picked, total = [], 0
            for p, sha in sorted(wanted):
                blob = json.loads(_api(
                    f"https://api.github.com/repos/JeffSackmann/{repo}/git/blobs/{sha}"))
                if blob.get("encoding") != "base64":
                    continue
                text = _b64.b64decode(blob["content"]).decode("utf-8", "replace")
                rows = list(_csv.DictReader(_io.StringIO(text)))
                n = _bsr.aggregate(rows, store)
                total += n
                picked.append(f"{p}:+{n}")
            report["repos"][repo] = {"files": len(picked), "matches": total,
                                     "tree_truncated": tree.get("truncated", False),
                                     "picked": picked}
        except _ue.HTTPError as e:
            report["errors"].append(f"{repo}: HTTPError {e.code}")
        except Exception as e:
            report["errors"].append(f"{repo}: {type(e).__name__}: {e}")

    report["players"] = len(store)
    probe = {nm: bool(main._resolve_surface_rec(nm)) or any(
                 nm.split()[-1].lower() in k for k in store)
             for nm in ("Aryna Sabalenka", "Coco Gauff", "Iga Swiatek")}
    report["wta_present"] = probe

    if len(store) >= 1500 and any(probe.values()):
        save_path = main._srf if str(main._srf).startswith("/data") else "/data/surface_records.json"
        try:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
        except Exception:
            pass
        try:
            with open(save_path, "w") as f:
                json.dump(store, f, separators=(",", ":"))
            main.SURFACE_RECORDS = store
            main._rebuild_surface_abbrev()
            report["saved_to"] = save_path
            report["status"] = "SAVED to volume + loaded into memory (Surface tab live now)"
        except Exception as e:
            main.SURFACE_RECORDS = store
            main._rebuild_surface_abbrev()
            report["status"] = (f"loaded into memory but volume write failed ({e}); "
                                "will rebuild on next restart")
    else:
        hint = ("" if tok else " No token was set, so reads ran anonymously and Railway's "
                "IP is filtered \u2014 add a CLASSIC GitHub token as the DATA_TOKEN env var on "
                "Railway and rerun.")
        report["status"] = ("NOT saved \u2014 guard failed (need \u22651500 players AND a WTA "
                             "name present)." + hint)
    return JSONResponse(report, headers={"Cache-Control": "no-store"})


@router.post("/api/surface/upload")
def _surface_upload(payload: dict, confirm: str = ""):
    """Receive a surface_records store built in the user's browser (which can
    reach raw.githubusercontent / jsDelivr from a residential IP) and save it to
    the /data volume + hot-reload memory. Guarded so a bad payload can't wipe a
    good cache."""
    if confirm != "yes":
        return JSONResponse({"error": "append ?confirm=yes"}, status_code=400)
    store = payload or {}
    n = len(store)
    probe = {nm: any(nm in k for k in store)
             for nm in ("sabalenka", "gauff", "swiatek")}
    if n < 1500 or not any(probe.values()):
        return JSONResponse({"saved": False, "players": n, "wta_probe": probe,
                             "error": "guard failed: need >=1500 players AND a WTA name"},
                            status_code=400)
    # shape sanity-check on a sample
    bad = 0
    for k in list(store.keys())[:50]:
        v = store[k]
        if not isinstance(v, dict) or "surfaces" not in v:
            bad += 1
    if bad:
        return JSONResponse({"saved": False, "error": f"payload shape invalid ({bad}/50 bad)"},
                            status_code=400)
    save_path = main._srf if str(main._srf).startswith("/data") else "/data/surface_records.json"
    try:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
    except Exception:
        pass
    try:
        with open(save_path, "w") as f:
            json.dump(store, f, separators=(",", ":"))
    except Exception as e:
        main.SURFACE_RECORDS = store
        main._rebuild_surface_abbrev()
        return JSONResponse({"saved": False, "players": n,
                             "note": f"loaded into memory but volume write failed: {e}"})
    main.SURFACE_RECORDS = store
    main._rebuild_surface_abbrev()
    return JSONResponse({"saved": True, "players": n, "saved_to": save_path,
                         "wta_probe": probe})


_SURFACE_BUILDER_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Surface records builder</title>
<style>
 body{font-family:-apple-system,system-ui,sans-serif;margin:0;padding:18px;background:#0f1115;color:#e7e9ee}
 h1{font-size:19px;margin:0 0 4px} p{font-size:14px;color:#aab;margin:6px 0 14px;line-height:1.4}
 button{font-size:16px;font-weight:600;padding:13px 18px;border:0;border-radius:11px;background:#3b82f6;color:#fff;width:100%}
 button:disabled{background:#334}
 #log{margin-top:16px;font-size:12.5px;font-family:ui-monospace,monospace;white-space:pre-wrap;
   background:#161922;border:1px solid #232838;border-radius:10px;padding:11px;max-height:62vh;overflow:auto}
 .ok{color:#4ade80}.err{color:#f87171}.mut{color:#8b93a7}
</style></head><body>
<h1>Build surface records</h1>
<p>This runs in <b>your browser</b>, so it pulls the tennis data from your normal connection (the server can't, but your phone can). It builds the file and sends it to the app. Takes ~30&ndash;60s. Leave this open while it runs.</p>
<button id="go" onclick="run()">Build &amp; upload</button>
<div id="log"></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/PapaParse/5.4.1/papaparse.min.js"></script>
<script>
const L=document.getElementById('log'), B=document.getElementById('go');
function log(m,c){const s=document.createElement('div');if(c)s.className=c;s.textContent=m;L.appendChild(s);L.scrollTop=L.scrollHeight;}
function normName(n){if(!n)return"";const d=n.normalize("NFKD");let o="";for(let i=0;i<d.length;i++){const c=d.charCodeAt(i);if(c>=768&&c<=879)continue;o+=d[i];}return o.toLowerCase().split(/\\s+/).filter(Boolean).join(" ");}
const SURF=new Set(["Hard","Clay","Grass","Carpet"]);
function titleSurf(s){s=(s||"").trim();if(!s)return"";return s.charAt(0).toUpperCase()+s.slice(1).toLowerCase();}
function bump(store,name,surface,year,won){
  const k=normName(name);if(!k)return;
  let r=store[k];if(!r){r={name:name,surfaces:{}};store[k]=r;}
  let sf=r.surfaces[surface];if(!sf){sf={career:[0,0],by_year:{}};r.surfaces[surface]=sf;}
  let y=sf.by_year[year];if(!y){y=[0,0];sf.by_year[year]=y;}
  sf.career[won?0:1]++;y[won?0:1]++;
}
async function fetchCsv(repo,fname,verbose){
  const raw="https://raw.githubusercontent.com/JeffSackmann/"+repo+"/master/"+fname;
  const urls=[
    raw,
    "https://cdn.jsdelivr.net/gh/JeffSackmann/"+repo+"@master/"+fname,
    "https://api.codetabs.com/v1/proxy?quest="+encodeURIComponent(raw),
    "https://api.allorigins.win/raw?url="+encodeURIComponent(raw),
    "https://api.allorigins.win/get?url="+encodeURIComponent(raw)
  ];
  const errs=[];
  for(const u of urls){
    const host=u.split("/")[2];
    try{
      const r=await fetch(u);
      if(r.ok){
        let t=await r.text();
        // allorigins /get wraps the body in JSON {contents: "..."}
        if(u.indexOf("/get?")>=0){try{t=JSON.parse(t).contents;}catch(e){}}
        if(t&&t.indexOf("tourney_")>=0)return t;
        errs.push(host+" badbody");
      } else errs.push(host+" HTTP "+r.status);
    }catch(e){errs.push(host+" "+(e&&e.message?e.message:e));}
  }
  if(verbose)log("    tried: "+errs.join(" | "),"mut");
  return null;
}
function aggregate(text,store){
  let added=0;
  const out=Papa.parse(text,{header:true,skipEmptyLines:true});
  for(const row of out.data){
    const surface=titleSurf(row.surface);
    if(!SURF.has(surface))continue;
    const date=(row.tourney_date||"").toString().trim();
    const year=date.slice(0,4);
    if(year.length!==4||isNaN(year))continue;
    const w=(row.winner_name||"").trim(), l=(row.loser_name||"").trim();
    if(!w||!l)continue;
    bump(store,w,surface,year,true);
    bump(store,l,surface,year,false);
    added++;
  }
  return added;
}
async function run(){
  B.disabled=true;L.innerHTML="";
  const store={};const y1=new Date().getFullYear();const start=2015;
  let total=0, tries=0;
  for(const [repo,pre] of [["tennis_atp","atp_matches_"],["tennis_wta","wta_matches_"]]){
    for(let y=start;y<=y1;y++){
      const fname=pre+y+".csv";
      const text=await fetchCsv(repo,fname,tries<2);tries++;
      if(!text){log("  skip "+fname+" (not found)","mut");continue;}
      const n=aggregate(text,store);total+=n;
      log("  "+fname+": +"+n.toLocaleString()+"  (players "+Object.keys(store).length.toLocaleString()+")");
    }
  }
  const players=Object.keys(store).length;
  const wta=["sabalenka","gauff","swiatek"].filter(nm=>Object.keys(store).some(k=>k.includes(nm)));
  log("");
  log("Built "+players.toLocaleString()+" players from "+total.toLocaleString()+" matches.");
  log("WTA check: "+(wta.length?wta.join(", "):"NONE FOUND"),wta.length?"ok":"err");
  if(players<1500||!wta.length){log("Aborting upload \\u2014 looks incomplete.","err");B.disabled=false;return;}
  log("Uploading to the app \\u2026");
  try{
    const res=await fetch("/api/surface/upload?confirm=yes",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify(store)});
    const j=await res.json();
    if(j.saved){log("\\u2705 SAVED "+j.players.toLocaleString()+" players to the server. Surface tab is live now.","ok");}
    else{log("Server rejected it: "+(j.error||JSON.stringify(j)),"err");}
  }catch(e){log("Upload failed: "+e,"err");}
  B.disabled=false;
}
</script></body></html>"""


@router.get("/surface-builder")
def _surface_builder_page():
    return Response(content=_SURFACE_BUILDER_HTML, media_type="text/html",
                    headers={"Cache-Control": "no-store"})


main._FEED_BUILD = {"running": False, "report": None}


def _run_feed_build(start: int, chunk_days: int = 7):
    """Wrapper: guarantees the running flag is cleared no matter what, so a build
    can never again get stuck 'running' with no report."""
    try:
        _run_feed_build_inner(start, chunk_days)
    except Exception as e:
        import traceback
        cur = main._FEED_BUILD.get("report") or {}
        cur["fatal"] = f"{type(e).__name__}: {e}"
        cur["trace"] = traceback.format_exc()[-1200:]
        main._FEED_BUILD["report"] = cur
    finally:
        main._FEED_BUILD["running"] = False


def _run_feed_build_inner(start: int, chunk_days: int = 7):
    import calendar as _cal
    report = {"start": start, "chunk_days": chunk_days, "by_year": {}, "errors": [], "calls": 0, "matches": 0}
    try:
        import apitennis as _at
        prov = _at.APITennisProvider()
    except Exception as e:
        report["status"] = f"api-tennis init failed: {e}"
        main._FEED_BUILD["report"] = report
        main._FEED_BUILD["running"] = False
        return
    SURF = {"Hard", "Clay", "Grass", "Carpet"}
    store: dict = {}

    def bump(nm, surface, year, won):
        k = main._norm_surface_name(nm)
        if not k:
            return
        r = store.setdefault(k, {"name": nm, "surfaces": {}})
        sf = r["surfaces"].setdefault(surface, {"career": [0, 0], "by_year": {}})
        yr = sf["by_year"].setdefault(year, [0, 0])
        sf["career"][0 if won else 1] += 1
        yr[0 if won else 1] += 1

    # Deep base: start from the committed Sackmann ATP file (full career history),
    # then overlay the feed (which adds WTA + any players the base lacks). Read the
    # repo/app copy explicitly, NOT /data (that's the previous feed output).
    base: dict = {}
    _basedir = os.path.dirname(os.path.abspath(__file__))
    for p in ("surface_records.json", os.path.join(_basedir, "surface_records.json"),
              "/app/surface_records.json"):
        try:
            with open(p) as bf:
                cand = json.load(bf)
            if isinstance(cand, dict) and len(cand) > len(base):
                base, report["base_file"] = cand, p
        except Exception:
            continue
    report["base_players"] = len(base)

    def _grab(d0, d1):
        return prov._call("get_fixtures", date_start=d0.isoformat(), date_stop=d1.isoformat())

    today = dt.date.today()
    if start < 2010:
        start = 2010
    span = max(1, min(31, chunk_days))
    cur = dt.date(start, 1, 1)
    empty_streak = 0
    try:
        while cur <= today:
            cend = min(cur + dt.timedelta(days=span - 1), today)
            try:
                rows = _grab(cur, cend)
                report["calls"] += 1
            except Exception as ex:
                rows = []
                # A timeout means the whole range is unreachable (e.g. a year not
                # in the plan) — don't waste 7x20s on day-by-day. A 500 is usually
                # a size issue, so a day-by-day retry is worth it there.
                if "timeout" in (type(ex).__name__ + str(ex)).lower():
                    report["errors"].append(f"{cur:%Y-%m-%d}: timeout (range skipped)")
                else:
                    d = cur
                    while d <= cend:
                        try:
                            rows += _grab(d, d) or []
                            report["calls"] += 1
                        except Exception as e2:
                            report["errors"].append(f"{d:%Y-%m-%d}: {type(e2).__name__}")
                        d += dt.timedelta(days=1)
            n = 0
            for fix in rows or []:
                if not fix.get("event_winner"):
                    continue
                win = _at._winner(fix.get("event_winner"))
                if not win:
                    continue
                pa = (fix.get("event_first_player") or "").strip()
                pb = (fix.get("event_second_player") or "").strip()
                if not pa or not pb or "/" in pa or "/" in pb:
                    continue
                tier = _at._classify_tier(fix)
                if tier not in ("ATP", "WTA"):
                    continue
                ds = (fix.get("event_date") or "").strip()
                year = ds[:4] if (len(ds) >= 4 and ds[:4].isdigit()) else str(cur.year)
                try:
                    when = dt.date.fromisoformat(ds)
                except Exception:
                    when = cur
                surface = _at._infer_surface(fix.get("tournament_name") or "", tier, when)
                if surface not in SURF:
                    continue
                w = pa if win == "a" else pb
                l = pb if win == "a" else pa
                bump(w, surface, year, True)
                bump(l, surface, year, False)
                n += 1
            if n:
                yk = f"{cur:%Y}"
                report.setdefault("by_year", {})[yk] = report["by_year"].get(yk, 0) + n
                empty_streak = 0
            else:
                empty_streak += 1
            report["matches"] += n
            report["players_so_far"] = len(store)
            main._FEED_BUILD["report"] = dict(report)  # live progress for polling
            # If we're deep into a range with zero matches found, the data isn't
            # there (e.g. a start year before your plan's history) — stop grinding.
            if report["matches"] == 0 and empty_streak >= 8:
                report["aborted"] = (f"no matches in first {empty_streak} chunks from "
                                     f"{start} \u2014 that history isn't in your api-tennis plan; "
                                     f"try a later &start=")
                break
            cur = cend + dt.timedelta(days=1)
    except Exception as e:
        report["errors"].append(f"loop: {type(e).__name__}: {e}")

    # Overlay: keep every deep base (Sackmann ATP) record; add feed players the
    # base doesn't have (all WTA + any new entrants). No double-counting of ATP.
    report["feed_players"] = len(store)
    if base:
        added = 0
        for k, v in store.items():
            if k not in base:
                base[k] = v
                added += 1
        report["feed_added_to_base"] = added
        store = base
    report["players"] = len(store)
    probe = {nm: any(nm in k for k in store) for nm in ("sabalenka", "gauff", "swiatek")}
    report["wta_present"] = probe
    if len(store) >= 200 and any(probe.values()):
        save_path = main._srf if str(main._srf).startswith("/data") else "/data/surface_records.json"
        try:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
        except Exception:
            pass
        try:
            with open(save_path, "w") as f:
                json.dump(store, f, separators=(",", ":"))
            main.SURFACE_RECORDS = store
            main._rebuild_surface_abbrev()
            report["saved_to"] = save_path
            report["status"] = "DONE \u2014 saved to volume + loaded into memory (Surface tab live now)"
        except Exception as e:
            main.SURFACE_RECORDS = store
            main._rebuild_surface_abbrev()
            report["status"] = f"DONE in memory; volume write failed ({e})"
    else:
        le = getattr(prov, "last_error", None)
        report["status"] = ("DONE but NOT saved \u2014 too few players / no WTA found."
                            + (f" api-tennis last_error: {le}" if le else ""))
    main._FEED_BUILD["report"] = report
    main._FEED_BUILD["running"] = False


@router.get("/api/surface/build-from-feed")
def _surface_from_feed(confirm: str = "", start: int = 2024, chunk: int = 7, force: str = ""):
    """Plan B: build surface_records.json from the api-tennis feed (which Railway
    reaches) instead of GitHub. Pulls finished ATP+WTA singles from ?start=YYYY
    (default 2024) to today in small date-chunks, infers surface from the
    tournament name the same way the board does, aggregates per-player W/L, saves
    to /data and hot-reloads. Background; poll /api/surface/feed-status.
    &force=yes clears a stuck/hung previous run."""
    if confirm != "yes":
        yrs = max(1, dt.date.today().year - start + 1)
        return JSONResponse({"note": "append ?confirm=yes to run",
                             "start": start, "approx_calls": yrs * 12,
                             "effect": "background build from api-tennis finished singles -> /data"})
    if main._FEED_BUILD["running"] and force != "yes":
        return JSONResponse({"status": "already running",
                             "tip": "if it's stuck, add &force=yes to clear and restart",
                             "poll": "/api/surface/feed-status"})
    main._FEED_BUILD["running"] = True
    main._FEED_BUILD["report"] = None
    import threading
    threading.Thread(target=_run_feed_build, args=(start, chunk), daemon=True).start()
    return JSONResponse({"status": "build started in background"
                                   + (" (forced over a stuck run)" if force == "yes" else ""),
                         "poll": "/api/surface/feed-status",
                         "note": "refresh feed-status until running=false"})


@router.get("/api/surface/feed-probe")
def _feed_probe(date: str = ""):
    """Test ONE api-tennis get_fixtures call in isolation, wrapped so it can't hang
    the request. Tells us if the calls work, error, or hang — which is what's
    been stalling the background build."""
    import time as _t, threading
    d = date or (dt.date.today() - dt.timedelta(days=2)).isoformat()
    out = {}

    def _do():
        try:
            import apitennis as _at
            prov = _at.APITennisProvider()
            out["req_count"] = getattr(prov, "_req_count", None)
            out["daily_max"] = getattr(_at, "_DAILY_MAX", None)
            t0 = _t.time()
            rows = prov._call("get_fixtures", date_start=d, date_stop=d)
            out["seconds"] = round(_t.time() - t0, 1)
            out["rows"] = len(rows or [])
            out["finished"] = sum(1 for f in (rows or []) if f.get("event_winner"))
            out["sample"] = [{"t": (f.get("tournament_name") or "")[:28],
                              "p1": f.get("event_first_player"),
                              "p2": f.get("event_second_player"),
                              "w": f.get("event_winner")} for f in (rows or [])[:3]]
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"
            out["last_error"] = getattr(locals().get("prov", None), "last_error", None)

    th = threading.Thread(target=_do, daemon=True)
    th.start()
    th.join(25)
    if th.is_alive():
        return JSONResponse({"date": d, "result": "HUNG \u2014 the call did not return in 25s; "
                             "the deployed apitennis._call has no working timeout",
                             "partial": out}, headers={"Cache-Control": "no-store"})
    return JSONResponse({"date": d, **out}, headers={"Cache-Control": "no-store"})


@router.get("/api/whoami")
def _whoami():
    import threading
    return JSONResponse({"pid": os.getpid(),
                         "threads": threading.active_count(),
                         "feed_running": main._FEED_BUILD["running"],
                         "feed_report_null": main._FEED_BUILD["report"] is None},
                        headers={"Cache-Control": "no-store"})


@router.get("/api/surface/feed-status")
def _feed_status():
    return JSONResponse({"running": main._FEED_BUILD["running"], "report": main._FEED_BUILD["report"]},
                        headers={"Cache-Control": "no-store"})


@router.get("/api/tennis/settle-stale")
def _tennis_settle_stale(confirm: str = "", hours: int = 0):
    """Force-settle canceled/stuck tennis (start time past the staleness window,
    not finished) as PUSHES right now, so hanging single bets and parlay legs
    clear immediately. ?confirm=yes to run; optional &hours=N overrides the
    window for this run."""
    if confirm != "yes":
        return JSONResponse({"note": "append ?confirm=yes to run",
                             "window_hours": main.STALE_TENNIS_HOURS,
                             "tip": "use &hours=24 to be more aggressive for old stuck bets"})
    pushed = main._settle_stale_tennis(hours if hours and hours > 0 else None)
    try:
        main._settle_parlays()   # re-grade slips now that legs settled
    except Exception as e:
        return JSONResponse({"pushed": pushed, "parlay_regrade_error": str(e)})
    return JSONResponse({"pushed_as_canceled": pushed, "parlays": "re-graded",
                         "window_hours": (hours or main.STALE_TENNIS_HOURS)},
                        headers={"Cache-Control": "no-store"})


@router.get("/api/surface/reset-volume")
def _surface_reset_volume(confirm: str = ""):
    """Undo the last feed build: delete /data/surface_records.json so the app falls
    back to the committed surface_records.json (your deep Sackmann ATP file, which
    has real grass). Reports how many players load from the committed file so you
    can see what the base actually contains."""
    if confirm != "yes":
        return JSONResponse({"note": "append ?confirm=yes to wipe the volume file and "
                             "revert to the committed surface_records.json"})
    path = "/data/surface_records.json"
    existed = os.path.exists(path)
    try:
        if existed:
            os.remove(path)
    except Exception as e:
        return JSONResponse({"error": f"could not delete {path}: {e}"})
    src = main._load_surface_records()      # reloads from committed file now that /data is gone
    return JSONResponse({"deleted_volume_file": existed,
                         "now_loaded_from": src,
                         "players_loaded": len(main.SURFACE_RECORDS),
                         "note": "this is your committed base; rebuild WTA on top with "
                                 "/api/surface/build-from-feed"},
                        headers={"Cache-Control": "no-store"})
