from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
API_SNAPSHOT_DIR = BACKEND_ROOT / "logs" / "api_snapshots"


def _load_dotenv() -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    hrfco_api_key: str | None
    kwater_api_key: str | None
    openai_api_key: str | None
    openai_model: str | None
    hrfco_base_url: str
    kwater_base_url: str
    enable_llm: bool
    save_api_snapshots: bool
    strict_data_mode: bool


def get_settings() -> Settings:
    _load_dotenv()
    return Settings(
        hrfco_api_key=os.environ.get("HRFCO_API_KEY") or None,
        kwater_api_key=os.environ.get("KWATER_API_KEY") or None,
        openai_api_key=os.environ.get("OPENAI_API_KEY") or None,
        openai_model=os.environ.get("OPENAI_MODEL") or None,
        hrfco_base_url=os.environ.get("HRFCO_BASE_URL", "http://api.hrfco.go.kr").rstrip("/"),
        kwater_base_url=os.environ.get(
            "KWATER_BASE_URL",
            "http://apis.data.go.kr/B500001/dam/sluicePresentCondition",
        ).rstrip("/"),
        enable_llm=_bool_env("ENABLE_LLM", False),
        save_api_snapshots=_bool_env("SAVE_API_SNAPSHOTS", True),
        strict_data_mode=_bool_env("STRICT_DATA_MODE", True),
    )


def public_config_status() -> dict:
    settings = get_settings()
    return {
        "strict_data_mode": settings.strict_data_mode,
        "save_api_snapshots": settings.save_api_snapshots,
        "enable_llm": settings.enable_llm,
        "keys": {
            "hrfco_api_key": "configured" if settings.hrfco_api_key else "missing",
            "kwater_api_key": "configured" if settings.kwater_api_key else "missing",
            "openai_api_key": "configured" if settings.openai_api_key else "missing",
        },
        "sources": {
            "hrfco_base_url": settings.hrfco_base_url,
            "kwater_base_url": settings.kwater_base_url,
        },
    }

