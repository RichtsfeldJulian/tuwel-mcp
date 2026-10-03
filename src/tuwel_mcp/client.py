"""Thin async client for the Moodle REST web service API."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from .auth import AuthError, Session

# Moodle error codes meaning the token is no longer usable.
TOKEN_ERRORS = {"invalidtoken", "accessexception", "invalidlogin", "servicerequireslogin"}


class MoodleError(Exception):
    def __init__(self, message: str, errorcode: str | None = None):
        super().__init__(message)
        self.errorcode = errorcode


def _flatten(params: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Encode nested params the way Moodle expects: a[0][b]=1."""
    out: dict[str, str] = {}
    for key, value in params.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict):
            out.update(_flatten(value, name))
        elif isinstance(value, (list, tuple)):
            out.update(_flatten(dict(enumerate(value)), name))
        elif isinstance(value, bool):
            out[name] = "1" if value else "0"
        elif value is not None:
            out[name] = str(value)
    return out


class MoodleClient:
    def __init__(self, session: Session):
        self.session = session
        self.base = session.site_url.rstrip("/")
        self.http = httpx.AsyncClient(timeout=60, follow_redirects=True)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def call(self, function: str, **params: Any) -> Any:
        data = _flatten(params)
        data.update(
            wstoken=self.session.token,
            wsfunction=function,
            moodlewsrestformat="json",
            moodlewssettingfilter="true",
            moodlewssettingfileurl="true",
        )
        resp = await self.http.post(f"{self.base}/webservice/rest/server.php", data=data)
        resp.raise_for_status()
        result = resp.json()
        if isinstance(result, dict) and "exception" in result:
            code = result.get("errorcode")
            if code in TOKEN_ERRORS:
                raise AuthError(f"TUWEL token rejected ({code}): {result.get('message')}")
            raise MoodleError(f"{function}: {result.get('message')}", code)
        return result

    # ------------------------------------------------------------------ files

    def file_url(self, url: str) -> str:
        """Turn a Moodle file URL into a token-authenticated download URL."""
        if "/webservice/pluginfile.php" not in url and "/pluginfile.php" in url:
            url = url.replace("/pluginfile.php", "/webservice/pluginfile.php", 1)
        sep = "&" if "?" in url else "?"
        if "token=" not in url:
            url = f"{url}{sep}token={self.session.token}"
        return url

    async def download(self, url: str, dest_dir: Path, filename: str | None = None) -> Path:
        if not url.startswith(self.base):
            raise MoodleError(f"Refusing to download non-TUWEL URL: {url}")
        dest_dir.mkdir(parents=True, exist_ok=True)
        if not filename:
            filename = unquote(Path(urlparse(url).path).name) or "download"
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename).strip() or "download"
        target = dest_dir / filename
        async with self.http.stream("GET", self.file_url(url)) as resp:
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            if "application/json" in ctype:
                body = (await resp.aread()).decode(errors="replace")
                if "invalidtoken" in body:
                    raise AuthError("TUWEL token rejected while downloading a file.")
                raise MoodleError(f"Download failed: {body[:300]}")
            with target.open("wb") as fh:
                async for chunk in resp.aiter_bytes():
                    fh.write(chunk)
        return target
