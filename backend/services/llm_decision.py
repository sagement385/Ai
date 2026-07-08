from __future__ import annotations

from typing import Any


ALLOWED_ACTIONS = [
    "close_underpass_recommendation",
    "inspect_levee_recommendation",
    "dispatch_field_team_recommendation",
    "notify_police_recommendation",
    "prepare_pump_station_recommendation",
    "inspect_drainage_gate_recommendation",
    "issue_driver_alert_recommendation",
    "monitor_cctv_recommendation",
]

FORBIDDEN_ACTIONS = [
    "operate_dam",
    "operate_gate",
    "declare_levee_collapse_without_official_event",
    "invent_missing_data",
]


def decision_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["risk_level", "summary", "data_gaps", "actions", "map_updates"],
        "properties": {
            "risk_level": {"enum": ["normal", "watch", "danger", "critical", "unknown"]},
            "summary": {"type": "string"},
            "data_gaps": {"type": "array", "items": {"type": "string"}},
            "actions": {"type": "array"},
            "map_updates": {"type": "array"},
        },
    }


def build_decision_cards(simulation_result: dict[str, Any]) -> dict[str, Any]:
    risk = simulation_result.get("overall_risk")
    if risk == "not_available":
        risk = "unknown"
    data_gaps = simulation_result.get("data_gaps", [])
    actions = []
    map_updates = []

    for prediction in simulation_result.get("predictions", []):
        if not isinstance(prediction, dict):
            continue
        basis = prediction.get("source_basis") or []
        if not basis:
            continue
        risk_level = prediction.get("risk_level", "not_available")
        asset_type = prediction.get("asset_type")
        target = prediction.get("name") or prediction.get("asset_id")
        if risk_level in {"danger", "critical"}:
            if asset_type == "underpass":
                actions.append(_action("close_underpass_recommendation", target, 1, "침수 위험 신호가 임계 수준입니다.", basis))
                actions.append(_action("issue_driver_alert_recommendation", target, 2, "차량 진입 위험 알림이 필요합니다.", basis))
            elif asset_type == "levee":
                actions.append(_action("inspect_levee_recommendation", target, 1, "제방 월류 또는 취약성 점검이 필요합니다.", basis))
                actions.append(_action("dispatch_field_team_recommendation", target, 2, "현장 확인 전까지 붕괴로 단정하지 않습니다.", basis))
            elif asset_type == "pump_station":
                actions.append(_action("prepare_pump_station_recommendation", target, 2, "배수 능력 검토가 필요합니다.", basis))
            else:
                actions.append(_action("dispatch_field_team_recommendation", target, 3, "위험 신호 확인이 필요합니다.", basis))
        map_updates.append(
            {
                "target": target,
                "status": risk_level,
                "color": prediction.get("map_color", "gray"),
                "source_basis": basis,
            }
        )

    if not actions and data_gaps:
        actions.append(
            {
                "action": "monitor_cctv_recommendation",
                "target": "데이터 공백 지점",
                "priority": 3,
                "reason": "예측에 필요한 출처 있는 수문/시설 데이터가 부족합니다.",
                "source_basis": ["system_data_validation"],
                "confidence": "low",
            }
        )

    return {
        "risk_level": risk if risk in {"normal", "watch", "danger", "critical"} else "unknown",
        "summary": _summary(simulation_result),
        "data_gaps": data_gaps,
        "actions": _filter_allowed(actions),
        "map_updates": map_updates,
        "guardrails": {
            "allowed_actions": ALLOWED_ACTIONS,
            "forbidden_actions": FORBIDDEN_ACTIONS,
            "llm_role": "summary_and_prioritization_only",
        },
    }


def _action(action: str, target: str, priority: int, reason: str, basis: list[str]) -> dict[str, Any]:
    return {
        "action": action,
        "target": target,
        "priority": priority,
        "reason": reason,
        "source_basis": basis,
        "confidence": "medium",
    }


def _filter_allowed(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    filtered = []
    for action in actions:
        if action.get("action") not in ALLOWED_ACTIONS:
            continue
        if not action.get("source_basis"):
            continue
        filtered.append(action)
    return sorted(filtered, key=lambda item: item.get("priority", 99))


def _summary(simulation_result: dict[str, Any]) -> str:
    risk = simulation_result.get("overall_risk", "unknown")
    gaps = len(simulation_result.get("data_gaps", []))
    count = len(simulation_result.get("predictions", []))
    if risk in {"danger", "critical"}:
        return f"{count}개 시설 중 위험 신호가 감지되었습니다. 단, 붕괴 확정 표현은 사용하지 않습니다."
    if risk in {"normal", "watch"}:
        return f"{count}개 시설에 대해 출처 있는 입력으로 위험도를 계산했습니다."
    return f"예측에 필요한 데이터가 부족합니다. 데이터 공백 {gaps}건을 먼저 해소해야 합니다."

