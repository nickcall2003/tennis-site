"""
auth.py — password hashing and session helpers, standard-library only.

No external crypto dependency (bcrypt/argon2 may not be installed on the host),
so we use hashlib.pbkdf2_hmac — a sound, widely-accepted KDF at a high iteration
count with a per-user random salt. Passwords are NEVER stored or logged in
plaintext; only the derived hash is kept.

The stored value is SELF-DESCRIBING (Django-style):

    pbkdf2_sha256$<iterations>$<salt>$<hex-hash>

so the work factor can be raised later without locking existing users out —
verify reads the iteration count from the stored string. A legacy fallback
(raw hash + separate salt at the original 200k count) keeps any pre-upgrade
account working.
"""
import re
import hmac
import secrets
import hashlib
import datetime as dt

_ALGO = "sha256"
_PREFIX = "pbkdf2_sha256"
_ITERATIONS = 600_000          # OWASP 2023 guidance for PBKDF2-HMAC-SHA256
_LEGACY_ITERATIONS = 200_000   # what the first cut shipped with
SESSION_DAYS = 60

_USERNAME_RE = re.compile(r"[A-Za-z0-9_.]{3,30}")

_COMMON = {
    "password", "password1", "password123", "12345678", "123456789",
    "1234567890", "qwerty", "qwerty123", "111111", "123123", "abc12345",
    "letmein", "iloveyou", "admin", "welcome", "monkey", "dragon",
    "football", "baseball", "sunshine", "princess", "trustno1",
    "passw0rd", "test1234", "changeme",
}


def hash_password(password: str, salt: str | None = None,
                  iterations: int = _ITERATIONS) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(_ALGO, password.encode("utf-8"),
                             salt.encode("utf-8"), iterations)
    return f"{_PREFIX}${iterations}${salt}${dk.hex()}"


def verify_password(password: str, stored: str, salt: str | None = None) -> bool:
    try:
        if stored and stored.startswith(_PREFIX + "$"):
            _, iter_s, s, hexh = stored.split("$", 3)
            dk = hashlib.pbkdf2_hmac(_ALGO, password.encode("utf-8"),
                                     s.encode("utf-8"), int(iter_s))
            return hmac.compare_digest(dk.hex(), hexh)
        if salt:
            dk = hashlib.pbkdf2_hmac(_ALGO, password.encode("utf-8"),
                                     salt.encode("utf-8"), _LEGACY_ITERATIONS)
            return hmac.compare_digest(dk.hex(), stored or "")
    except Exception:
        pass
    return False


def needs_rehash(stored: str) -> bool:
    try:
        if not stored or not stored.startswith(_PREFIX + "$"):
            return True
        return int(stored.split("$", 3)[1]) < _ITERATIONS
    except Exception:
        return True


def new_token() -> str:
    return secrets.token_urlsafe(32)


def session_expiry() -> dt.datetime:
    return dt.datetime.utcnow() + dt.timedelta(days=SESSION_DAYS)


def valid_username(u: str) -> bool:
    return bool(u and _USERNAME_RE.fullmatch(u))


def username_error(u: str) -> str | None:
    if not u or len(u) < 3:
        return "Username must be at least 3 characters."
    if len(u) > 30:
        return "Username must be 30 characters or fewer."
    if not _USERNAME_RE.fullmatch(u):
        return "Username can use letters, numbers, underscore and dot only."
    return None


def password_error(p: str) -> str | None:
    if not p or len(p) < 8:
        return "Password must be at least 8 characters."
    if len(p) > 200:
        return "Password is too long."
    if p.lower() in _COMMON:
        return "That password is too common - pick something harder to guess."
    return None
