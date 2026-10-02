"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .fee_service import FeeService
from .service import DomainService
from .storage import Database


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None, fee: FeeService | None = None
          ) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    fee = fee or FeeService(service.database)
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        status, payload = _fee_route(fee, method, parsed, body, actor_id)
        if status is not None:
            return status, payload
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _receipt_status(receipt) -> tuple[int, dict[str, Any]]:
    return (200 if receipt.replayed else 201), receipt.__dict__


def _fee_route(fee: FeeService, method: str, parsed, body: dict[str, Any],
               actor_id: str) -> tuple[int | None, dict[str, Any]]:
    """销售费用承诺与分配系统的路由表。"""

    path = parsed.path
    query = parse_qs(parsed.query)

    if method == "POST":
        table = {
            "/products": fee.register_product,
            "/lifecycle-versions": fee.set_lifecycle,
            "/annual-budgets": fee.set_annual_budget,
            "/period-budgets": fee.set_period_budget,
            "/activities": fee.create_activity,
            "/activities/cancel": fee.cancel_activity,
            "/commitments": fee.create_commitment,
            "/allocations": fee.generate_allocation,
            "/shares/confirm": fee.confirm_share,
            "/shares/dispute": fee.raise_dispute,
            "/shares/dispute-resolve": fee.resolve_dispute,
            "/accruals": fee.record_accrual,
            "/invoices": fee.record_invoice,
            "/refunds": fee.record_refund,
            "/settlements": fee.settle_commitment,
            "/exceptions": fee.request_exception,
            "/exceptions/decide": fee.decide_exception,
            "/exceptions/revoke": fee.revoke_exception,
        }
        handler = table.get(path)
        if handler is not None:
            return _receipt_status(handler(actor_id=actor_id, **body))
    if method == "GET" and path == "/lifecycle-versions":
        product_id = query.get("product_id", [""])[0]
        if not product_id:
            raise ValidationError("product_id 不能为空")
        return 200, {"items": fee.list_lifecycle_versions(product_id)}
    if method == "GET" and path == "/budget-status":
        product_id = query.get("product_id", [""])[0]
        period = query.get("period", [""])[0]
        if not product_id or not period:
            raise ValidationError("product_id 和 period 不能为空")
        return 200, fee.budget_status(product_id, period)
    if method == "GET" and path.startswith("/commitments/"):
        commitment_id = path.rsplit("/", 1)[1]
        if path.endswith("/trace"):
            commitment_id = path.split("/")[2]
            return 200, fee.commitment_trace(commitment_id)
        return 200, fee.get_commitment(commitment_id)
    return None, {}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService
    fee: FeeService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")},
                                fee=self.fee)
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DomainService(database)
    Handler.fee = FeeService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
