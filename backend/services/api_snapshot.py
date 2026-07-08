from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import API_SNAPSHOT_DIR


def save_snapshot(
    *,
    prefix: str,
    source_name: str,
    source_url: str,
    request_params: dict[str, Any],
    raw_response: Any,
) -> Path:
    API_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now().astimezone()
    safe_prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", prefix).strip("_")
    filename = f"{safe_prefix}_{now.strftime('%Y%m%dT%H%M%S%z')}.json"
    path = API_SNAPSHOT_DIR / filename
    payload = {
        "fetched_at": now.isoformat(),
        "source_name": source_name,
        "source_url": source_url,
        "request_params": request_params,
        "raw_response": raw_response,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path

