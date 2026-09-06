from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import browser_cookie3


def _chrome_user_data_dir() -> Path:
    if sys.platform == "win32":
        local_app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        return local_app_data / "Google/Chrome/User Data"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Google/Chrome"
    config_dir = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config_dir / "google-chrome"


CHROME_DIR = _chrome_user_data_dir()


def find_chrome_binary() -> Optional[str]:
    candidates: List[Path] = []
    if sys.platform == "win32":
        for name in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            if os.environ.get(name):
                candidates.append(Path(os.environ[name]) / "Google/Chrome/Application/chrome.exe")
    elif sys.platform == "darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    for name in ("chrome", "google-chrome", "google-chrome-stable"):
        binary = shutil.which(name)
        if binary:
            return binary
    return None


def _profile_cookie_file(profile_dir: Path) -> Optional[Path]:
    # Current Chrome stores cookies under Network; older versions used the
    # profile root. Prefer the current database if both are present.
    for cookie_file in (profile_dir / "Network/Cookies", profile_dir / "Cookies"):
        if cookie_file.is_file():
            return cookie_file
    return None


@dataclass
class ChromeSessionData:
    profile: str
    login_at: Optional[str]
    login_key: Optional[str]
    userkey: Optional[str]
    tkey: Optional[str]
    cookies: Dict[str, str]


def list_chrome_profiles() -> List[str]:
    profiles: List[str] = []
    if not CHROME_DIR.exists():
        return profiles

    for path in sorted(CHROME_DIR.iterdir()):
        if not path.is_dir():
            continue
        if _profile_cookie_file(path):
            profiles.append(path.name)
    return profiles


def load_chrome_novelpia_session(profile: str) -> ChromeSessionData:
    if not profile or profile in (".", "..") or "/" in profile or "\\" in profile:
        raise ValueError("Select a Chrome profile name, such as Default or Profile 1.")
    cookie_file = _profile_cookie_file(CHROME_DIR / profile)
    if cookie_file is None:
        raise FileNotFoundError(f"Chrome profile '{profile}' does not have a Cookies DB.")

    key_file = CHROME_DIR / "Local State"
    try:
        jar = browser_cookie3.chrome(
            cookie_file=str(cookie_file),
            domain_name="novelpia.com",
            key_file=str(key_file) if key_file.is_file() else None,
        )
    except Exception as exc:
        raise RuntimeError(
            "Chrome cookies could not be read. Close Chrome completely and retry "
            "using the same operating system account that owns this profile. "
            "If Chrome's cookie encryption prevents import, use the login window "
            "in the app or enter session values manually."
        ) from exc
    cookies: Dict[str, str] = {}
    for c in jar:
        domain = c.domain.lstrip(".").lower()
        if domain != "novelpia.com" and not domain.endswith(".novelpia.com"):
            continue
        if c.is_expired():
            continue
        cookies[c.name] = c.value

    login_key = cookies.get("LOGINKEY")
    userkey = cookies.get("USERKEY")
    tkey = cookies.get("TKEY")

    return ChromeSessionData(
        profile=profile,
        # LOGINKEY is a cookie, not the LOGINAT access token. The caller must
        # refresh the imported cookie session to obtain a real login-at token.
        login_at=None,
        login_key=login_key,
        userkey=userkey,
        tkey=tkey,
        cookies=cookies,
    )
