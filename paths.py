"""Where the app lives on disk. Set APP_DATA to keep runtime files off the code tree (the container does this)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

DATA = Path(os.getenv("APP_DATA", ROOT)).expanduser().resolve()
DATA.mkdir(parents=True, exist_ok=True)

HISTORY = DATA / "runs.jsonl"
CONFIG = DATA / "config.json"
PROFILE = DATA / "chrome-profile"
SESSION_FILE = DATA / "session.json"
LOGS = DATA / "logs"
SHOTS = LOGS / "shots"
DOWNLOADS = DATA / "downloads"
SIGNATURE = DATA / "signature.png"
HAND_FONT = ROOT / "fonts" / "IndieFlower-Regular.ttf"
INDEX = ROOT / "index.html"
SIGNIN_PAGE = ROOT / "signin.html"
