from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import browser_cookie3


CHROME_DIR = Path.home() / "Library/Application Support/Google/Chrome"


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
        if (path / "Cookies").exists():
            profiles.append(path.name)
    return profiles


def load_chrome_novelpia_session(profile: str) -> ChromeSessionData:
    cookie_file = CHROME_DIR / profile / "Cookies"
    if not cookie_file.exists():
        raise FileNotFoundError(f"Chrome profile '{profile}' does not have a Cookies DB.")

    jar = browser_cookie3.chrome(cookie_file=str(cookie_file), domain_name="novelpia.com")
    cookies: Dict[str, str] = {}
    for c in jar:
        if "novelpia" not in c.domain:
            continue
        cookies[c.name] = c.value

    login_key = cookies.get("LOGINKEY")
    userkey = cookies.get("USERKEY")
    tkey = cookies.get("TKEY")

    return ChromeSessionData(
        profile=profile,
        # Novelpia uses a separate login-at header; LOGINKEY is the closest browser-side value
        # we can import automatically, so we surface it as a best-effort prefill.
        login_at=login_key,
        login_key=login_key,
        userkey=userkey,
        tkey=tkey,
        cookies=cookies,
    )
