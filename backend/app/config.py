import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
VAR_DIR = BASE_DIR / "var"
UPLOAD_DIR = BASE_DIR / "uploads"
FRONTEND_DIR = BASE_DIR.parent / "frontend"
STATIC_DIR = FRONTEND_DIR / "static"
VENDOR_DIR = FRONTEND_DIR / "vendor"
INDEX_HTML = FRONTEND_DIR / "index.html"

ENV_FILE = BASE_DIR.parent / ".env"


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


_load_dotenv(ENV_FILE)

DB_PATH = Path(os.getenv("CAMPUSPILOT_DB", str(VAR_DIR / "campuspilot.db")))
MAP_PATH = Path(os.getenv("CAMPUSPILOT_MAP", str(DATA_DIR / "campus_map.json")))

SPEED_MPS = float(os.getenv("CAMPUSPILOT_SPEED_MPS", "3.0"))
CONGESTED_FACTOR = float(os.getenv("CAMPUSPILOT_CONGESTED_FACTOR", "2.0"))
SIM_SPEED_MPS = float(os.getenv("CAMPUSPILOT_SIM_SPEED_MPS", "40.0"))
DEFAULT_DWELL_SECONDS = float(os.getenv("CAMPUSPILOT_DWELL_SECONDS", "60"))

# Optional upstream adapter; existing manual workflows work with this disabled.
SEMANTIC_BRIDGE_ENABLED = os.getenv("CAMPUSPILOT_SEMANTIC_BRIDGE", "1").lower() in ("1", "true", "yes")
SIM_TICK_INTERVAL = max(0.05, float(os.getenv("CAMPUSPILOT_SIM_TICK_INTERVAL", "0.1")))
SEMANTIC_MAX_AGE_SECONDS = float(os.getenv("CAMPUSPILOT_SEMANTIC_MAX_AGE", "30"))
# 点击启用模拟源后多久发送第一条危险消息；0 表示立即发送并先停车。
SIM_UPSTREAM_DELAY_SECONDS = max(0.0, float(os.getenv("CAMPUSPILOT_SIM_UPSTREAM_DELAY", "0")))
# 自动解除等待时间；0 表示不自动解除，必须人工确认（用于施工围挡）。
SIM_UPSTREAM_CLEAR_SECONDS = {
    "pedestrian": max(0.0, float(os.getenv("CAMPUSPILOT_SIM_CLEAR_PEDESTRIAN", "3"))),
    "crowd": max(0.0, float(os.getenv("CAMPUSPILOT_SIM_CLEAR_CROWD", "30"))),
    "construction": max(0.0, float(os.getenv("CAMPUSPILOT_SIM_CLEAR_CONSTRUCTION", "0"))),
}
SIM_UPSTREAM_EXPECTED_SECONDS = {
    "pedestrian": 5.0,
    "crowd": 30.0,
    "construction": 1800.0,
}

LLM_BASE_URL = os.getenv("CAMPUSPILOT_LLM_BASE_URL", "")
LLM_API_KEY = os.getenv("CAMPUSPILOT_LLM_API_KEY", "")
LLM_MODEL = os.getenv("CAMPUSPILOT_LLM_MODEL", "")
LLM_TIMEOUT_SECONDS = float(os.getenv("CAMPUSPILOT_LLM_TIMEOUT", "20"))


def ensure_dirs() -> None:
    VAR_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
