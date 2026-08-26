"""Login for the district dashboard.

The dashboard shows real names and real danger signs. It was public. This puts
it behind a password using only the standard library: scrypt for hashing, an
HMAC-signed cookie for the session. No new dependencies, nothing to rotate but
one secret.

Add an account:  .venv/bin/python -m app.auth add felix
"""

import base64
import hashlib
import hmac
import os
import secrets
import time

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text as sql

from app.config import settings
from app.db import SessionLocal

COOKIE = "alaafei_session"
MAX_AGE = 60 * 60 * 12  # a working day


def _secret() -> bytes:
    raw = os.environ.get("ALAAFEI_SESSION_SECRET") or getattr(
        settings, "whatsapp_app_secret", ""
    )
    if not raw:
        raise RuntimeError("No session secret configured")
    return raw.encode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return salt.hex() + ":" + digest.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split(":", 1)
    except ValueError:
        return False
    digest = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt_hex), n=16384, r=8, p=1, dklen=32
    )
    return hmac.compare_digest(digest.hex(), digest_hex)


def _sign(username: str, expires: int) -> str:
    payload = f"{username}|{expires}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()


def _unsign(token: str) -> str | None:
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        username, expires, sig = raw.rsplit("|", 2)
    except Exception:
        return None
    expected = hmac.new(
        _secret(), f"{username}|{expires}".encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    if int(expires) < int(time.time()):
        return None
    return username


async def current_user(request: Request) -> str | None:
    token = request.cookies.get(COOKIE)
    return _unsign(token) if token else None


async def require_user(request: Request) -> str:
    """Redirect a person to the login page. Answer a script with a 401."""
    user = await current_user(request)
    if user:
        return user
    if request.url.path.endswith("/data"):
        raise HTTPException(status_code=401, detail="Sign in required")
    raise HTTPException(
        status_code=307, headers={"Location": "/login"}, detail="Sign in required"
    )


router = APIRouter()

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Alaafei</title><style>
body{font-family:system-ui,sans-serif;background:#f6f4ef;margin:0;
display:flex;min-height:100vh;align-items:center;justify-content:center}
form{background:#fff;padding:32px;border-radius:12px;width:300px;
box-shadow:0 1px 3px rgba(0,0,0,.08)}
h1{font-size:18px;margin:0 0 4px}p{color:#666;font-size:13px;margin:0 0 20px}
label{display:block;font-size:12px;color:#444;margin-bottom:4px}
input{width:100%;padding:9px;margin-bottom:14px;border:1px solid #ddd;
border-radius:6px;font-size:14px;box-sizing:border-box}
button{width:100%;padding:10px;background:#1a6b4a;color:#fff;border:0;
border-radius:6px;font-size:14px;cursor:pointer}
.err{color:#b3261e;font-size:13px;margin-bottom:12px}
</style></head><body><form method="post" action="/login">
<h1>Alaafei</h1><p>Savelugu district referral watch</p>
__ERROR__
<label>Name</label><input name="username" autocapitalize="none" autofocus>
<label>Password</label><input name="password" type="password">
<button type="submit">Sign in</button></form></body></html>"""


@router.get("/login")
async def login_page():
    return HTMLResponse(_PAGE.replace("__ERROR__", ""))


@router.post("/login")
async def login(username: str = Form(...), password: str = Form(...)):
    async with SessionLocal() as session:
        row = (
            await session.execute(
                sql("SELECT password_hash FROM users WHERE username = :u"),
                {"u": username.strip().lower()},
            )
        ).first()
    if row is None or not verify_password(password, row[0]):
        return HTMLResponse(
            _PAGE.replace("__ERROR__", '<div class="err">Wrong name or password.</div>'),
            status_code=401,
        )
    expires = int(time.time()) + MAX_AGE
    token = _sign(username.strip().lower(), expires)
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(
        COOKIE, token, max_age=MAX_AGE, httponly=True, samesite="lax"
    )
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE)
    return response


if __name__ == "__main__":
    import asyncio
    import getpass
    import sys

    async def _add(username: str) -> None:
        password = getpass.getpass("Password: ")
        if len(password) < 8:
            print("Use at least 8 characters.")
            return
        if password != getpass.getpass("Again: "):
            print("They did not match.")
            return
        async with SessionLocal() as session:
            await session.execute(
                sql(
                    "INSERT INTO users (username, password_hash) VALUES (:u, :p) "
                    "ON CONFLICT(username) DO UPDATE SET password_hash = :p"
                ),
                {"u": username.strip().lower(), "p": hash_password(password)},
            )
            await session.commit()
        print(f"Account ready: {username.strip().lower()}")

    if len(sys.argv) == 3 and sys.argv[1] == "add":
        asyncio.run(_add(sys.argv[2]))
    else:
        print("Usage: python -m app.auth add <username>")
