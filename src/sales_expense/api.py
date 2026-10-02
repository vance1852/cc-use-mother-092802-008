"""销售费用域的 HTTP/JSON 边界，风格与基础服务一致。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from beverage_ops_foundation.api import route as foundation_route
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.storage import Database

from .errors import ExpenseError
from .service import ExpenseService
from .storage import ensure_expense_schema


def route(service: ExpenseService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None,
          foundation: DomainService | None = None) -> tuple[int, dict[str, Any]]:
    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")
    p = parsed.path
    if p == "/health":
        from beverage_ops_foundation.audit import verify_chain
        ok, events = verify_chain(service.database.connection)
        return 200, {"status": "ok", "audit_valid": ok, "audit_events": events}
    # 费用域之外的路径（组织、操作者、场所、基础资料、审计事件）交给基础服务
    if not p.startswith("/expense"):
        if foundation is not None:
            return foundation_route(foundation, method, path, body, headers)
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    try:
        # ---- 产品与生命周期 ----
        if method == "POST" and p == "/expense/products":
            r = service.register_product(actor_id=actor_id, **body)
            return 200 if r.get("replayed") else 201, r
        if method == "POST" and p == "/expense/lifecycle-versions":
            r = service.publish_lifecycle_version(actor_id=actor_id, **body)
            return 200 if r.get("replayed") else 201, r
        if method == "GET" and p == "/expense/lifecycle-versions":
            product_id = query.get("product_id", [""])[0]
            on_date = query.get("on_date", [None])[0]
            if not product_id:
                raise ExpenseError("product_id 不能为空")
            if on_date:
                return 200, service.effective_version(product_id, on_date)
            return 200, {"items": service.list_lifecycle_versions(product_id)}

        # ---- 额度 ----
        if method == "POST" and p == "/expense/budgets/annual":
            r = service.set_annual_budget(actor_id=actor_id, **body)
            return 200 if r.get("replayed") else 201, r
        if method == "POST" and p == "/expense/budgets/periods":
            r = service.set_period_budget(actor_id=actor_id, **body)
            return 200 if r.get("replayed") else 201, r
        if method == "GET" and p == "/expense/budgets":
            return 200, service.budget_status(query["product_id"][0], int(query["year"][0]))

        # ---- 活动与受益范围 ----
        if method == "POST" and p == "/expense/campaigns":
            r = service.create_campaign(actor_id=actor_id, **body)
            return 200 if r.get("replayed") else 201, r
        if method == "POST" and p == "/expense/beneficiaries":
            r = service.set_beneficiaries(actor_id=actor_id, **body)
            return 200 if r.get("replayed") else 201, r

        # ---- 承诺与分摊 ----
        if method == "POST" and p == "/expense/commitments":
            r = service.create_commitment(actor_id=actor_id, **body)
            return 200 if r.get("replayed") else 201, r
        if method == "POST" and p == "/expense/allocations/redistribute":
            r = service.redistribute(actor_id=actor_id, **body)
            return 200 if r.get("replayed") else 201, r
        if method == "POST" and p == "/expense/shares/confirm":
            r = service.confirm_share(actor_id=actor_id, **body)
            return 200 if r.get("replayed") else 200, r
        if method == "POST" and p == "/expense/shares/reject":
            r = service.reject_share(actor_id=actor_id, **body)
            return 200, r
        if method == "POST" and p == "/expense/shares/settle":
            r = service.settle_share(actor_id=actor_id, **body)
            return 200, r

        # ---- 发票 / 应计 / 退款 / 取消 ----
        if method == "POST" and p == "/expense/invoices":
            r = service.register_invoice(actor_id=actor_id, **body)
            return 200 if r.get("replayed") else 201, r
        if method == "POST" and p == "/expense/accruals":
            r = service.book_accrual(actor_id=actor_id, **body)
            return 200 if r.get("replayed") else 201, r
        if method == "POST" and p == "/expense/refunds":
            r = service.register_refund(actor_id=actor_id, **body)
            return 200 if r.get("replayed") else 201, r
        if method == "POST" and p == "/expense/commitments/cancel":
            r = service.cancel_commitment(actor_id=actor_id, **body)
            return 200, r
        if method == "POST" and p == "/expense/periods/settle":
            r = service.settle_period(actor_id=actor_id, **body)
            return 200, r

        # ---- 争议 ----
        if method == "POST" and p == "/expense/disputes":
            r = service.raise_dispute(actor_id=actor_id, **body)
            return 200 if r.get("replayed") else 201, r
        if method == "POST" and p == "/expense/disputes/resolve":
            r = service.resolve_dispute(actor_id=actor_id, **body)
            return 200, r

        # ---- 超预算例外 ----
        if method == "POST" and p == "/expense/exceptions":
            r = service.request_exception(actor_id=actor_id, **body)
            return 200 if r.get("replayed") else 201, r
        if method == "POST" and p == "/expense/exceptions/decide":
            r = service.decide_exception(actor_id=actor_id, **body)
            return 200, r

        # ---- 全过程追踪 ----
        if method == "GET" and p == "/expense/trace":
            commitment_id = query.get("commitment_id", [""])[0]
            if not commitment_id:
                raise ExpenseError("commitment_id 不能为空")
            return 200, service.expense_trace(commitment_id)

        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except ExpenseError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    service: ExpenseService
    foundation: DomainService

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
                                foundation=self.foundation)
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


def build_service(database_path: str) -> tuple[Database, DomainService, ExpenseService]:
    database = Database(database_path)
    ensure_expense_schema(database.connection)
    foundation = DomainService(database)
    return database, foundation, ExpenseService(database)


def main() -> int:
    parser = argparse.ArgumentParser(description="启动销售费用承诺与分配服务")
    parser.add_argument("--database", default="expense.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    database, foundation, service = build_service(args.database)
    Handler.service = service
    Handler.foundation = foundation
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
