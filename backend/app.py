from __future__ import annotations

import json
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.config import FRONTEND_ROOT, get_settings, public_config_status
from backend.services.hrfco_client import (
    get_discharge,
    get_flood_warning,
    get_rainfall,
    get_station_metadata,
    get_water_level,
)
from backend.services.kwater_client import get_dam_observations
from backend.services.llm_decision import build_decision_cards, decision_schema
from backend.services.simulation_engine import run_simulation
from backend.services.validation_engine import compare_predictions_to_events


STATIC_ROOT = FRONTEND_ROOT


class Handler(BaseHTTPRequestHandler):
    server_version = "ChungbukFloodDigitalTwin/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            return self._json({"status": "ok", "service": "chungbuk-flood-digital-twin"})
        if parsed.path == "/api/config/status":
            return self._json(public_config_status())
        if parsed.path == "/api/decision/schema":
            return self._json(decision_schema())
        if parsed.path == "/api/hrfco/stations":
            qs = parse_qs(parsed.query)
            return self._json(get_station_metadata(qs.get("hydro_type", ["waterlevel"])[0]))
        if parsed.path == "/api/hrfco/rainfall":
            return self._json(self._hrfco_series(parsed, get_rainfall))
        if parsed.path == "/api/hrfco/water-level":
            return self._json(self._hrfco_series(parsed, get_water_level))
        if parsed.path == "/api/hrfco/discharge":
            return self._json(self._hrfco_series(parsed, get_discharge))
        if parsed.path == "/api/hrfco/flood-warning":
            qs = parse_qs(parsed.query)
            return self._json(
                get_flood_warning(
                    qs.get("station_code", [None])[0],
                    qs.get("start_time", [None])[0],
                    qs.get("end_time", [None])[0],
                )
            )
        if parsed.path == "/api/kwater/dam-observations":
            qs = parse_qs(parsed.query)
            return self._json(
                get_dam_observations(
                    qs.get("dam_code", [""])[0],
                    qs.get("start_time", [""])[0],
                    qs.get("end_time", [""])[0],
                    interval=qs.get("interval", ["hour"])[0],
                )
            )
        return self._static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/simulations/run":
            payload = self._read_json()
            if payload is None:
                return self._json({"status": "error", "reason": "invalid_json"}, status=400)
            result = run_simulation(payload, strict_data_mode=get_settings().strict_data_mode)
            return self._json({"simulation": result, "decision_cards": build_decision_cards(result)})
        if parsed.path == "/api/validation/compare":
            payload = self._read_json()
            if payload is None:
                return self._json({"status": "error", "reason": "invalid_json"}, status=400)
            prediction = payload.get("prediction_result") or payload.get("simulation") or {}
            validation = payload.get("validation") or payload
            return self._json(compare_predictions_to_events(prediction, validation))
        return self._json({"status": "error", "reason": "not_found"}, status=404)

    def log_message(self, format: str, *args) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), format % args))

    def _hrfco_series(self, parsed, fn):
        qs = parse_qs(parsed.query)
        return fn(
            qs.get("station_code", [""])[0],
            qs.get("start_time", [""])[0],
            qs.get("end_time", [""])[0],
            qs.get("interval", ["10M"])[0],
        )

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = self.rfile.read(length).decode("utf-8")
            return json.loads(data or "{}")
        except (ValueError, json.JSONDecodeError):
            return None

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path: str) -> None:
        if path == "/":
            path = "/index.html"
        target = (STATIC_ROOT / path.lstrip("/")).resolve()
        if not str(target).startswith(str(STATIC_ROOT.resolve())) or not target.exists() or target.is_dir():
            return self._json({"status": "error", "reason": "not_found"}, status=404)
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    port = 8787
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Serving Chungbuk flood digital twin at http://127.0.0.1:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

