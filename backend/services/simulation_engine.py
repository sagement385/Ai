from __future__ import annotations

from datetime import datetime
from typing import Any

from .data_validator import (
    usable_number,
    validate_source,
    validate_sourced_value,
)
from .mechanical_calc import rational_runoff_cms, water_level_risk


RISK_ORDER = {
    "normal": 0,
    "watch": 1,
    "danger": 2,
    "critical": 3,
    "not_available": -1,
    "unknown": -1,
}


def run_simulation(payload: dict[str, Any], *, strict_data_mode: bool = True) -> dict[str, Any]:
    mode = payload.get("mode", "observed")
    scenario_id = payload.get("scenario_id") or "unnamed_scenario"
    strict_actual_source = strict_data_mode and mode == "observed"
    rainfall = _valid_rainfall(payload.get("rainfall_observations", []), strict_actual_source)
    water_levels = _valid_water_levels(payload.get("water_level_observations", []), strict_actual_source)
    assets = payload.get("hydraulic_assets", [])
    if not isinstance(assets, list):
        assets = []

    predictions = []
    unmapped_assets = []
    data_gaps = []

    if not rainfall["usable"]:
        data_gaps.append("usable_rainfall_observations_missing")

    for asset in assets:
        if not isinstance(asset, dict):
            data_gaps.append("invalid_asset_object")
            continue
        prediction = _predict_asset(asset, rainfall, water_levels, strict_actual_source)
        predictions.append(prediction)
        if not prediction["geometry_available"]:
            unmapped_assets.append(
                {
                    "asset_id": prediction["asset_id"],
                    "name": prediction["name"],
                    "reason": "geometry_missing_or_unverified",
                }
            )
        data_gaps.extend(prediction.get("data_gaps", []))

    if not assets:
        data_gaps.append("hydraulic_assets_missing")

    overall = _overall_risk(predictions)
    return {
        "status": "ok",
        "scenario_id": scenario_id,
        "mode": mode,
        "generated_at": datetime.now().astimezone().isoformat(),
        "strict_data_mode": strict_data_mode,
        "principle": "prediction_uses_only_sourced_inputs_validation_labels_are_excluded",
        "overall_risk": overall,
        "rainfall_summary": rainfall["summary"],
        "predictions": predictions,
        "unmapped_assets": unmapped_assets,
        "data_gaps": sorted(set(data_gaps)),
        "prohibited_outputs": [
            "confirmed_collapse_from_prediction",
            "invented_coordinates",
            "invented_hydraulic_thresholds",
            "unsourced_actual_rainfall",
        ],
    }


def _valid_rainfall(observations: Any, strict_actual_source: bool) -> dict[str, Any]:
    usable = []
    invalid = []
    if not isinstance(observations, list):
        observations = []
    for obs in observations:
        if not isinstance(obs, dict):
            invalid.append({"reason": "invalid_observation_object"})
            continue
        check = validate_source(
            obs,
            require_observed_at=True,
            require_unit=True,
            numeric_field="value",
            strict_actual_source=strict_actual_source,
        )
        if not check["valid"]:
            invalid.append({"observation": obs.get("station_code"), **check})
            continue
        if obs.get("unit") not in {"mm", "millimeter", "millimeters"}:
            invalid.append({"observation": obs.get("station_code"), "reason": "unsupported_rainfall_unit"})
            continue
        usable.append(obs)
    values = [usable_number(item) for item in usable]
    values = [value for value in values if value is not None]
    durations = [
        usable_number(item.get("duration_hours"))
        for item in usable
        if usable_number(item.get("duration_hours")) is not None
    ]
    return {
        "usable": usable,
        "invalid": invalid,
        "summary": {
            "count": len(usable),
            "max_rainfall_mm": max(values) if values else None,
            "max_duration_hours": max(durations) if durations else None,
            "invalid_count": len(invalid),
        },
    }


def _valid_water_levels(observations: Any, strict_actual_source: bool) -> dict[str, Any]:
    usable = []
    invalid = []
    if not isinstance(observations, list):
        observations = []
    for obs in observations:
        if not isinstance(obs, dict):
            invalid.append({"reason": "invalid_observation_object"})
            continue
        check = validate_source(
            obs,
            require_observed_at=True,
            require_unit=True,
            numeric_field="value",
            strict_actual_source=strict_actual_source,
        )
        if not check["valid"]:
            invalid.append({"observation": obs.get("station_code"), **check})
            continue
        usable.append(obs)
    return {"usable": usable, "invalid": invalid}


def _predict_asset(
    asset: dict[str, Any],
    rainfall: dict[str, Any],
    water_levels: dict[str, Any],
    strict_actual_source: bool,
) -> dict[str, Any]:
    asset_id = asset.get("asset_id") or asset.get("id") or "unknown_asset"
    name = asset.get("name") or asset_id
    asset_type = asset.get("asset_type") or "unknown"
    data_gaps = []
    source_check = validate_source(
        {
            "source": asset.get("source", {}),
            "observed_at": asset.get("source_observed_at", "2000-01-01T00:00:00+09:00"),
        },
        require_observed_at=False,
        strict_actual_source=strict_actual_source,
    )
    if not source_check["valid"]:
        data_gaps.append(f"{asset_id}:asset_source_invalid:{source_check['reason']}")

    geometry = asset.get("geometry")
    geometry_available = _geometry_is_valid(geometry) and source_check["valid"]
    if geometry and not geometry_available:
        data_gaps.append(f"{asset_id}:geometry_unverified")

    thresholds = asset.get("thresholds") if isinstance(asset.get("thresholds"), dict) else {}
    catchment = asset.get("catchment") if isinstance(asset.get("catchment"), dict) else {}
    rain_status = _rainfall_threshold_status(asset_id, rainfall, thresholds, strict_actual_source, data_gaps)
    runoff_status = _runoff_capacity_status(
        asset_id,
        rainfall,
        catchment,
        thresholds,
        strict_actual_source,
        data_gaps,
    )
    level_status = _water_level_status(
        asset_id,
        asset,
        water_levels,
        thresholds,
        strict_actual_source,
        data_gaps,
    )

    component_statuses = [rain_status["status"], runoff_status["status"], level_status["status"]]
    risk_level = _max_status(component_statuses)
    collapse_status = _collapse_status(asset_type, risk_level, thresholds, strict_actual_source, data_gaps)
    map_color = _risk_color(risk_level)

    return {
        "asset_id": asset_id,
        "name": name,
        "asset_type": asset_type,
        "geometry": geometry if geometry_available else None,
        "geometry_available": geometry_available,
        "risk_level": risk_level,
        "map_color": map_color,
        "rainfall_threshold_analysis": rain_status,
        "runoff_capacity_analysis": runoff_status,
        "water_level_analysis": level_status,
        "collapse_assessment": collapse_status,
        "data_gaps": data_gaps,
        "source_basis": _source_basis(asset, rainfall, water_levels),
    }


def _rainfall_threshold_status(
    asset_id: str,
    rainfall: dict[str, Any],
    thresholds: dict[str, Any],
    strict_actual_source: bool,
    data_gaps: list[str],
) -> dict[str, Any]:
    max_rain = rainfall["summary"]["max_rainfall_mm"]
    threshold = thresholds.get("critical_rainfall_mm")
    check = validate_sourced_value(
        threshold,
        field_name="critical_rainfall_mm",
        strict_actual_source=strict_actual_source,
    )
    if max_rain is None:
        return {"status": "not_available", "reason": "rainfall_missing"}
    if not check["valid"]:
        data_gaps.append(f"{asset_id}:critical_rainfall_threshold_missing_or_invalid")
        return {"status": "not_available", "reason": check["reason"]}
    threshold_value = usable_number(threshold)
    if threshold_value is None or threshold_value <= 0:
        return {"status": "not_available", "reason": "invalid_threshold_value"}
    ratio = max_rain / threshold_value
    if ratio < 0.7:
        status = "normal"
    elif ratio < 0.9:
        status = "watch"
    elif ratio < 1.0:
        status = "danger"
    else:
        status = "critical"
    return {
        "status": status,
        "ratio": ratio,
        "observed_rainfall_mm": max_rain,
        "critical_rainfall_mm": threshold_value,
        "reason": "sourced_rainfall_threshold_comparison",
    }


def _runoff_capacity_status(
    asset_id: str,
    rainfall: dict[str, Any],
    catchment: dict[str, Any],
    thresholds: dict[str, Any],
    strict_actual_source: bool,
    data_gaps: list[str],
) -> dict[str, Any]:
    max_rain = rainfall["summary"]["max_rainfall_mm"]
    duration = rainfall["summary"]["max_duration_hours"]
    area_obj = catchment.get("area_km2")
    runoff_obj = catchment.get("runoff_coefficient")
    capacity_obj = thresholds.get("drainage_capacity_cms") or thresholds.get("channel_capacity_cms")
    required = {
        "catchment_area_km2": area_obj,
        "runoff_coefficient": runoff_obj,
        "capacity_cms": capacity_obj,
    }
    for name, item in required.items():
        check = validate_sourced_value(item, field_name=name, strict_actual_source=strict_actual_source)
        if not check["valid"]:
            data_gaps.append(f"{asset_id}:{name}_missing_or_invalid")
            return {"status": "not_available", "reason": check["reason"]}
    runoff = rational_runoff_cms(
        max_rain,
        duration,
        usable_number(area_obj),
        usable_number(runoff_obj),
    )
    if runoff["status"] != "calculated":
        return runoff
    capacity = usable_number(capacity_obj)
    if capacity is None or capacity <= 0:
        return {"status": "not_available", "reason": "invalid_capacity"}
    ratio = runoff["value"] / capacity
    if ratio < 0.7:
        status = "normal"
    elif ratio < 0.9:
        status = "watch"
    elif ratio < 1.0:
        status = "danger"
    else:
        status = "critical"
    return {
        "status": status,
        "ratio": ratio,
        "estimated_runoff_cms": runoff["value"],
        "capacity_cms": capacity,
        "method": runoff["method"],
        "reason": "sourced_capacity_comparison",
    }


def _water_level_status(
    asset_id: str,
    asset: dict[str, Any],
    water_levels: dict[str, Any],
    thresholds: dict[str, Any],
    strict_actual_source: bool,
    data_gaps: list[str],
) -> dict[str, Any]:
    station_code = asset.get("water_level_station_code")
    matching = [
        obs for obs in water_levels["usable"]
        if station_code and obs.get("station_code") == station_code
    ]
    if not matching:
        return {"status": "not_available", "reason": "matching_water_level_missing"}
    latest = sorted(matching, key=lambda item: item.get("observed_at", ""))[-1]
    reference = thresholds.get("reference_level_el_m") or thresholds.get("levee_crest_level_el_m")
    check = validate_sourced_value(
        reference,
        field_name="reference_level_el_m",
        strict_actual_source=strict_actual_source,
    )
    if not check["valid"]:
        data_gaps.append(f"{asset_id}:reference_level_missing_or_invalid")
        return {"status": "not_available", "reason": check["reason"]}
    result = water_level_risk(usable_number(latest), usable_number(reference))
    return {
        **result,
        "current_level": usable_number(latest),
        "reference_level": usable_number(reference),
        "station_code": station_code,
    }


def _collapse_status(
    asset_type: str,
    risk_level: str,
    thresholds: dict[str, Any],
    strict_actual_source: bool,
    data_gaps: list[str],
) -> dict[str, Any]:
    if asset_type != "levee":
        return {"status": "not_applicable", "reason": "asset_not_levee"}
    fragility = thresholds.get("structural_fragility")
    if fragility is None:
        if risk_level in {"danger", "critical"}:
            return {
                "status": "collapse_not_determined",
                "reason": "hydraulic_risk_detected_but_structural_fragility_missing",
                "allowed_expression": "overtopping_or_field_inspection_required",
            }
        return {"status": "not_available", "reason": "structural_fragility_missing"}
    check = validate_source(
        {"source": fragility.get("source", {})},
        require_observed_at=False,
        strict_actual_source=strict_actual_source,
    )
    if not check["valid"]:
        data_gaps.append("structural_fragility_source_invalid")
        return {"status": "not_available", "reason": check["reason"]}
    return {
        "status": "fragility_model_available",
        "reason": "separate_structural_model_required_before_failure_claim",
    }


def _geometry_is_valid(geometry: Any) -> bool:
    if not isinstance(geometry, dict):
        return False
    if geometry.get("type") == "Point":
        coords = geometry.get("coordinates")
        return (
            isinstance(coords, list)
            and len(coords) == 2
            and all(isinstance(value, (int, float)) for value in coords)
        )
    if geometry.get("type") in {"LineString", "Polygon"}:
        return isinstance(geometry.get("coordinates"), list)
    return False


def _overall_risk(predictions: list[dict[str, Any]]) -> str:
    if not predictions:
        return "unknown"
    return _max_status([item.get("risk_level", "not_available") for item in predictions])


def _max_status(statuses: list[str]) -> str:
    usable = [status for status in statuses if status in RISK_ORDER and RISK_ORDER[status] >= 0]
    if not usable:
        return "not_available"
    return max(usable, key=lambda status: RISK_ORDER[status])


def _risk_color(status: str) -> str:
    return {
        "normal": "green",
        "watch": "yellow",
        "danger": "orange",
        "critical": "red",
    }.get(status, "gray")


def _source_basis(asset: dict[str, Any], rainfall: dict[str, Any], water_levels: dict[str, Any]) -> list[str]:
    basis = []
    asset_source = asset.get("source", {})
    if asset_source.get("source_name"):
        basis.append(asset_source["source_name"])
    for obs in rainfall["usable"][:3]:
        if obs.get("source_name"):
            basis.append(obs["source_name"])
        elif isinstance(obs.get("source"), dict) and obs["source"].get("source_name"):
            basis.append(obs["source"]["source_name"])
    for obs in water_levels["usable"][:3]:
        if obs.get("source_name"):
            basis.append(obs["source_name"])
        elif isinstance(obs.get("source"), dict) and obs["source"].get("source_name"):
            basis.append(obs["source"]["source_name"])
    return sorted(set(basis))

