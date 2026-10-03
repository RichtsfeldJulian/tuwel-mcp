"""Configuration: site URL, local paths and credential sources."""

from __future__ import annotations

import os
from pathlib import Path

SITE_URL = os.environ.get("TUWEL_URL", "https://tuwel.tuwien.ac.at").rstrip("/")
SERVICE = "moodle_mobile_app"
URL_SCHEME = "moodlemobile"
KEYRING_SERVICE = "tuwel-mcp"


def data_dir() -> Path:
    """Directory holding the token file and downloads (override with TUWEL_MCP_HOME)."""
    override = os.environ.get("TUWEL_MCP_HOME")
    if override:
        path = Path(override)
    elif os.name == "nt":
        path = Path(os.environ.get("APPDATA", Path.home())) / "tuwel-mcp"
    else:
        path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "tuwel-mcp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def token_file() -> Path:
    return data_dir() / "session.json"


def download_dir() -> Path:
    override = os.environ.get("TUWEL_DOWNLOAD_DIR")
    path = Path(override) if override else data_dir() / "downloads"
    path.mkdir(parents=True, exist_ok=True)
    return path
