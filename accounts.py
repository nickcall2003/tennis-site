"""
accounts.py — user accounts, sessions, the synced bet log, and storage health.

Split out of main.py to keep that file small (and downloadable on mobile) and to
keep the security-sensitive code in one isolated place. Everything here mounts
under the same app via an APIRouter, so the public URLs are unchanged.

Security model recap:
  * passwords -> PBKDF2-HMAC-SHA256, self-describing hash, never plaintext (auth.py)
  * sessions  -> random opaque tokens stored in the DB (revocable)
  * brute force-> per-username login lockout + per-IP signup cap (in-memory)
  * recovery  -> a one-time recovery code shown at signup (hashed, email-free reset)
"""
import datetime as dt

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from db import SessionLocal

router = APIRouter()


def _make_recovery_code():
    """A human-friendly one-time code, e.g. K7Q2-9XMP-4RTW-8BVC. Ambiguous
    characters (0/O/1/I/L) are excluded so it's easy to copy by hand."""
    import secrets
    alpha = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "-".join("".join(secrets.choice(alpha) for _ in range(4)) for _ in range(4))


def _canon_code(s):
    return (s or "").upper().replace("-", "").replace(" ", "")


# ===================== Accounts (optional login) =====================
# In-memory brute-force throttles. They reset on redeploy, which is fine — the
# goal is to slow online password guessing, not to be a durable audit log.
_login_fails = {}        # username -> [recent failure timestamps]
_signup_ips = {}         # ip -> [recent signup timestamps]
_LOGIN_WINDOW = 900      # 15 min
_LOGIN_MAX = 8           # lock a username after this many fails in the window
_SIGNUP_WINDOW = 3600
_SIGNUP_MAX = 5          # new accounts per IP per hour


def _client_ip(request):
    try:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
        return request.client.host if request.client else "?"
    except Exception:
        return "?"


def _recent(store, key, window):
    import time as _t
    now = _t.time()
    arr = [t for t in store.get(key, []) if now - t < window]
    store[key] = arr
    return arr


def _login_locked(username):
    return len(_recent(_login_fails, username, _LOGIN_WINDOW)) >= _LOGIN_MAX


def _login_fail(username):
    import time as _t
    _login_fails.setdefault(username, []).append(_t.time())


def _login_clear(username):
    _login_fails.pop(username, None)


def _bearer(authorization):
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return authorization.strip() or None


def _user_from_token(db, token):
    """Resolve a session token to a User, or None. Expired sessions are deleted
    lazily on access."""
    if not token:
        return None
    from models import AuthSession, User
    s = db.query(AuthSession).filter(AuthSession.token == token).first()
    if not s:
        return None
    if s.expires_at and s.expires_at < dt.datetime.utcnow():
        try:
            db.delete(s); db.commit()
        except Exception:
            db.rollback()
        return None
    u = db.query(User).filter(User.id == s.user_id).first()
    return u if u else None


@router.post("/api/auth/signup")
def auth_signup(payload: dict, request: Request):
    import auth as _auth
    from models import User, AuthSession
    ip = _client_ip(request)
    if len(_recent(_signup_ips, ip, _SIGNUP_WINDOW)) >= _SIGNUP_MAX:
        return JSONResponse({"error": "Too many new accounts from here. Try again later."},
                            status_code=429)
    username = (payload or {}).get("username", "").strip()
    password = (payload or {}).get("password", "")
    email = ((payload or {}).get("email") or "").strip() or None
    err = _auth.username_error(username) or _auth.password_error(password)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    with SessionLocal() as db:
        if db.query(User).filter(User.username == username).first():
            return JSONResponse({"error": "That username is taken."}, status_code=409)
        u = User(username=username, email=email,
                 pw_hash=_auth.hash_password(password), pw_salt="")
        db.add(u); db.commit(); db.refresh(u)
        tok = _auth.new_token()
        db.add(AuthSession(token=tok, user_id=u.id, expires_at=_auth.session_expiry()))
        # one-time recovery code (stored hashed; shown to the user exactly once)
        from models import RecoveryCode
        recovery = _make_recovery_code()
        db.add(RecoveryCode(user_id=u.id, code_hash=_auth.hash_password(_canon_code(recovery))))
        db.commit()
    import time as _t
    _signup_ips.setdefault(ip, []).append(_t.time())
    return {"token": tok, "username": username, "recovery_code": recovery}


@router.post("/api/auth/login")
def auth_login(payload: dict):
    import auth as _auth
    from models import User, AuthSession
    username = (payload or {}).get("username", "").strip()
    password = (payload or {}).get("password", "")
    if _login_locked(username):
        return JSONResponse({"error": "Too many attempts. Try again in a few minutes."},
                            status_code=429)
    with SessionLocal() as db:
        u = db.query(User).filter(User.username == username).first()
        ok = bool(u) and _auth.verify_password(password, u.pw_hash, u.pw_salt)
        if not ok:
            _login_fail(username)
            return JSONResponse({"error": "Wrong username or password."}, status_code=401)
        _login_clear(username)
        # transparently upgrade an old/weak hash now that we have the password
        if _auth.needs_rehash(u.pw_hash):
            try:
                u.pw_hash = _auth.hash_password(password); u.pw_salt = ""
                db.commit()
            except Exception:
                db.rollback()
        tok = _auth.new_token()
        db.add(AuthSession(token=tok, user_id=u.id, expires_at=_auth.session_expiry()))
        db.commit()
        return {"token": tok, "username": u.username}


@router.post("/api/auth/password")
def auth_change_password(payload: dict, authorization: str | None = Header(None)):
    """Change password while signed in (requires the current one). Other active
    sessions are revoked so a changed password actually locks others out."""
    import auth as _auth
    from models import AuthSession
    with SessionLocal() as db:
        u = _user_from_token(db, _bearer(authorization))
        if not u:
            return JSONResponse({"error": "Not signed in."}, status_code=401)
        cur = (payload or {}).get("current", "")
        new = (payload or {}).get("new", "")
        if not _auth.verify_password(cur, u.pw_hash, u.pw_salt):
            return JSONResponse({"error": "Current password is wrong."}, status_code=403)
        perr = _auth.password_error(new)
        if perr:
            return JSONResponse({"error": perr}, status_code=400)
        u.pw_hash = _auth.hash_password(new); u.pw_salt = ""
        keep = _bearer(authorization)
        db.query(AuthSession).filter(AuthSession.user_id == u.id,
                                     AuthSession.token != keep).delete()
        db.commit()
        return {"ok": True}


@router.post("/api/auth/forgot")
def auth_forgot(payload: dict):
    """Reset a forgotten password using the one-time recovery code shown at
    signup. No email required. On success the password is reset, the recovery
    code is rotated (a fresh one is returned), and all sessions are revoked."""
    import auth as _auth
    from models import User, AuthSession, RecoveryCode
    username = (payload or {}).get("username", "").strip()
    code = _canon_code((payload or {}).get("code", ""))
    newpw = (payload or {}).get("new", "")
    if _login_locked("forgot:" + username):
        return JSONResponse({"error": "Too many attempts. Try again later."}, status_code=429)
    perr = _auth.password_error(newpw)
    if perr:
        return JSONResponse({"error": perr}, status_code=400)
    with SessionLocal() as db:
        u = db.query(User).filter(User.username == username).first()
        rc = (db.query(RecoveryCode).filter(RecoveryCode.user_id == u.id).first()
              if u else None)
        ok = bool(rc) and code and _auth.verify_password(code, rc.code_hash)
        if not ok:
            _login_fail("forgot:" + username)
            return JSONResponse({"error": "Wrong username or recovery code."},
                                status_code=401)
        _login_clear("forgot:" + username)
        u.pw_hash = _auth.hash_password(newpw); u.pw_salt = ""
        new_code = _make_recovery_code()
        rc.code_hash = _auth.hash_password(_canon_code(new_code))
        db.query(AuthSession).filter(AuthSession.user_id == u.id).delete()
        db.commit()
        return {"ok": True, "recovery_code": new_code}


@router.post("/api/auth/logout")
def auth_logout(authorization: str | None = Header(None)):
    from models import AuthSession
    tok = _bearer(authorization)
    if tok:
        with SessionLocal() as db:
            db.query(AuthSession).filter(AuthSession.token == tok).delete()
            db.commit()
    return {"ok": True}


@router.get("/api/auth/me")
def auth_me(authorization: str | None = Header(None)):
    with SessionLocal() as db:
        u = _user_from_token(db, _bearer(authorization))
        if not u:
            return JSONResponse({"error": "Not signed in."}, status_code=401)
        return {"username": u.username, "email": u.email}


def _bet_json(b):
    return {"id": b.id, "date": b.date, "sport": b.sport, "desc": b.descr,
            "odds": b.odds, "stake": b.stake, "book": b.book,
            "closing": b.closing, "result": b.result}


@router.get("/api/bets")
def bets_list(authorization: str | None = Header(None)):
    from models import UserBet
    with SessionLocal() as db:
        u = _user_from_token(db, _bearer(authorization))
        if not u:
            return JSONResponse({"error": "Not signed in."}, status_code=401)
        rows = (db.query(UserBet).filter(UserBet.user_id == u.id)
                  .order_by(UserBet.date.desc(), UserBet.id.desc()).all())
        return {"bets": [_bet_json(b) for b in rows]}


@router.post("/api/bets")
def bets_add(payload: dict, authorization: str | None = Header(None)):
    from models import UserBet
    with SessionLocal() as db:
        u = _user_from_token(db, _bearer(authorization))
        if not u:
            return JSONResponse({"error": "Not signed in."}, status_code=401)
        p = payload or {}
        try:
            b = UserBet(user_id=u.id, date=str(p.get("date") or "")[:12],
                        sport=str(p.get("sport") or "other")[:12],
                        descr=str(p.get("desc") or "")[:200],
                        odds=int(p.get("odds")), stake=float(p.get("stake")),
                        book=(str(p.get("book"))[:40] if p.get("book") else None),
                        closing=(int(p["closing"]) if p.get("closing") not in (None, "") else None),
                        result=str(p.get("result") or "pending")[:10])
        except (TypeError, ValueError):
            return JSONResponse({"error": "Bad bet fields."}, status_code=400)
        db.add(b); db.commit(); db.refresh(b)
        return _bet_json(b)


@router.patch("/api/bets/{bet_id}")
def bets_update(bet_id: int, payload: dict, authorization: str | None = Header(None)):
    from models import UserBet
    with SessionLocal() as db:
        u = _user_from_token(db, _bearer(authorization))
        if not u:
            return JSONResponse({"error": "Not signed in."}, status_code=401)
        b = db.query(UserBet).filter(UserBet.id == bet_id, UserBet.user_id == u.id).first()
        if not b:
            return JSONResponse({"error": "Not found."}, status_code=404)
        p = payload or {}
        if "result" in p:
            b.result = str(p.get("result") or "pending")[:10]
        if "closing" in p:
            b.closing = int(p["closing"]) if p.get("closing") not in (None, "") else None
        db.commit()
        return _bet_json(b)


@router.delete("/api/bets/{bet_id}")
def bets_delete(bet_id: int, authorization: str | None = Header(None)):
    from models import UserBet
    with SessionLocal() as db:
        u = _user_from_token(db, _bearer(authorization))
        if not u:
            return JSONResponse({"error": "Not signed in."}, status_code=401)
        db.query(UserBet).filter(UserBet.id == bet_id, UserBet.user_id == u.id).delete()
        db.commit()
        return {"ok": True}


@router.get("/api/health/storage")
def health_storage():
    """Tells you whether your data (accounts, bets, stats) will SURVIVE a
    redeploy. The single most important thing to verify before inviting users."""
    import os as _os
    from db import DATABASE_URL
    from models import User
    is_pg = DATABASE_URL.startswith("postgresql")
    info = {"backend": "postgres" if is_pg else "sqlite"}
    if is_pg:
        info["durable"] = True
        info["detail"] = ("External Postgres \u2014 persists independently of the "
                          "app container. This is the safest setup.")
    else:
        path = DATABASE_URL.replace("sqlite:///", "")
        parent = _os.path.dirname(path) or "."
        mounted = False
        try:
            mounted = _os.path.ismount(parent) or _os.path.ismount("/data")
        except Exception:
            pass
        info["db_path"] = path
        info["on_mounted_volume"] = mounted
        info["durable"] = mounted
        info["detail"] = ("SQLite on a mounted volume \u2014 survives redeploys."
                          if mounted else
                          "DANGER: SQLite on EPHEMERAL disk \u2014 accounts and bets are "
                          "ERASED on every redeploy. Attach a Railway Volume mounted at "
                          "/data, or set DATABASE_URL to a managed Postgres.")
    try:
        with SessionLocal() as db:
            info["users"] = db.query(User).count()
    except Exception as e:
        info["users_error"] = str(e)
    return info

