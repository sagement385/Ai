from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def get_json(url: str, params: dict[str, Any] | None = None, timeout: int = 20) -> dict[str, Any]:
    query = urlencode({k: v for k, v in (params or {}).items() if v is not None}, doseq=True)
    full_url = f"{url}?{query}" if query else url
    request = Request(full_url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            try:
                return {"ok": True, "url": full_url, "data": json.loads(raw), "raw": raw}
            except json.JSONDecodeError:
                return {"ok": False, "url": full_url, "reason": "invalid_json_response", "raw": raw}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "url": full_url, "reason": "http_error", "status": exc.code, "raw": raw}
    except URLError as exc:
        return {"ok": False, "url": full_url, "reason": "url_error", "message": str(exc.reason)}
    except TimeoutError:
        return {"ok": False, "url": full_url, "reason": "timeout"}

