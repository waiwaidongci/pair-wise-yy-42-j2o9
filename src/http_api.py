from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Optional, Tuple
from urllib.parse import urlparse

from .domain import (ConflictError, DomainError, NotFoundError, PermissionDenied,
                     ValidationError)
from .service import Service


def make_handler(service: Service, static_dir: str):
    root = Path(static_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "WildfireCommand/1.0"

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

        def _body(self) -> dict:
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
                # 资源争用/批次待重试等冲突携带结构化上下文
                payload = {"error": exc.__class__.__name__,
                           "message": exc.message, "status": "conflict"}
                if getattr(exc, "payload", None):
                    payload.update(exc.payload)
                self._json(409, payload)
                return
            elif isinstance(exc, ValueError):
                status = 422
            elif isinstance(exc, DomainError):
                status = 400
            else:
                status = 500
            self._json(status, {"error": exc.__class__.__name__,
                                "message": str(exc)})

        @staticmethod
        def _segments(path: str):
            return [seg for seg in path.split("/") if seg != ""]

        def do_GET(self) -> None:
            try:
                path = urlparse(self.path).path
                segs = self._segments(path)
                actor, role = self._identity()
                del actor
                if path == "/health":
                    self._json(200, {"status": "ok"})
                elif path == "/":
                    self._html(root / "index.html")
                elif segs == ["api", "resources"]:
                    self._json(200, service.list_resources(role))
                elif segs == ["api", "tickets"]:
                    self._json(200, {"tickets": service.list_tickets(role)})
                elif len(segs) == 3 and segs[:2] == ["api", "tickets"]:
                    self._json(200, service.get_ticket_view(segs[2], role))
                elif segs == ["api", "batches"]:
                    self._json(200, {"batches": service.list_batches(role)})
                elif len(segs) == 3 and segs[:2] == ["api", "batches"]:
                    self._json(200, service.get_batch_view(segs[2], role))
                elif segs == ["api", "reviews"]:
                    self._json(200, {"reviews": service.list_reviews(role)})
                elif segs == ["api", "audit"]:
                    self._json(200, {"events": service.audit(role)})
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                segs = self._segments(path)
                actor, role = self._identity()
                body = self._body()
                if segs == ["api", "resources"]:
                    self._json(201, service.register_resource(body, actor, role))
                elif segs == ["api", "tickets"]:
                    self._json(201, service.register_ticket(body, actor, role))
                elif (len(segs) == 4 and segs[:2] == ["api", "tickets"]
                      and segs[3] == "occupations"):
                    self._json(200, service.occupy(segs[2], body, actor, role))
                elif (len(segs) == 4 and segs[:2] == ["api", "tickets"]
                      and segs[3] == "amend"):
                    self._json(200, service.amend_ticket(segs[2], body, actor, role))
                elif (len(segs) == 4 and segs[:2] == ["api", "tickets"]
                      and segs[3] == "close"):
                    self._json(200, service.close_ticket(segs[2], actor, role))
                elif segs == ["api", "batches"]:
                    result = service.submit_batch(body, actor, role)
                    # 已受理但写入失败待重试 -> 202，现场可凭batch_no下次重试
                    self._json(202 if result.get("retryable") else 200, result)
                elif (len(segs) == 4 and segs[:2] == ["api", "batches"]
                      and segs[3] == "retry"):
                    result = service.retry_batch(segs[2], actor, role)
                    self._json(202 if result.get("retryable") else 200, result)
                elif (len(segs) == 4 and segs[:2] == ["api", "reviews"]
                      and segs[3] == "resolve"):
                    self._json(200, service.resolve_review(int(segs[2]), body,
                                                           actor, role))
                elif (len(segs) == 4 and segs[:2] == ["api", "occupations"]
                      and segs[3] == "release"):
                    self._json(200, service.release_occupation(int(segs[2]), actor,
                                                               role))
                elif (len(segs) == 4 and segs[:2] == ["api", "tasks"]
                      and segs[3] == "transition"):
                    self._json(200, service.transition_task(
                        int(segs[2]), body.get("target"), actor, role))
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

    return Handler
