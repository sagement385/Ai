from __future__ import annotations


def water_level_risk(current_level: float | None, reference_level: float | None) -> dict:
    if current_level is None or reference_level is None:
        return {"status": "not_available", "ratio": None, "reason": "missing_level_or_reference"}
    if reference_level <= 0:
        return {"status": "not_available", "ratio": None, "reason": "invalid_reference_level"}
    ratio = current_level / reference_level
    if ratio < 0.7:
        status = "normal"
    elif ratio < 0.9:
        status = "watch"
    elif ratio < 1.0:
        status = "danger"
    else:
        status = "critical"
    return {"status": status, "ratio": ratio, "reason": "calculated"}


def discharge_trend(current_q: float | None, previous_q: float | None) -> dict:
    if current_q is None or previous_q is None:
        return {"status": "unknown", "delta": None, "reason": "missing_discharge"}
    delta = current_q - previous_q
    if delta > 0:
        status = "increasing"
    elif delta < 0:
        status = "decreasing"
    else:
        status = "stable"
    return {"status": status, "delta": delta, "reason": "calculated"}


def release_trend(current_release: float | None, previous_release: float | None) -> dict:
    if current_release is None or previous_release is None:
        return {
            "status": "unknown",
            "delta": None,
            "interpretation": "release_data_missing",
        }
    delta = current_release - previous_release
    if delta > 0:
        status = "increasing"
    elif delta < 0:
        status = "decreasing"
    else:
        status = "stable"
    return {
        "status": status,
        "delta": delta,
        "interpretation": "downstream_effect_review_required",
    }


def gate_backflow_check(river_level: float | None, inner_water_level: float | None) -> dict:
    if river_level is None or inner_water_level is None:
        return {
            "status": "not_available",
            "recommended_action": "field_check_required",
            "reason": "missing_river_or_inner_level",
        }
    if river_level > inner_water_level:
        return {
            "status": "backflow_possible",
            "recommended_action": "keep_or_check_closed",
            "reason": "river_level_higher_than_inner_water_level",
        }
    return {
        "status": "drainage_possible",
        "recommended_action": "inspect_opening_condition",
        "reason": "river_level_not_higher_than_inner_water_level",
    }


def rational_runoff_cms(
    rainfall_mm: float | None,
    duration_hours: float | None,
    catchment_area_km2: float | None,
    runoff_coefficient: float | None,
) -> dict:
    if (
        rainfall_mm is None
        or duration_hours is None
        or catchment_area_km2 is None
        or runoff_coefficient is None
    ):
        return {"status": "not_available", "value": None, "reason": "missing_rational_method_inputs"}
    if duration_hours <= 0 or catchment_area_km2 <= 0 or not (0 <= runoff_coefficient <= 1):
        return {"status": "not_available", "value": None, "reason": "invalid_rational_method_inputs"}
    intensity_mm_per_hour = rainfall_mm / duration_hours
    runoff_cms = 0.277778 * runoff_coefficient * intensity_mm_per_hour * catchment_area_km2
    return {
        "status": "calculated",
        "value": runoff_cms,
        "unit": "m3/s",
        "method": "rational_method",
        "inputs": {
            "rainfall_mm": rainfall_mm,
            "duration_hours": duration_hours,
            "catchment_area_km2": catchment_area_km2,
            "runoff_coefficient": runoff_coefficient,
        },
    }

