from pathlib import Path
import sys

# ----------------------------
# Constants
# ----------------------------

# Resolve the application root directory:
# - Frozen (PyInstaller): directory containing the .exe
# - Source: project root (parent of src/)
if getattr(sys, 'frozen', False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent.parent

BASE_URL = "https://global.novelpia.com"
API_BASE = "https://api-global.novelpia.com"
IMG_BASE_HTTPS = "https:"
HTTP_LOG = False 
CONFIG_PATH = APP_DIR / ".api.json"

SESSION_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "origin": BASE_URL,
    "referer": f"{BASE_URL}/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0"
    ),
    "x-requested-with": "XMLHttpRequest",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "sec-fetch-dest": "empty",
    "sec-ch-ua": '"Not)A;Brand";v="8", "Chromium";v="147", "Microsoft Edge";v="147"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}
