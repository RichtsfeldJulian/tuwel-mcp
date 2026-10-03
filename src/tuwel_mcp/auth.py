"""Authentication against TUWEL (Moodle) via TU Wien SSO + TOTP.

TUWEL uses SAML SSO (SimpleSAMLphp IdP at idp.zid.tuwien.ac.at) where username,
password and the 6-digit TOTP code are submitted in a single form. We drive the
exact flow the official Moodle mobile app uses:

    /admin/tool/mobile/launch.php  ->  Moodle login page  ->  SAML IdP form
    -> SAML assertion posted back to Moodle  ->  launch.php
    -> redirect to  moodlemobile://token=BASE64(signature:::token:::privatetoken)

The resulting web-service token is long-lived (weeks to months), so the 2FA code
is only needed when the token is first created or has expired.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from . import config

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 tuwel-mcp"
)
TOKEN_RE = re.compile(re.escape(config.URL_SCHEME) + r"://token=([A-Za-z0-9+/=_-]+)")


class AuthError(Exception):
    """Login failed or no valid session is available."""


# --------------------------------------------------------------------------- #
# Session (token) storage
# --------------------------------------------------------------------------- #


@dataclass
class Session:
    token: str
    privatetoken: str | None = None
    site_url: str = config.SITE_URL
    userid: int | None = None
    username: str | None = None
    fullname: str | None = None
    created: float = field(default_factory=time.time)

    def save(self) -> None:
        path = config.token_file()
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        if os.name != "nt":
            os.chmod(path, 0o600)

    @classmethod
    def load(cls) -> Session | None:
        env_token = os.environ.get("TUWEL_TOKEN")
        if env_token:
            return cls(token=env_token)
        path = config.token_file()
        if not path.exists():
            return None
        try:
            return cls(**json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def clear() -> None:
        config.token_file().unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Credential storage (env vars take precedence over the OS keyring)
# --------------------------------------------------------------------------- #

_CRED_ENV = {
    "username": "TUWEL_USERNAME",
    "password": "TUWEL_PASSWORD",
    "totp_secret": "TUWEL_TOTP_SECRET",
}


def get_credential(name: str) -> str | None:
    value = os.environ.get(_CRED_ENV[name])
    if value:
        return value
    try:
        import keyring

        return keyring.get_password(config.KEYRING_SERVICE, name)
    except Exception:
        return None


def set_credential(name: str, value: str | None) -> None:
    import keyring

    if value:
        keyring.set_password(config.KEYRING_SERVICE, name, value)
    else:
        try:
            keyring.delete_password(config.KEYRING_SERVICE, name)
        except Exception:
            pass


def current_totp(secret: str) -> str:
    """Generate the current TOTP code; waits for a fresh window if the current one is about to expire."""
    import pyotp

    totp = pyotp.parse_uri(secret) if secret.startswith("otpauth://") else pyotp.TOTP(
        secret.replace(" ", "").upper()
    )
    remaining = totp.interval - (time.time() % totp.interval)
    if remaining < 4:
        time.sleep(remaining + 0.5)
    return totp.now()


# --------------------------------------------------------------------------- #
# SSO login flow
# --------------------------------------------------------------------------- #


def _decode_token(blob: str, passport: str, site_url: str) -> tuple[str, str | None]:
    padded = blob + "=" * (-len(blob) % 4)
    decoded = base64.b64decode(padded).decode("utf-8")
    parts = decoded.split(":::")
    if len(parts) < 2:
        raise AuthError("Unexpected token format returned by Moodle.")
    expected = hashlib.md5((site_url + passport).encode()).hexdigest()
    if parts[0] != expected:
        raise AuthError("Token signature mismatch - refusing token (possible tampering).")
    return parts[1], parts[2] if len(parts) > 2 else None


def _form_fields(form) -> dict[str, str]:
    data: dict[str, str] = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if name and inp.get("type", "text") not in ("submit", "button", "image"):
            data[name] = inp.get("value", "")
    return data


def _page_error(soup: BeautifulSoup) -> str:
    for selector in (".alert-danger", ".alert-error", ".error", "#error", ".alert"):
        el = soup.select_one(selector)
        if el and el.get_text(strip=True):
            return " ".join(el.get_text(" ", strip=True).split())[:300]
    return ""


class _Flow:
    def __init__(self, site_url: str):
        self.site_url = site_url
        self.passport = f"{random.random() * 1000:.6f}"
        self.client = httpx.Client(
            follow_redirects=False,
            timeout=30,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "de,en;q=0.8"},
        )
        self.token_blob: str | None = None

    def _check_token(self, text: str | None) -> bool:
        if text:
            m = TOKEN_RE.search(text)
            if m:
                self.token_blob = m.group(1)
                return True
        return False

    def request(self, method: str, url: str, **kw) -> httpx.Response | None:
        """Perform a request and follow redirects manually, stopping at the app-scheme redirect."""
        for _ in range(20):
            resp = self.client.request(method, url, **kw)
            if resp.is_redirect:
                location = resp.headers.get("location", "")
                if self._check_token(location):
                    return None
                url = urljoin(str(resp.url), location)
                method, kw = "GET", {}
                continue
            if self._check_token(resp.text):
                return None
            return resp
        raise AuthError("Too many redirects during login.")

    def submit_auto_forms(self, resp: httpx.Response | None) -> httpx.Response | None:
        """Submit SAML auto-post forms (SAMLRequest/SAMLResponse) until a regular page is reached."""
        for _ in range(5):
            if resp is None:
                return None
            soup = BeautifulSoup(resp.content, "html.parser")
            form = next(
                (
                    f
                    for f in soup.find_all("form")
                    if f.find("input", attrs={"name": re.compile("^SAML(Response|Request)$")})
                ),
                None,
            )
            if form is None:
                return resp
            action = urljoin(str(resp.url), form.get("action") or str(resp.url))
            resp = self.request("POST", action, data=_form_fields(form))
        return resp


def login(
    username: str,
    password: str,
    totp_code: str,
    site_url: str = config.SITE_URL,
) -> Session:
    """Run the full SSO login and return a new Session (not yet saved)."""
    flow = _Flow(site_url)
    launch = (
        f"{site_url}/admin/tool/mobile/launch.php?service={config.SERVICE}"
        f"&passport={flow.passport}&urlscheme={config.URL_SCHEME}"
    )
    try:
        # 1. launch.php -> Moodle login page with the SSO button
        resp = flow.request("GET", launch)
        if resp is not None:
            soup = BeautifulSoup(resp.content, "html.parser")
            link = soup.find("a", href=re.compile(r"auth/saml2/login\.php"))
            if link is None:
                raise AuthError(f"Could not find the TU Wien SSO login link on {resp.url}")
            # 2. Follow SSO link -> IdP login form
            resp = flow.submit_auto_forms(flow.request("GET", urljoin(str(resp.url), link["href"])))

        if resp is not None:
            soup = BeautifulSoup(resp.content, "html.parser")
            form = soup.find("form", id="f") or next(
                (f for f in soup.find_all("form") if f.find("input", attrs={"name": "password"})), None
            )
            if form is None:
                raise AuthError(f"Unexpected page during login (no login form): {resp.url}")
            data = _form_fields(form)
            data.update({"username": username, "password": password, "totp": totp_code})
            action = urljoin(str(resp.url), form.get("action") or str(resp.url))
            # 3. Submit credentials + TOTP, then relay the SAML assertion back to Moodle
            resp = flow.submit_auto_forms(flow.request("POST", action, data=data))

        if resp is not None and flow.token_blob is None:
            soup = BeautifulSoup(resp.content, "html.parser")
            if soup.find("input", attrs={"name": "password"}):
                msg = _page_error(soup) or "Invalid username, password or 2FA code."
                raise AuthError(f"Login rejected by TU Wien IdP: {msg}")
            if "tool/mfa" in str(resp.url):
                raise AuthError("Moodle requested an additional MFA step that is not supported headlessly.")
            # Logged in but launch.php was not reached yet - request it directly.
            resp = flow.request("GET", launch)

        if flow.token_blob is None:
            where = resp.url if resp is not None else "?"
            raise AuthError(f"Login finished without receiving a token (ended at {where}).")

        token, private = _decode_token(flow.token_blob, flow.passport, site_url)
        return Session(token=token, privatetoken=private, site_url=site_url, username=username)
    except httpx.HTTPError as exc:
        raise AuthError(f"Network error during login: {exc}") from exc
    finally:
        flow.client.close()


def session_from_launch_url(value: str, passport: str | None, site_url: str = config.SITE_URL) -> Session:
    """Build a session from a manually copied moodlemobile://token=... URL or a raw token."""
    value = value.strip()
    m = TOKEN_RE.search(value)
    if m:
        if passport is None:
            raise AuthError("A passport is required to verify a moodlemobile:// token URL.")
        token, private = _decode_token(m.group(1), passport, site_url)
        return Session(token=token, privatetoken=private, site_url=site_url)
    if re.fullmatch(r"[0-9a-f]{32}", value):
        return Session(token=value, site_url=site_url)
    raise AuthError("Expected a moodlemobile://token=... URL or a 32-character web service token.")


def login_with_stored_credentials(totp_code: str | None = None) -> Session:
    """Log in using credentials from env vars / keyring. totp_code overrides the stored TOTP secret."""
    username = get_credential("username")
    password = get_credential("password")
    if not username or not password:
        raise AuthError(
            "No stored TUWEL credentials. Run `tuwel-mcp login` once in a terminal "
            "(or set TUWEL_USERNAME / TUWEL_PASSWORD)."
        )
    if not totp_code:
        secret = get_credential("totp_secret")
        if not secret:
            raise AuthError(
                "A 2FA code is required: no TOTP secret is stored. Ask the user for the current "
                "6-digit code from their authenticator app and pass it as totp_code."
            )
        totp_code = current_totp(secret)
    return login(username, password, totp_code.strip())
