from __future__ import annotations

from typing import Any

from .api_snapshot import save_snapshot
from .config import get_settings
from .http_json import get_json


SOURCE_NAME = "한국수자원공사_수문 운영 정보"
SOURCE_URL = "https://www.data.go.kr/data/15099110/openapi.do"


def _error(reason: str, message: str) -> dict[str, Any]:
    return {
        "status": "error",
        "reason": reason,
        "source": SOURCE_NAME,
        "message": message,
    }


def _call(operation: str, params: dict[str, Any], snapshot_prefix: str) -> dict[str, Any]:
    settings = get_settings()
    if not settings.kwater_api_key:
        return _error("api_key_missing", "KWATER_API_KEY가 없어 실제 데이터를 불러오지 않았습니다.")
    url = f"{settings.kwater_base_url}/{operation}"
    request_params = {
        "serviceKey": settings.kwater_api_key,
        "pageNo": params.get("pageNo", 1),
        "numOfRows": params.get("numOfRows", 100),
        "damcode": params.get("damcode"),
        "stdt": params.get("stdt"),
        "eddt": params.get("eddt"),
        "_type": "json",
    }
    result = get_json(url, request_params)
    if settings.save_api_snapshots:
        safe_params = {**request_params, "serviceKey": "***"}
        save_snapshot(
            prefix=snapshot_prefix,
            source_name=SOURCE_NAME,
            source_url=result.get("url", url).replace(settings.kwater_api_key, "***"),
            request_params=safe_params,
            raw_response=result.get("data", result.get("raw", result)),
        )
    if not result.get("ok"):
        return _error("external_api_failed", "실제 데이터를 불러오지 못했습니다. 임의 데이터로 대체하지 않습니다.") | {
            "detail": result
        }
    body = (((result["data"] or {}).get("response") or {}).get("body") or {})
    items = ((body.get("items") or {}).get("item") or [])
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        items = []
    return {
        "status": "ok",
        "source_name": SOURCE_NAME,
        "source_url": result["url"].replace(settings.kwater_api_key, "***"),
        "source_type": "api",
        "fetched_count": len(items),
        "raw": result["data"],
        "items": items,
    }


def get_dam_observations(
    dam_code: str,
    start_time: str,
    end_time: str,
    *,
    interval: str = "hour",
) -> dict[str, Any]:
    operation_map = {
        "hour": "hourlist",
        "10min": "list",
        "day": "daylist",
    }
    operation = operation_map.get(interval)
    if not operation:
        return _error("unsupported_interval", "지원하지 않는 K-water 조회 간격입니다.")
    params = {
        "damcode": dam_code,
        "stdt": start_time[:10],
        "eddt": end_time[:10],
    }
    return _call(operation, params, f"kwater_dam_{dam_code}_{interval}")


def get_dam_latest(dam_code: str) -> dict[str, Any]:
    return _error(
        "not_available",
        "K-water 최신값 조회는 공식 문서상 기간 조회로 연결합니다. 시작/종료일을 지정해 호출하세요.",
    ) | {"dam_code": dam_code}

