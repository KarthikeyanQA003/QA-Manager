"""
Sign-in, sessions, and project (workspace) access.

Default sign-in is email + password, all in the UI: the first account created
becomes the admin and fixes the company email domain; after that anyone with an
email on that domain can register. Passwords are stored as salted PBKDF2-SHA256
hashes, and repeated wrong passwords lock the account for a few minutes.

Optional: Google sign-in. Add a Google OAuth Client ID to data/config.json and
a "Sign in with Google" button appears; the browser's ID token is verified with
Google's tokeninfo endpoint (audience, issuer, expiry, verified email, domain).

Config lives in data/config.json (git-ignored):
    {"allowed_domains": ["cavintek.com"], "google_client_id": ""}
"""

import hashlib
import hmac
import json
import re
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

SESSION_COOKIE = "tcm_session"
SESSION_DAYS = 7
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo?id_token="
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}

AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    email TEXT PRIMARY KEY, name TEXT, picture TEXT, created_at TEXT, last_login_at TEXT,
    password_hash TEXT, is_admin INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY, email TEXT NOT NULL, created_at TEXT, expires_at TEXT
);
CREATE TABLE IF NOT EXISTS workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    app_url TEXT DEFAULT '',
    environments TEXT DEFAULT '["Test"]',
    members TEXT DEFAULT '[]',
    owner TEXT,
    created_at TEXT,
    updated_at TEXT
);
"""


class AuthError(Exception):
    """Not signed in / not allowed -- answered with 401 or 403."""

    def __init__(self, message, status=401):
        super().__init__(message)
        self.status = status


def _now():
    return datetime.now()


def _stamp(dt=None):
    return (dt or _now()).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path):
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        cfg = {}
    cfg.setdefault("google_client_id", "")
    cfg.setdefault("allowed_domains", [])
    return cfg


def save_config(path, cfg):
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Email + password accounts
# ---------------------------------------------------------------------------

PBKDF2_ITERATIONS = 240_000
MIN_PASSWORD = 8
MAX_FAILURES = 5
LOCK_MINUTES = 5
_failures = {}            # email -> (count, locked_until)
_failures_lock = threading.Lock()


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest}"


def check_password(password, stored):
    try:
        _, iterations, salt, digest = stored.split("$")
    except (AttributeError, ValueError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
    return hmac.compare_digest(candidate, digest)


def needs_setup(conn):
    """True until the first account with a password exists."""
    return not conn.execute("SELECT 1 FROM users WHERE password_hash IS NOT NULL LIMIT 1").fetchone()


def clean_email(email):
    email = (email or "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("Enter a valid email address")
    return email


def clean_password(password, confirm=None):
    if len(password or "") < MIN_PASSWORD:
        raise ValueError(f"Password must be at least {MIN_PASSWORD} characters")
    if confirm is not None and password != confirm:
        raise ValueError("The two passwords don't match")
    return password


def register(conn, cfg, name, email, password, is_admin=False):
    email = clean_email(email)
    if not domain_allowed(cfg, email):
        raise AuthError(f"Only {' / '.join('@' + d for d in cfg['allowed_domains'])} emails can register", 403)
    row = conn.execute("SELECT password_hash FROM users WHERE email=?", (email,)).fetchone()
    if row and row[0]:
        raise ValueError("An account with this email already exists -- sign in instead")
    name = (name or "").strip() or email.split("@")[0]
    conn.execute(
        """INSERT INTO users (email, name, picture, created_at, last_login_at, password_hash, is_admin)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(email) DO UPDATE SET name=excluded.name, password_hash=excluded.password_hash,
             is_admin=excluded.is_admin""",
        (email, name, "", _stamp(), _stamp(), hash_password(password), 1 if is_admin else 0))
    return email


def login(conn, email, password):
    email = (email or "").strip().lower()
    with _failures_lock:
        count, until = _failures.get(email, (0, None))
        if until and until > _now():
            mins = max(1, int((until - _now()).total_seconds() // 60) + 1)
            raise AuthError(f"Too many wrong passwords -- try again in {mins} minute(s)", 429)
    row = conn.execute("SELECT name, password_hash FROM users WHERE email=?", (email,)).fetchone()
    # Same message whether the email or the password is wrong.
    if not row or not row["password_hash"] or not check_password(password or "", row["password_hash"]):
        with _failures_lock:
            count += 1
            _failures[email] = (count, _now() + timedelta(minutes=LOCK_MINUTES) if count >= MAX_FAILURES else None)
        raise AuthError("Wrong email or password")
    with _failures_lock:
        _failures.pop(email, None)
    conn.execute("UPDATE users SET last_login_at=? WHERE email=?", (_stamp(), email))
    return email


def change_password(conn, email, current, new):
    row = conn.execute("SELECT password_hash FROM users WHERE email=?", (email,)).fetchone()
    if not row or not check_password(current or "", row[0] or ""):
        raise AuthError("Current password is wrong", 403)
    conn.execute("UPDATE users SET password_hash=? WHERE email=?", (hash_password(clean_password(new)), email))


def google_enabled(cfg):
    """Google sign-in is optional: on only when a Client ID is in config.json."""
    return bool(cfg.get("google_client_id")) and bool(cfg.get("allowed_domains"))


def clean_domains(value):
    if isinstance(value, str):
        value = value.replace(";", ",").split(",")
    out = []
    for d in value or []:
        d = d.strip().lower().lstrip("@")
        if d and "." in d and d not in out:
            out.append(d)
    return out


def email_domain(email):
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def domain_allowed(cfg, email):
    return email_domain(email) in [d.lower() for d in cfg.get("allowed_domains", [])]


# ---------------------------------------------------------------------------
# Google ID token
# ---------------------------------------------------------------------------

def verify_google_token(cfg, credential):
    """Return (email, name, picture) for a valid ID token from an allowed account."""
    if not credential or len(credential) > 8192:
        raise AuthError("Missing Google credential")
    try:
        with urllib.request.urlopen(TOKENINFO_URL + urllib.parse.quote(credential), timeout=10) as r:
            info = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError:
        raise AuthError("Google rejected the sign-in token -- try again")
    except (urllib.error.URLError, TimeoutError):
        raise AuthError("Could not reach Google to verify the sign-in (check the internet connection)", 503)

    if info.get("aud") != cfg["google_client_id"]:
        raise AuthError("This sign-in token was issued for a different app")
    if info.get("iss") not in GOOGLE_ISSUERS:
        raise AuthError("Sign-in token was not issued by Google")
    if int(info.get("exp", 0)) < int(_now().timestamp()):
        raise AuthError("Sign-in token expired -- try again")
    if str(info.get("email_verified")).lower() != "true":
        raise AuthError("Your Google email address is not verified")

    email = info.get("email", "").lower()
    if not domain_allowed(cfg, email):
        raise AuthError(f"{email} is not allowed -- sign in with your "
                        f"{' / '.join('@' + d for d in cfg['allowed_domains'])} account", 403)
    domain = email_domain(email)
    # A personal Google account can carry a verified non-Gmail address, so for a
    # company domain also require the account to belong to that Workspace (hd).
    if domain != "gmail.com" and info.get("hd", "").lower() != domain:
        raise AuthError(f"{email} is not a {domain} Google Workspace account", 403)
    return email, info.get("name") or email.split("@")[0], info.get("picture", "")


# ---------------------------------------------------------------------------
# Users and sessions
# ---------------------------------------------------------------------------

def upsert_user(conn, email, name, picture):
    conn.execute(
        """INSERT INTO users (email, name, picture, created_at, last_login_at) VALUES (?,?,?,?,?)
           ON CONFLICT(email) DO UPDATE SET name=excluded.name, picture=excluded.picture,
             last_login_at=excluded.last_login_at""",
        (email, name, picture, _stamp(), _stamp()))


def create_session(conn, email):
    token = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_stamp(),))
    conn.execute("INSERT INTO sessions (token, email, created_at, expires_at) VALUES (?,?,?,?)",
                 (token, email, _stamp(), _stamp(_now() + timedelta(days=SESSION_DAYS))))
    return token


def session_user(conn, token):
    if not token:
        return None
    row = conn.execute(
        """SELECT u.email, u.name, u.picture, u.is_admin FROM sessions s JOIN users u ON u.email = s.email
           WHERE s.token = ? AND s.expires_at > ?""", (token, _stamp())).fetchone()
    return dict(row) if row else None


def end_session(conn, token):
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def cookie_header(token, clear=False):
    if clear:
        return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
    return (f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={SESSION_DAYS * 86400}")


def token_from_cookie(header):
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == SESSION_COOKIE:
            return value
    return None


# ---------------------------------------------------------------------------
# Workspaces (the Jira-style "projects" a user signs in to)
# ---------------------------------------------------------------------------

def ws_row(row):
    w = dict(row)
    w["environments"] = json.loads(w.get("environments") or "[]")
    w["members"] = json.loads(w.get("members") or "[]")
    return w


def is_admin(conn, email):
    row = conn.execute("SELECT is_admin FROM users WHERE email = ?", (email,)).fetchone()
    return bool(row and row[0])


def can_access(ws, email, admin=False):
    # Admins see every project; others see the ones they own or are a member of.
    return admin or ws["owner"] == email or email in ws["members"] or not ws["owner"]


def user_workspaces(conn, email):
    admin = is_admin(conn, email)
    return [w for w in (ws_row(r) for r in conn.execute("SELECT * FROM workspaces ORDER BY name"))
            if can_access(w, email, admin)]


def get_workspace(conn, ws_id, email):
    row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (ws_id,)).fetchone()
    if not row:
        raise AuthError("Project not found", 404)
    ws = ws_row(row)
    if not can_access(ws, email, is_admin(conn, email)):
        raise AuthError("You are not a member of this project", 403)
    if not ws["owner"]:
        # Projects migrated from before sign-in existed: the first person to
        # open one becomes its owner.
        conn.execute("UPDATE workspaces SET owner = ? WHERE id = ? AND owner IS NULL", (email, ws_id))
        ws["owner"] = email
    return ws


def clean_workspace_fields(cfg, data, owner):
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("Project name is required")
    key = (data.get("key") or "").strip().upper()
    if not key:
        key = "".join(w[0] for w in name.split() if w[:1].isalnum())[:4].upper() or name[:3].upper()
    if not key.isalnum() or len(key) > 10:
        raise ValueError("Project key must be 1-10 letters or digits (e.g. CFL)")
    url = (data.get("app_url") or "").strip()
    if url and not url.lower().startswith(("http://", "https://")):
        raise ValueError("Application URL must start with http:// or https://")
    envs = data.get("environments") or []
    if isinstance(envs, str):
        envs = envs.split(",")
    envs = [e.strip() for e in envs if e.strip()] or ["Test"]
    members = data.get("members") or []
    if isinstance(members, str):
        members = members.replace(";", ",").replace("\n", ",").split(",")
    members = sorted({m.strip().lower() for m in members if m.strip()} - {owner})
    # Before sign-in is configured (developer sign-in only) there is no domain to check.
    domains = cfg.get("allowed_domains") or []
    bad = [m for m in members if "@" not in m or (domains and not domain_allowed(cfg, m))]
    if bad:
        allowed = " / ".join("@" + d for d in domains) or "valid email"
        raise ValueError(f"Team members must be {allowed} addresses: {', '.join(bad)}")
    return {"key": key, "name": name, "description": (data.get("description") or "").strip(),
            "app_url": url, "environments": json.dumps(envs), "members": json.dumps(members)}
