from __future__ import annotations

from typing import Any

from .api_snapshot import save_snapshot
from .config import get_settings
from .http_json import get_json


SOURCE_NAME = "기후에너지환경부 한강홍수통제소_표준수문DB"
SOURCE_URL = "https://www.data.go.kr/data/3040409/openapi.do"


def _error(reason: str, message: str) -> dict[str, Any]:
    return {
        "status": "error",
        "reason": reason,
        "source": SOURCE_NAME,
        "message": message,
    }


def _fmt_time(value: str) -> str:
    return value.replace("-", "").replace(":", "").replace("T", "").replace("+09:00", "")[:12]


def _call(path: str, params: dict[str, Any], snapshot_prefix: str) -> dict[str, Any]:
    settings = get_settings()
    if not settings.hrfco_api_key:
        return _error("api_key_missing", "HRFCO_API_KEY가 없어 실제 데이터를 불러오지 않았습니다.")
    url = f"{settings.hrfco_base_url}/{settings.hrfco_api_key}/{path.lstrip('/')}"
    result = get_json(url)
    if settings.save_api_snapshots:
        save_snapshot(
            prefix=snapshot_prefix,
            source_name=SOURCE_NAME,
            source_url=result.get("url", url),
            request_params=params,
            raw_response=result.get("data", result.get("raw", result)),
        )
    if not result.get("ok"):
        return _error("external_api_failed", "실제 데이터를 불러오지 못했습니다. 임의 데이터로 대체하지 않습니다.") | {
            "detail": result
        }
    content = result["data"].get("content", []) if isinstance(result["data"], dict) else []
    return {
        "status": "ok",
        "source_name": SOURCE_NAME,
        "source_url": result["url"],
        "source_type": "api",
        "fetched_count": len(content) if isinstance(content, list) else None,
        "raw": result["data"],
        "items": content if isinstance(content, list) else [],
    }


def get_station_metadata(hydro_type: str = "waterlevel") -> dict[str, Any]:
    if hydro_type not in {"waterlevel", "rainfall", "dam", "bo"}:
        return _error("unsupported_hydro_type", "지원하지 않는 관측소 유형입니다.")
    path = f"{hydro_type}/info.json"
    return _call(path, {"hydro_type": hydro_type}, f"hrfco_{hydro_type}_info")


def get_rainfall(station_code: str, start_time: str, end_time: str, interval: str = "10M") -> dict[str, Any]:
    start = _fmt_time(start_time)
    end = _fmt_time(end_time)
    path = f"rainfall/list/{interval}/{station_code}/{start}/{end}.json"
    return _call(
        path,
        {"station_code": station_code, "start_time": start_time, "end_time": end_time, "interval": interval},
        f"hrfco_rainfall_{station_code}",
    )


def get_water_level(station_code: str, start_time: str, end_time: str, interval: str = "10M") -> dict[str, Any]:
    start = _fmt_time(start_time)
    end = _fmt_time(end_time)
    path = f"waterlevel/list/{interval}/{station_code}/{start}/{end}.json"
    return _call(
        path,
        {"station_code": station_code, "start_time": start_time, "end_time": end_time, "interval": interval},
        f"hrfco_water_level_{station_code}",
    )


def get_discharge(station_code: str, start_time: str, end_time: str, interval: str = "10M") -> dict[str, Any]:
    data = get_water_level(station_code, start_time, end_time, interval)
    if data.get("status") != "ok":
        return data
    items = []
    for item in data.get("items", []):
        if not isinstance(item, dict):
            continue
        discharge = item.get("fw") or item.get("q") or item.get("discharge")
        items.append(
            {
                "station_code": station_code,
                "value": discharge,
                "unit": "m3/s",
                "observed_at": item.get("ymdhm"),
                "source_name": SOURCE_NAME,
                "source_url": data.get("source_url"),
                "source_type": "api",
            }
        )
    return {**data, "items": items}


def get_flood_warning(station_code: str | None = None, start_time: str | None = None, end_time: str | None = None) -> dict[str, Any]:
    return _error(
        "endpoint_not_confirmed",
        "홍수특보 API 경로는 환경에서 검증된 뒤 연결해야 합니다. 임의 경로로 호출하지 않습니다.",
    ) | {
        "request": {
            "station_code": station_code,
            "start_time": start_time,
            "end_time": end_time,
        }
    }

