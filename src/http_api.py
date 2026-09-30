from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .domain import (ConflictError, DomainError, NotFoundError, PermissionDenied,
                     ValidationError)
from .service import Service


def make_handler(service: Service, static_dir: str):
    root = Path(static_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ModularHell/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, path: Path) -> None:
            if not path.exists():
                self._json(404, {"error": "not_found"})
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _identity(self) -> Tuple[str, str]:
            return self.headers.get("X-Actor", ""), self.headers.get("X-Role", "")

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            if length > 2_000_000:
                raise ValidationError("请求体过大")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体不是有效JSON") from exc
            if not isinstance(value, dict):
                raise ValidationError("请求体必须是JSON对象")
            return value

        def _send_error(self, exc: Exception) -> None:
            if isinstance(exc, ValidationError):
                status = 422
            elif isinstance(exc, NotFoundError):
                status = 404
            elif isinstance(exc, PermissionDenied):
                status = 403
            elif isinstance(exc, ConflictError):
                status = 409
            elif isinstance(exc, ValueError):
                status = 422
            elif isinstance(exc, DomainError):
                status = 400
            else:
                status = 500
            body = {"error": exc.__class__.__name__, "message": str(exc)}
            if getattr(exc, "details", None) is not None:
                body["details"] = exc.details
            self._json(status, body)

        def do_GET(self) -> None:
            try:
                path = urlparse(self.path).path
                parts = [p for p in path.split("/") if p]
                actor, role = self._identity()
                del actor
                if path == "/health":
                    self._json(200, {"status": "ok"})
                elif path == "/":
                    self._html(root / "index.html")
                elif parts == ["api", "items"]:
                    self._json(200, {"items": service.list_items(role)})
                elif parts == ["api", "batches"]:
                    self._json(200, {"batches": service.list_batches(role)})
                elif parts == ["api", "resources"]:
                    self._json(200, {"resources": service.list_resources(role)})
                elif parts == ["api", "audit"]:
                    self._json(200, {"events": service.audit(role)})
                elif len(parts) == 3 and parts[0] == "api" and parts[1] == "batches":
                    self._json(200, service.get_batch(parts[2], role))
                elif len(parts) == 4 and parts[0] == "api" and parts[1] == "items":
                    item_id = int(parts[2])
                    if parts[3] == "records":
                        self._json(200, {"records": service.list_records(item_id, role)})
                    elif parts[3] == "fire-lines":
                        self._json(200, {"fire_lines": service.list_fire_lines(item_id, role)})
                    elif parts[3] == "wind-records":
                        self._json(200, {"wind_records": service.list_wind_records(item_id, role)})
                    elif parts[3] == "task-areas":
                        self._json(200, {"task_areas": service.list_task_areas(item_id, role)})
                    elif parts[3] == "tasks":
                        self._json(200, {"tasks": service.list_tasks(item_id, role)})
                    else:
                        self._json(404, {"error": "not_found"})
                elif len(parts) == 4 and parts[0] == "api" and parts[1] == "resources" and parts[3] == "occupancies":
                    self._json(200, {"occupancies": service.list_occupancies(int(parts[2]), role)})
                elif len(parts) == 3 and parts[0] == "api" and parts[1] == "tasks":
                    self._json(200, service.get_task(int(parts[2]), role))
                elif len(parts) == 2 and parts[0] == "api" and parts[1] == "items":
                    self._json(200, service.get_item(int(parts[1]), role))
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                parts = [p for p in path.split("/") if p]
                actor, role = self._identity()
                body = self._body()
                if parts == ["api", "items"]:
                    self._json(201, service.create_item(body, actor, role))
                elif parts == ["api", "batches"]:
                    result = service.submit_batch(body, actor, role)
                    status_map = {"merged": 201, "idempotent": 200,
                                  "conflict": 409, "failed": 202}
                    self._json(status_map[result["outcome"]], result)
                elif parts == ["api", "resources"]:
                    self._json(201, service.create_resource(body, actor, role))
                elif len(parts) == 4 and parts[0] == "api" and parts[1] == "items":
                    item_id = int(parts[2])
                    if parts[3] == "records":
                        self._json(201, service.add_record(item_id, body, actor, role))
                    elif parts[3] == "fire-lines":
                        self._json(201, service.create_fire_line(item_id, body, actor, role))
                    elif parts[3] == "wind-records":
                        self._json(201, service.create_wind_record(item_id, body, actor, role))
                    elif parts[3] == "task-areas":
                        self._json(201, service.create_task_area(item_id, body, actor, role))
                    elif parts[3] == "tasks":
                        self._json(201, service.create_task(item_id, body, actor, role))
                    elif parts[3] == "transition":
                        self._json(200, service.transition(
                            item_id, body.get("target"), body.get("expected_version"),
                            actor, role))
                    else:
                        self._json(404, {"error": "not_found"})
                elif len(parts) == 5 and parts[0] == "api" and parts[1] == "batches" and parts[3] == "retry":
                    result = service.retry_batch(parts[2], actor, role)
                    status_map = {"merged": 201, "failed": 202}
                    self._json(status_map[result["outcome"]], result)
                elif len(parts) == 4 and parts[0] == "api" and parts[1] == "resources" and parts[3] == "occupancies":
                    self._json(201, service.create_occupancy(int(parts[2]), body, actor, role))
                elif len(parts) == 4 and parts[0] == "api" and parts[1] == "occupancies" and parts[3] == "release":
                    self._json(200, service.release_occupancy(int(parts[2]), actor, role))
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_PATCH(self) -> None:
            try:
                path = urlparse(self.path).path
                parts = [p for p in path.split("/") if p]
                actor, role = self._identity()
                body = self._body()
                if len(parts) == 3 and parts[0] == "api" and parts[1] == "fire-lines":
                    self._json(200, service.update_fire_line(int(parts[2]), body, actor, role))
                elif len(parts) == 3 and parts[0] == "api" and parts[1] == "wind-records":
                    self._json(200, service.update_wind_record(int(parts[2]), body, actor, role))
                elif len(parts) == 3 and parts[0] == "api" and parts[1] == "task-areas":
                    self._json(200, service.update_task_area(int(parts[2]), body, actor, role))
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

    return Handler
