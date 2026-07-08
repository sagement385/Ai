from __future__ import annotations

from typing import Any

from .data_validator import validate_source


def compare_predictions_to_events(prediction_result: dict[str, Any], validation_payload: dict[str, Any]) -> dict[str, Any]:
    events = validation_payload.get("validation_events", [])
    if not isinstance(events, list):
        events = []

    valid_events = []
    rejected_events = []
    for event in events:
        if not isinstance(event, dict):
            rejected_events.append({"reason": "invalid_event_object"})
            continue
        check = validate_source(
            event,
            require_observed_at=True,
            require_unit=False,
            numeric_field=None,
            strict_actual_source=True,
        )
        if not check["valid"]:
            rejected_events.append({"event_id": event.get("event_id"), **check})
            continue
        valid_events.append(event)

    predictions = prediction_result.get("predictions", [])
    by_asset = {item.get("asset_id"): item for item in predictions if isinstance(item, dict)}
    matches = []
    misses = []
    for event in valid_events:
        asset_id = event.get("asset_id")
        predicted = by_asset.get(asset_id)
        if predicted is None:
            misses.append(
                {
                    "event_id": event.get("event_id"),
                    "asset_id": asset_id,
                    "reason": "asset_not_predicted",
                }
            )
            continue
        expected = event.get("expected_model_signal")
        actual = predicted.get("risk_level")
        matches.append(
            {
                "event_id": event.get("event_id"),
                "asset_id": asset_id,
                "expected_model_signal": expected,
                "predicted_risk_level": actual,
                "matched": _matches(expected, actual),
                "source_name": event.get("source_name"),
                "source_url": event.get("source_url"),
            }
        )

    return {
        "status": "ok",
        "principle": "validation_events_are_labels_not_prediction_inputs",
        "valid_event_count": len(valid_events),
        "rejected_event_count": len(rejected_events),
        "matches": matches,
        "misses": misses,
        "rejected_events": rejected_events,
    }


def _matches(expected: str | None, actual: str | None) -> bool:
    if not expected or not actual:
        return False
    if expected == actual:
        return True
    if expected == "failure_or_inundation_reported" and actual in {"danger", "critical"}:
        return True
    if expected == "no_reported_damage" and actual in {"normal", "watch"}:
        return True
    return False

