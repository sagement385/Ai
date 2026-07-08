from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import BACKEND_ROOT, get_settings
from .data_validator import validate_source


GIS_ROOT = BACKEND_ROOT / "data" / "gis"
DEFAULT_MANIFEST = GIS_ROOT / "layers_manifest.json"


def _manifest_path() -> Path:
    settings = get_settings()
    import os

    configured = os.environ.get("GIS_LAYER_MANIFEST")
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = BACKEND_ROOT.parent / path
        return path
    return DEFAULT_MANIFEST


def load_catalog() -> dict[str, Any]:
    path = _manifest_path()
    if not path.exists():
        return {
            "status": "error",
            "reason": "manifest_missing",
            "path": str(path),
            "layers": [],
        }
    data = json.loads(path.read_text(encoding="utf-8"))
    layers = data.get("layers", [])
    if not isinstance(layers, list):
        layers = []
    validated_layers = []
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        validation = validate_source(
            {
                "source_name": layer.get("source_name"),
                "source_url": layer.get("source_url"),
                "source_type": layer.get("source_type"),
            },
            require_observed_at=False,
            strict_actual_source=True,
        )
        layer_path = layer.get("path")
        file_exists = False
        size_bytes = None
        if isinstance(layer_path, str) and layer_path:
            resolved = (GIS_ROOT / layer_path).resolve()
            if str(resolved).startswith(str(GIS_ROOT.resolve())) and resolved.exists():
                file_exists = True
                size_bytes = resolved.stat().st_size
        validated_layers.append(
            {
                **layer,
                "valid_source": validation["valid"],
                "validation_reason": validation["reason"],
                "file_exists": file_exists,
                "size_bytes": size_bytes,
                "url": f"/api/gis/layers/{layer.get('id')}" if file_exists else layer.get("url"),
            }
        )
    return {
        "status": "ok",
        "manifest_path": str(path),
        "gis_root": str(GIS_ROOT),
        "layers": validated_layers,
        "external_catalog": data.get("external_catalog", []),
        "terrain": _terrain_config(),
    }


def read_layer(layer_id: str) -> tuple[dict[str, Any] | list[Any] | None, str | None]:
    catalog = load_catalog()
    for layer in catalog.get("layers", []):
        if layer.get("id") != layer_id:
            continue
        if not layer.get("file_exists"):
            return None, "layer_file_missing"
        resolved = (GIS_ROOT / layer["path"]).resolve()
        if resolved.suffix.lower() not in {".json", ".geojson"}:
            return None, "unsupported_layer_format"
        return json.loads(resolved.read_text(encoding="utf-8")), None
    return None, "layer_not_found"


def _terrain_config() -> dict[str, Any]:
    import os

    tile_url = os.environ.get("DEM_TERRAIN_TILE_URL") or ""
    return {
        "available": bool(tile_url),
        "tile_url": tile_url or None,
        "attribution": os.environ.get("DEM_TERRAIN_ATTRIBUTION") or None,
    }

