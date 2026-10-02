"""销售费用承诺与分配的领域服务。

核心不变量：
- 产品份额在确认时快照生命周期版本、阶段与期间额度；之后生命周期切换不改变已确认/已发生费用。
- 只有份额负责人本人能确认自己的份额；确认才占用额度，草案不占用。
- 争议只冻结相关产品份额，其余份额照常结账。
- 取消、退款、跨期发票都回溯到原承诺与原份额，不新建孤立事实。
- 超预算必须凭限定金额、期限与逐级授权链的例外才能占用。

金额全程为整数最小货币单位（分）；期间键为 `YYYYMmm`（月度）。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, timedelta
from typing import Any, Callable

from beverage_ops_foundation.audit import append_event, canonical_json, digest
from beverage_ops_foundation.clock import Clock, SystemClock
from beverage_ops_foundation.models import Actor

from . import allocation
from .allocation import BenefitInput, allocate, explain
from .errors import (AuthorizationChainError, BudgetExceeded, ConflictError, NotFoundError,
                     PermissionDenied, ValidationError, WorkflowError)
from .storage import ensure_expense_schema

STAGES = ("mature", "new", "nurturing")
TEAMS = ("brand", "region", "channel")
PERIOD_RE = re.compile(r"^(\d{4})M(0[1-9]|1[0-2])$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
COMMITMENT_OPEN = ("requested", "approved")


class ExpenseService:
    def __init__(self, database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        ensure_expense_schema(database.connection)

    # ---------- 基础工具 ----------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _today(self) -> str:
        return self.clock.now().date().isoformat()

    def _id(self, value: str, field: str) -> str:
        value = str(value or "").strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 300) -> str:
        value = str(value or "").strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _amount(self, value: Any, field: str = "amount") -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{field} 必须是整数金额（分）")
        if value < 0:
            raise ValidationError(f"{field} 不能为负")
        return value

    def _period(self, value: str) -> str:
        value = str(value or "").strip()
        if not PERIOD_RE.fullmatch(value):
            raise ValidationError("期间键格式应为 YYYYMmm，例如 2026M03")
        return value

    def _period_year(self, period_key: str) -> int:
        return int(period_key[:4])

    def _actor(self, conn, actor_id: str) -> Actor:
        row = conn.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require_role(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied(f"当前角色不能执行该动作，需要：{'/'.join(roles)}")

    def _idempotent(self, conn, *, request_id: str, action: str, payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
        request_id = self._id(request_id, "request_id")
        payload_hash = digest(payload)
        row = conn.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {"request_id": request_id, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"], "replayed": True,
                    **json.loads(row["response_json"])}
        resource_type, resource_id, response = create()
        conn.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {"request_id": request_id, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False, **response}

    def _audit(self, conn, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(conn, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    def _row(self, conn, sql: str, params: tuple[Any, ...], message: str):
        row = conn.execute(sql, params).fetchone()
        if row is None:
            raise NotFoundError(message)
        return row

    # ---------- 产品与生命周期版本 ----------

    def register_product(self, *, request_id: str, actor_id: str, product_id: str,
                         organization_id: str, name: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "product_id": product_id, "name": name}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin", "operator")
            product_id = self._id(product_id, "product_id")
            name = self._text(name, "name")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO exp_products(product_id,organization_id,name,created_by,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (product_id, organization_id, name, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("产品编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="expense.product.registered",
                            resource_type="product", resource_id=product_id, detail={"name": name})
                return "product", product_id, {"product_id": product_id}

            return self._idempotent(conn, request_id=request_id, action="register_product",
                                    payload=payload, create=create)

    def publish_lifecycle_version(self, *, request_id: str, actor_id: str, product_id: str,
                                  stage: str, effective_from: str, note: str | None = None) -> dict[str, Any]:
        """发布新生效版本；当前 active 版本在生效日前一日终止并标记被取代。"""
        payload = {"actor_id": actor_id, "product_id": product_id, "stage": stage,
                   "effective_from": effective_from}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin")
            self._row(conn, "SELECT 1 FROM exp_products WHERE product_id=?", (product_id,), "产品不存在")
            if stage not in STAGES:
                raise ValidationError("stage 必须是 mature/new/nurturing")
            if not DATE_RE.fullmatch(effective_from or ""):
                raise ValidationError("effective_from 必须是 YYYY-MM-DD")
            current = conn.execute(
                "SELECT * FROM exp_lifecycle_versions WHERE product_id=? AND status='active' "
                "ORDER BY effective_from DESC LIMIT 1", (product_id,)).fetchone()
            if current and effective_from <= current["effective_from"]:
                raise ValidationError("新生效日期必须晚于当前生效版本")

            def create() -> tuple[str, str, dict[str, Any]]:
                version_id = uuid.uuid4().hex
                if current:
                    y, m, d = (int(x) for x in effective_from.split("-"))
                    end_day = (date(y, m, d) - timedelta(days=1)).isoformat()
                    conn.execute(
                        "UPDATE exp_lifecycle_versions SET status='superseded',effective_to=?,"
                        "superseded_by=? WHERE version_id=?",
                        (end_day, version_id, current["version_id"]),
                    )
                conn.execute(
                    "INSERT INTO exp_lifecycle_versions(version_id,product_id,stage,effective_from,"
                    "effective_to,status,superseded_by,note,created_by,created_at) "
                    "VALUES(?,?,?,?,NULL,'active',NULL,?,?,?)",
                    (version_id, product_id, stage, effective_from, note, actor_id, self._now()),
                )
                self._audit(conn, actor_id=actor_id, action="expense.lifecycle.published",
                            resource_type="lifecycle_version", resource_id=version_id,
                            detail={"product_id": product_id, "stage": stage,
                                    "effective_from": effective_from,
                                    "superseded": current["version_id"] if current else None})
                return "lifecycle_version", version_id, {"version_id": version_id}

            return self._idempotent(conn, request_id=request_id, action="publish_lifecycle",
                                    payload=payload, create=create)

    def effective_version(self, product_id: str, on_date: str | None = None) -> dict[str, Any]:
        on_date = on_date or self._today()
        row = self.database.connection.execute(
            "SELECT * FROM exp_lifecycle_versions WHERE product_id=? AND effective_from<=? "
            "ORDER BY effective_from DESC LIMIT 1", (product_id, on_date)).fetchone()
        if row is None:
            raise NotFoundError("该日期之前没有生效的生命周期版本")
        return {"version_id": row["version_id"], "product_id": row["product_id"],
                "stage": row["stage"], "effective_from": row["effective_from"],
                "effective_to": row["effective_to"], "status": row["status"]}

    def list_lifecycle_versions(self, product_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.database.connection.execute(
            "SELECT version_id,product_id,stage,effective_from,effective_to,status,superseded_by,note "
            "FROM exp_lifecycle_versions WHERE product_id=? ORDER BY effective_from", (product_id,))]

    # ---------- 年度与期间额度 ----------

    def set_annual_budget(self, *, request_id: str, actor_id: str, product_id: str, year: int,
                          stage: str, amount: int, currency: str = "CNY") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "product_id": product_id, "year": year,
                   "stage": stage, "amount": amount}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin")
            self._row(conn, "SELECT 1 FROM exp_products WHERE product_id=?", (product_id,), "产品不存在")
            if stage not in STAGES:
                raise ValidationError("stage 必须是 mature/new/nurturing")
            if not isinstance(year, int) or not 2000 <= year <= 2100:
                raise ValidationError("year 不合法")
            amount = self._amount(amount, "amount")
            currency = self._text(currency, "currency", 8)
            used = conn.execute(
                "SELECT COALESCE(SUM(COALESCE(r.reserved_amount,0)+COALESCE(r.occurred_amount,0)),0) AS used "
                "FROM exp_budget_annual b LEFT JOIN exp_budget_periods p ON p.budget_id=b.budget_id "
                "LEFT JOIN exp_budget_reservations r ON r.period_budget_id=p.period_id "
                "WHERE b.product_id=? AND b.year=? AND b.stage=?",
                (product_id, year, stage)).fetchone()["used"]
            if used > amount:
                raise ConflictError("额度不能下调到已占用/已发生金额以下")
            existing = conn.execute(
                "SELECT budget_id FROM exp_budget_annual WHERE product_id=? AND year=? AND stage=?",
                (product_id, year, stage)).fetchone()

            def create() -> tuple[str, str, dict[str, Any]]:
                if existing:
                    budget_id = existing["budget_id"]
                    conn.execute("UPDATE exp_budget_annual SET amount=?,currency=? WHERE budget_id=?",
                                 (amount, currency, budget_id))
                else:
                    budget_id = uuid.uuid4().hex
                    conn.execute(
                        "INSERT INTO exp_budget_annual(budget_id,product_id,year,stage,amount,currency,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (budget_id, product_id, year, stage, amount, currency, actor_id, self._now()),
                    )
                self._audit(conn, actor_id=actor_id, action="expense.budget.annual_set",
                            resource_type="annual_budget", resource_id=budget_id,
                            detail={"product_id": product_id, "year": year, "stage": stage, "amount": amount})
                return "annual_budget", budget_id, {"budget_id": budget_id, "amount": amount}

            return self._idempotent(conn, request_id=request_id, action="set_annual_budget",
                                    payload=payload, create=create)

    def set_period_budget(self, *, request_id: str, actor_id: str, budget_id: str,
                          period_key: str, amount: int) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "budget_id": budget_id, "period_key": period_key, "amount": amount}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin")
            budget = self._row(conn, "SELECT * FROM exp_budget_annual WHERE budget_id=?",
                               (budget_id,), "年度额度不存在")
            period_key = self._period(period_key)
            if self._period_year(period_key) != budget["year"]:
                raise ValidationError("期间不属于该年度额度")
            amount = self._amount(amount, "amount")
            existing = conn.execute(
                "SELECT p.period_id, COALESCE(r.reserved_amount,0)+COALESCE(r.occurred_amount,0) AS used "
                "FROM exp_budget_periods p LEFT JOIN exp_budget_reservations r "
                "ON r.period_budget_id=p.period_id WHERE p.budget_id=? AND p.period_key=?",
                (budget_id, period_key)).fetchone()
            if existing and existing["used"] > amount:
                raise ConflictError("期间额度不能下调到已占用/已发生金额以下")

            def create() -> tuple[str, str, dict[str, Any]]:
                if existing:
                    period_id = existing["period_id"]
                    conn.execute("UPDATE exp_budget_periods SET amount=? WHERE period_id=?",
                                 (amount, period_id))
                else:
                    period_id = uuid.uuid4().hex
                    conn.execute(
                        "INSERT INTO exp_budget_periods(period_id,budget_id,period_key,amount,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (period_id, budget_id, period_key, amount, actor_id, self._now()),
                    )
                self._audit(conn, actor_id=actor_id, action="expense.budget.period_set",
                            resource_type="period_budget", resource_id=period_id,
                            detail={"budget_id": budget_id, "period_key": period_key, "amount": amount})
                return "period_budget", period_id, {"period_id": period_id, "amount": amount}

            return self._idempotent(conn, request_id=request_id, action="set_period_budget",
                                    payload=payload, create=create)

    def budget_status(self, product_id: str, year: int) -> dict[str, Any]:
        out: list[dict[str, Any]] = []
        for annual in self.database.connection.execute(
                "SELECT * FROM exp_budget_annual WHERE product_id=? AND year=? ORDER BY stage",
                (product_id, year)):
            periods = []
            for p in self.database.connection.execute(
                    "SELECT p.period_id,p.period_key,p.amount AS budget,COALESCE(r.reserved_amount,0) AS reserved,"
                    "COALESCE(r.occurred_amount,0) AS occurred FROM exp_budget_periods p "
                    "LEFT JOIN exp_budget_reservations r ON r.period_budget_id=p.period_id "
                    "WHERE p.budget_id=? ORDER BY p.period_key", (annual["budget_id"],)):
                periods.append({"period_key": p["period_key"], "budget": p["budget"],
                                "reserved": p["reserved"], "occurred": p["occurred"],
                                "available": p["budget"] - p["reserved"] - p["occurred"]})
            out.append({"budget_id": annual["budget_id"], "stage": annual["stage"],
                        "annual_amount": annual["amount"], "currency": annual["currency"], "periods": periods})
        return {"product_id": product_id, "year": year, "budgets": out}

    # ---------- 活动、受益范围与承诺 ----------

    def create_campaign(self, *, request_id: str, actor_id: str, campaign_id: str,
                        organization_id: str, name: str, owner_team: str,
                        starts_on: str, ends_on: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "campaign_id": campaign_id, "name": name,
                   "owner_team": owner_team}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin", "operator")
            campaign_id = self._id(campaign_id, "campaign_id")
            name = self._text(name, "name")
            if owner_team not in TEAMS:
                raise ValidationError("owner_team 必须是 brand/region/channel")
            if not DATE_RE.fullmatch(starts_on or "") or not DATE_RE.fullmatch(ends_on or ""):
                raise ValidationError("日期必须是 YYYY-MM-DD")
            if ends_on < starts_on:
                raise ValidationError("结束日期不能早于开始日期")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO exp_campaigns(campaign_id,organization_id,name,owner_team,owner_actor_id,"
                        "status,starts_on,ends_on,created_by,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,'draft',?,?,?,?,?)",
                        (campaign_id, organization_id, name, owner_team, actor_id,
                         starts_on, ends_on, actor_id, self._now(), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("活动编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="expense.campaign.created",
                            resource_type="campaign", resource_id=campaign_id,
                            detail={"name": name, "owner_team": owner_team})
                return "campaign", campaign_id, {"campaign_id": campaign_id}

            return self._idempotent(conn, request_id=request_id, action="create_campaign",
                                    payload=payload, create=create)

    def set_beneficiaries(self, *, request_id: str, actor_id: str, campaign_id: str,
                          beneficiaries: list[dict[str, Any]]) -> dict[str, Any]:
        """登记活动受益范围：[{product_id, factor, owner_actor_id, override_amount?}]。"""
        payload = {"actor_id": actor_id, "campaign_id": campaign_id, "beneficiaries": beneficiaries}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin", "operator")
            self._row(conn, "SELECT 1 FROM exp_campaigns WHERE campaign_id=?", (campaign_id,), "活动不存在")
            if conn.execute(
                    "SELECT 1 FROM exp_commitments WHERE campaign_id=? AND status IN ('requested','approved')",
                    (campaign_id,)).fetchone():
                raise ConflictError("活动已有进行中的承诺，不能整体改写受益范围")
            if not isinstance(beneficiaries, list) or not beneficiaries:
                raise ValidationError("beneficiaries 必须是非空数组")
            normalized: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in beneficiaries:
                pid = self._id(item.get("product_id", ""), "product_id")
                if pid in seen:
                    raise ValidationError(f"受益产品 {pid} 重复")
                seen.add(pid)
                self._row(conn, "SELECT 1 FROM exp_products WHERE product_id=?", (pid,), f"产品 {pid} 不存在")
                owner = self._id(item.get("owner_actor_id", ""), "owner_actor_id")
                self._actor(conn, owner)
                factor = item.get("factor", 0)
                if isinstance(factor, bool) or not isinstance(factor, int) or factor < 0:
                    raise ValidationError("factor 必须是非负整数")
                override = item.get("override_amount")
                if override is not None:
                    override = self._amount(override, "override_amount")
                normalized.append({"product_id": pid, "owner_actor_id": owner,
                                   "factor": factor, "override_amount": override})

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute("DELETE FROM exp_beneficiaries WHERE campaign_id=?", (campaign_id,))
                for item in normalized:
                    conn.execute(
                        "INSERT INTO exp_beneficiaries(line_id,campaign_id,product_id,factor,"
                        "override_amount,owner_actor_id,created_at) VALUES(?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, campaign_id, item["product_id"], item["factor"],
                         item["override_amount"], item["owner_actor_id"], self._now()),
                    )
                conn.execute("UPDATE exp_campaigns SET status='requested',updated_at=? WHERE campaign_id=?",
                             (self._now(), campaign_id))
                self._audit(conn, actor_id=actor_id, action="expense.beneficiaries.set",
                            resource_type="campaign", resource_id=campaign_id,
                            detail={"products": [i["product_id"] for i in normalized]})
                return "beneficiaries", campaign_id, {"campaign_id": campaign_id,
                                                      "count": len(normalized)}

            return self._idempotent(conn, request_id=request_id, action="set_beneficiaries",
                                    payload=payload, create=create)

    def create_commitment(self, *, request_id: str, actor_id: str, campaign_id: str,
                          period_key: str, total_amount: int, rule_name: str = allocation.RULE_FACTOR,
                          contract_ref: str | None = None, vendor: str | None = None,
                          currency: str = "CNY") -> dict[str, Any]:
        """登记合同承诺并立即按可解释规则形成各产品分摊草案（草案不占用额度）。"""
        payload = {"actor_id": actor_id, "campaign_id": campaign_id, "period_key": period_key,
                   "total_amount": total_amount, "rule_name": rule_name}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin", "operator")
            self._row(conn, "SELECT 1 FROM exp_campaigns WHERE campaign_id=?", (campaign_id,), "活动不存在")
            period_key = self._period(period_key)
            total_amount = self._amount(total_amount, "total_amount")
            if total_amount <= 0:
                raise ValidationError("total_amount 必须大于零")
            if rule_name not in allocation.RULES:
                raise ValidationError(f"未知分摊规则：{rule_name}")
            benefit_rows = conn.execute(
                "SELECT * FROM exp_beneficiaries WHERE campaign_id=? ORDER BY product_id",
                (campaign_id,)).fetchall()
            if not benefit_rows:
                raise ValidationError("活动尚未登记受益范围")
            if conn.execute(
                    "SELECT 1 FROM exp_commitments WHERE campaign_id=? AND status IN ('requested','approved')",
                    (campaign_id,)).fetchone():
                raise ConflictError("该活动已有进行中的承诺")
            benefits = [BenefitInput(product_id=r["product_id"], owner_actor_id=r["owner_actor_id"],
                                     factor=r["factor"], override_amount=r["override_amount"])
                        for r in benefit_rows]
            result = allocate(rule_name, total_amount, benefits)
            line_ids = {r["product_id"]: r["line_id"] for r in benefit_rows}

            def create() -> tuple[str, str, dict[str, Any]]:
                commitment_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO exp_commitments(commitment_id,campaign_id,period_key,total_amount,currency,"
                    "contract_ref,vendor,status,requested_at,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,'requested',?,?,?)",
                    (commitment_id, campaign_id, period_key, total_amount, currency,
                     contract_ref, vendor, self._now(), actor_id, self._now()),
                )
                self._write_shares(conn, commitment_id, result, period_key, line_ids)
                conn.execute(
                    "INSERT INTO exp_allocation_runs(run_id,commitment_id,kind,rule_name,summary_json,"
                    "rounding_gap,created_by,created_at) VALUES(?,?, 'draft', ?,?,?,?,?)",
                    (uuid.uuid4().hex, commitment_id, rule_name,
                     canonical_json(explain(result)), result.rounding_gap, actor_id, self._now()),
                )
                conn.execute("UPDATE exp_campaigns SET status='approved',updated_at=? WHERE campaign_id=?",
                             (self._now(), campaign_id))
                self._audit(conn, actor_id=actor_id, action="expense.commitment.created",
                            resource_type="commitment", resource_id=commitment_id,
                            detail={"campaign_id": campaign_id, "period_key": period_key,
                                    "total_amount": total_amount, "rule": rule_name})
                return "commitment", commitment_id, {"commitment_id": commitment_id,
                                                     "allocation": explain(result)}

            return self._idempotent(conn, request_id=request_id, action="create_commitment",
                                    payload=payload, create=create)

    def _write_shares(self, conn, commitment_id: str, result, period_key: str,
                      line_ids: dict[str, str]) -> None:
        for line in result.lines:
            conn.execute(
                "INSERT INTO exp_shares(share_id,commitment_id,product_id,line_id,owner_actor_id,"
                "period_key,amount,reserved_amount,status,rule_name,explanation_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,0,'draft',?,?,?,?)",
                (uuid.uuid4().hex, commitment_id, line.product_id, line_ids[line.product_id],
                 line.owner_actor_id, period_key, line.amount, line.rule_name,
                 canonical_json({"ratio": line.ratio, "reason": line.reason,
                                 "weight": line.weight, **result.explanation}),
                 self._now(), self._now()),
            )

    # ---------- 份额确认 / 拒绝 / 重新分摊 ----------

    def _ledger(self, conn, *, share, event: str, amount: int, detail: dict[str, Any], actor_id: str) -> None:
        occurred = conn.execute("SELECT COALESCE(SUM(amount),0) AS s FROM exp_accruals WHERE share_id=?",
                                (share["share_id"],)).fetchone()["s"]
        conn.execute(
            "INSERT INTO exp_share_ledger(share_id,commitment_id,product_id,event,amount,reserved_after,"
            "occurred_after,settled_after,detail_json,actor_id,occurred_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (share["share_id"], share["commitment_id"], share["product_id"], event, amount,
             share["reserved_amount"], occurred, share["settled_amount"],
             canonical_json(detail), actor_id, self._now()),
        )

    def confirm_share(self, *, request_id: str, actor_id: str, share_id: str) -> dict[str, Any]:
        """业务负责人确认自己负责的份额；此刻才按生效版本与期间额度占用预算。"""
        payload = {"actor_id": actor_id, "share_id": share_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            share = self._row(conn, "SELECT * FROM exp_shares WHERE share_id=?", (share_id,), "份额不存在")
            commitment = self._row(conn, "SELECT * FROM exp_commitments WHERE commitment_id=?",
                                   (share["commitment_id"],), "承诺不存在")
            if commitment["status"] not in COMMITMENT_OPEN:
                raise WorkflowError("承诺已关闭，不能再确认份额")
            if share["status"] != "draft":
                raise WorkflowError(f"份额当前状态为 {share['status']}，不能确认")
            if share["owner_actor_id"] != actor_id:
                raise PermissionDenied("只能确认自己负责的产品份额")

            on_date = self._today()
            version = conn.execute(
                "SELECT * FROM exp_lifecycle_versions WHERE product_id=? AND effective_from<=? "
                "ORDER BY effective_from DESC LIMIT 1", (share["product_id"], on_date)).fetchone()
            if version is None:
                raise ValidationError("产品缺少当前生效的生命周期版本，无法占用额度")
            period_key = share["period_key"]
            period_budget = conn.execute(
                "SELECT p.period_id,p.amount FROM exp_budget_annual b "
                "JOIN exp_budget_periods p ON p.budget_id=b.budget_id "
                "WHERE b.product_id=? AND b.year=? AND b.stage=? AND p.period_key=?",
                (share["product_id"], self._period_year(period_key), version["stage"], period_key)
            ).fetchone()
            if period_budget is None:
                raise ValidationError(f"缺少 {version['stage']} 阶段 {period_key} 期间额度")
            res = conn.execute("SELECT * FROM exp_budget_reservations WHERE period_budget_id=?",
                               (period_budget["period_id"],)).fetchone()
            used = (res["reserved_amount"] + res["occurred_amount"]) if res else 0
            over = used + share["amount"] - period_budget["amount"]
            exception_used: str | None = None
            if over > 0:
                exception_used = self._consume_exception(conn, commitment["commitment_id"], over)

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "UPDATE exp_shares SET status='confirmed',stage_snapshot=?,version_id=?,"
                    "period_budget_id=?,reserved_amount=amount,confirmed_by=?,confirmed_at=?,updated_at=? "
                    "WHERE share_id=?",
                    (version["stage"], version["version_id"], period_budget["period_id"],
                     actor_id, self._now(), self._now(), share_id),
                )
                conn.execute(
                    "INSERT INTO exp_budget_reservations(period_budget_id,reserved_amount,occurred_amount) "
                    "VALUES(?,?,0) ON CONFLICT(period_budget_id) DO UPDATE SET "
                    "reserved_amount=reserved_amount+?",
                    (period_budget["period_id"], share["amount"], share["amount"])),
                fresh = conn.execute("SELECT * FROM exp_shares WHERE share_id=?", (share_id,)).fetchone()
                self._ledger(conn, share=fresh, event="reserved", amount=share["amount"],
                             detail={"stage_snapshot": version["stage"], "period_key": period_key,
                                     "exception_id": exception_used}, actor_id=actor_id)
                self._maybe_approve(conn, commitment)
                self._audit(conn, actor_id=actor_id, action="expense.share.confirmed",
                            resource_type="share", resource_id=share_id,
                            detail={"commitment_id": commitment["commitment_id"],
                                    "product_id": share["product_id"], "amount": share["amount"],
                                    "stage_snapshot": version["stage"], "exception_id": exception_used})
                return "share", share_id, {"share_id": share_id, "status": "confirmed",
                                           "stage_snapshot": version["stage"],
                                           "over_amount": max(over, 0),
                                           "exception_id": exception_used}

            return self._idempotent(conn, request_id=request_id, action="confirm_share",
                                    payload=payload, create=create)

    def _consume_exception(self, conn, commitment_id: str, over: int) -> str:
        exc = conn.execute(
            "SELECT * FROM exp_exceptions WHERE commitment_id=? AND status='approved' "
            "ORDER BY created_at DESC LIMIT 1", (commitment_id,)).fetchone()
        if exc is None:
            raise BudgetExceeded(f"超出期间额度 {over} 分，且没有已批准的超预算例外")
        if self._today() > exc["valid_until"]:
            conn.execute("UPDATE exp_exceptions SET status='expired' WHERE exception_id=?",
                         (exc["exception_id"],))
            raise BudgetExceeded("超预算例外已过有效期")
        if over > exc["limit_amount"]:
            raise BudgetExceeded(f"超额 {over} 分超过例外上限 {exc['limit_amount']} 分")
        conn.execute("UPDATE exp_exceptions SET status='used',decided_at=? WHERE exception_id=?",
                     (self._now(), exc["exception_id"]))
        return exc["exception_id"]

    def _maybe_approve(self, conn, commitment) -> None:
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM exp_shares WHERE commitment_id=? AND status IN ('draft','rejected')",
            (commitment["commitment_id"],)).fetchone()["c"]
        if pending:
            return
        conn.execute("UPDATE exp_commitments SET status='approved',approved_by='system',approved_at=? "
                     "WHERE commitment_id=? AND status='requested'",
                     (self._now(), commitment["commitment_id"]))
        conn.execute("UPDATE exp_campaigns SET status='active',updated_at=? WHERE campaign_id="
                     "(SELECT campaign_id FROM exp_commitments WHERE commitment_id=?)",
                     (self._now(), commitment["commitment_id"]))

    def reject_share(self, *, request_id: str, actor_id: str, share_id: str, reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "share_id": share_id, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            share = self._row(conn, "SELECT * FROM exp_shares WHERE share_id=?", (share_id,), "份额不存在")
            commitment = self._row(conn, "SELECT * FROM exp_commitments WHERE commitment_id=?",
                                   (share["commitment_id"],), "承诺不存在")
            if commitment["status"] not in COMMITMENT_OPEN:
                raise WorkflowError("承诺已关闭")
            if share["status"] != "draft":
                raise WorkflowError("只能拒绝尚未确认的草案份额")
            if share["owner_actor_id"] != actor_id:
                raise PermissionDenied("只能拒绝自己负责的产品份额")
            reason = self._text(reason, "reason")

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute("UPDATE exp_shares SET status='rejected',updated_at=? WHERE share_id=?",
                             (self._now(), share_id))
                self._ledger(conn, share=share, event="rejected", amount=share["amount"],
                             detail={"reason": reason}, actor_id=actor_id)
                self._audit(conn, actor_id=actor_id, action="expense.share.rejected",
                            resource_type="share", resource_id=share_id,
                            detail={"reason": reason, "product_id": share["product_id"]})
                return "share", share_id, {"share_id": share_id, "status": "rejected"}

            return self._idempotent(conn, request_id=request_id, action="reject_share",
                                    payload=payload, create=create)

    def redistribute(self, *, request_id: str, actor_id: str, commitment_id: str,
                     rule_name: str | None = None) -> dict[str, Any]:
        """对尚未确认的受益产品用剩余承诺金额重新生成草案；已确认份额保持不动。"""
        payload = {"actor_id": actor_id, "commitment_id": commitment_id, "rule_name": rule_name}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin", "operator")
            commitment = self._row(conn, "SELECT * FROM exp_commitments WHERE commitment_id=?",
                                   (commitment_id,), "承诺不存在")
            if commitment["status"] != "requested":
                raise WorkflowError("只能在全部份额确认前重新分摊")
            locked_rows = conn.execute(
                "SELECT * FROM exp_shares WHERE commitment_id=? AND status='confirmed'",
                (commitment_id,)).fetchall()
            locked_total = sum(r["amount"] for r in locked_rows)
            remaining = commitment["total_amount"] - locked_total
            if remaining <= 0:
                raise WorkflowError("已确认金额已用完全部承诺")
            pending_rows = conn.execute(
                "SELECT * FROM exp_shares WHERE commitment_id=? AND status IN ('draft','rejected')",
                (commitment_id,)).fetchall()
            if not pending_rows:
                raise ConflictError("没有待重新分摊的份额")
            benefits = []
            for r in pending_rows:
                b = conn.execute("SELECT factor,override_amount FROM exp_beneficiaries WHERE campaign_id=? "
                                 "AND product_id=?", (commitment["campaign_id"], r["product_id"])).fetchone()
                benefits.append(BenefitInput(product_id=r["product_id"], owner_actor_id=r["owner_actor_id"],
                                             factor=b["factor"], override_amount=b["override_amount"]))
            chosen_rule = rule_name or pending_rows[0]["rule_name"] or allocation.RULE_FACTOR
            result = allocate(chosen_rule, remaining, benefits)
            line_ids = {r["product_id"]: r["line_id"] for r in conn.execute(
                "SELECT line_id,product_id FROM exp_beneficiaries WHERE campaign_id=?",
                (commitment["campaign_id"],))}

            def create() -> tuple[str, str, dict[str, Any]]:
                for r in pending_rows:
                    conn.execute("DELETE FROM exp_share_ledger WHERE share_id=?", (r["share_id"],))
                    conn.execute("DELETE FROM exp_shares WHERE share_id=?", (r["share_id"],))
                self._write_shares(conn, commitment_id, result, commitment["period_key"], line_ids)
                run_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO exp_allocation_runs(run_id,commitment_id,kind,rule_name,summary_json,"
                    "rounding_gap,created_by,created_at) VALUES(?,?,'redistribution',?,?,?,?,?)",
                    (run_id, commitment_id, chosen_rule, canonical_json(explain(result)),
                     result.rounding_gap, actor_id, self._now()),
                )
                self._audit(conn, actor_id=actor_id, action="expense.allocation.redistributed",
                            resource_type="allocation_run", resource_id=run_id,
                            detail={"commitment_id": commitment_id, "locked_total": locked_total,
                                    "remaining": remaining, "rule": chosen_rule})
                return "allocation_run", run_id, {"run_id": run_id, "allocation": explain(result),
                                                  "locked_total": locked_total}

            return self._idempotent(conn, request_id=request_id, action="redistribute",
                                    payload=payload, create=create)

    # ---------- 发票、应计、退款与跨期 ----------

    def _active_shares(self, conn, commitment_id: str):
        """已产生财务效力的份额（已确认/冻结/已结账）。草案份额未占用额度，不承接发票与计提事实。"""
        return conn.execute(
            "SELECT * FROM exp_shares WHERE commitment_id=? AND status IN ('confirmed','frozen','settled') "
            "ORDER BY product_id", (commitment_id,)).fetchall()

    def _allocate_to_shares(self, amount: int, shares) -> list[tuple[Any, int]]:
        weights = [(s["product_id"], s["share_id"], max(s["amount"], 0)) for s in shares]
        if sum(w for _, _, w in weights) <= 0:
            raise WorkflowError("份额金额均为零，无法归属")
        rows = allocation._largest_remainder(amount, weights)
        by_pid = {pid: a for pid, _sid, _w, a, _rem in rows}
        return [(s, by_pid[s["product_id"]]) for s in shares]

    def _budget_period(self, conn, share, period_key: str):
        """按份额确认时的阶段快照定位期间额度行（生命周期切换不改变已确认份额的阶段）。"""
        row = conn.execute(
            "SELECT p.period_id FROM exp_budget_annual b JOIN exp_budget_periods p ON p.budget_id=b.budget_id "
            "WHERE b.product_id=? AND b.year=? AND b.stage=? AND p.period_key=?",
            (share["product_id"], self._period_year(period_key), share["stage_snapshot"], period_key)
        ).fetchone()
        if row is None:
            raise ValidationError(f"产品 {share['product_id']} 缺少 {share['stage_snapshot']} 阶段 "
                                  f"{period_key} 期间额度")
        return row["period_id"]

    def _adjust_budget(self, conn, period_budget_id: str, *, reserved_delta: int = 0,
                       occurred_delta: int = 0) -> None:
        conn.execute(
            "INSERT INTO exp_budget_reservations(period_budget_id,reserved_amount,occurred_amount) "
            "VALUES(?,0,0) ON CONFLICT(period_budget_id) DO NOTHING", (period_budget_id,))
        conn.execute(
            "UPDATE exp_budget_reservations SET reserved_amount=reserved_amount+?,"
            "occurred_amount=occurred_amount+? WHERE period_budget_id=?",
            (reserved_delta, occurred_delta, period_budget_id))

    def _occur(self, conn, share, *, part: int, period_key: str, kind: str,
               invoice_id: str | None, relieve_reserve: bool) -> int:
        """登记一笔带符号的应计事实并同步份额占用与期间额度桶，返回实际冲减的占用金额。"""
        signed = part if kind in ("accrual", "invoice") else -part
        target_pb = share["period_budget_id"] if period_key == share["period_key"] \
            else self._budget_period(conn, share, period_key)
        conn.execute(
            "INSERT INTO exp_accruals(accrual_id,commitment_id,invoice_id,share_id,period_key,amount,"
            "kind,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, share["commitment_id"], invoice_id, share["share_id"], period_key,
             signed, kind, share["owner_actor_id"], self._now()),
        )
        self._adjust_budget(conn, target_pb, occurred_delta=signed)
        relief = 0
        if relieve_reserve:
            if signed > 0:
                relief = min(part, share["reserved_amount"])
                if relief:
                    conn.execute("UPDATE exp_shares SET reserved_amount=reserved_amount-? WHERE share_id=?",
                                 (relief, share["share_id"]))
                    self._adjust_budget(conn, share["period_budget_id"], reserved_delta=-relief)
            else:
                # 退款/冲回：未发生的钱回到原承诺占用
                cap = share["amount"] - share["reserved_amount"]
                relief = min(part, max(cap, 0))
                if relief:
                    conn.execute("UPDATE exp_shares SET reserved_amount=reserved_amount+? WHERE share_id=?",
                                 (relief, share["share_id"]))
                    self._adjust_budget(conn, share["period_budget_id"], reserved_delta=relief)
        return relief

    def register_invoice(self, *, request_id: str, actor_id: str, commitment_id: str, invoice_no: str,
                         invoice_date: str, taxable_period: str, amount: int,
                         vendor: str | None = None, tax_amount: int = 0,
                         reverse_accrual_period: str | None = None) -> dict[str, Any]:
        """登记发票（含跨期发票）。金额按份额比例回到原承诺各产品份额。

        reverse_accrual_period：发票跨期到达时，原计提所在期间，用于红字冲回应计事实。
        """
        payload = {"actor_id": actor_id, "commitment_id": commitment_id, "invoice_no": invoice_no,
                   "taxable_period": taxable_period, "amount": amount,
                   "reverse_accrual_period": reverse_accrual_period}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin", "operator", "reviewer")
            commitment = self._row(conn, "SELECT * FROM exp_commitments WHERE commitment_id=?",
                                   (commitment_id,), "承诺不存在")
            if commitment["status"] not in COMMITMENT_OPEN:
                raise WorkflowError("承诺已关闭，不能登记发票")
            if not DATE_RE.fullmatch(invoice_date or ""):
                raise ValidationError("invoice_date 必须是 YYYY-MM-DD")
            taxable_period = self._period(taxable_period)
            if reverse_accrual_period:
                reverse_accrual_period = self._period(reverse_accrual_period)
            amount = self._amount(amount, "amount")
            tax_amount = self._amount(tax_amount, "tax_amount")
            if amount <= 0:
                raise ValidationError("发票金额必须大于零")
            billed = conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS s FROM exp_invoices WHERE commitment_id=? AND status='received'",
                (commitment_id,)).fetchone()["s"]
            refunded = conn.execute("SELECT COALESCE(SUM(amount),0) AS s FROM exp_refunds WHERE commitment_id=?",
                                    (commitment_id,)).fetchone()["s"]
            if billed - refunded + amount > commitment["total_amount"]:
                raise ConflictError("发票累计净额超过承诺总额")
            shares = self._active_shares(conn, commitment_id)
            allocations = self._allocate_to_shares(amount, shares)
            cross_period = taxable_period != commitment["period_key"]

            def create() -> tuple[str, str, dict[str, Any]]:
                invoice_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO exp_invoices(invoice_id,commitment_id,invoice_no,vendor,invoice_date,"
                    "taxable_period,amount,tax_amount,currency,status,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,'received',?,?)",
                    (invoice_id, commitment_id, invoice_no, vendor, invoice_date, taxable_period,
                     amount, tax_amount, commitment["currency"], actor_id, self._now()),
                )
                for share, part in allocations:
                    if reverse_accrual_period:
                        self._occur(conn, share, part=part, period_key=reverse_accrual_period,
                                    kind="reversal", invoice_id=invoice_id, relieve_reserve=False)
                    self._occur(conn, share, part=part, period_key=taxable_period,
                                kind="invoice", invoice_id=invoice_id,
                                relieve_reserve=not reverse_accrual_period)
                    fresh = conn.execute("SELECT * FROM exp_shares WHERE share_id=?",
                                         (share["share_id"],)).fetchone()
                    self._ledger(conn, share=fresh, event="occurred", amount=part,
                                 detail={"invoice_id": invoice_id, "invoice_no": invoice_no,
                                         "taxable_period": taxable_period, "cross_period": cross_period,
                                         "reversed_accrual_period": reverse_accrual_period},
                                 actor_id=actor_id)
                self._audit(conn, actor_id=actor_id, action="expense.invoice.registered",
                            resource_type="invoice", resource_id=invoice_id,
                            detail={"commitment_id": commitment_id, "invoice_no": invoice_no,
                                    "amount": amount, "taxable_period": taxable_period,
                                    "cross_period": cross_period,
                                    "reverse_accrual_period": reverse_accrual_period})
                return "invoice", invoice_id, {"invoice_id": invoice_id, "cross_period": cross_period,
                                               "allocation": [{"product_id": s["product_id"], "amount": a}
                                                              for s, a in allocations]}

            return self._idempotent(conn, request_id=request_id, action="register_invoice",
                                    payload=payload, create=create)

    def book_accrual(self, *, request_id: str, actor_id: str, commitment_id: str,
                     period_key: str, amount: int, reason: str) -> dict[str, Any]:
        """月底对尚未到票的承诺计提费用（跨期发票到达时可红字冲回）。"""
        payload = {"actor_id": actor_id, "commitment_id": commitment_id, "period_key": period_key,
                   "amount": amount, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin", "operator", "reviewer")
            commitment = self._row(conn, "SELECT * FROM exp_commitments WHERE commitment_id=?",
                                   (commitment_id,), "承诺不存在")
            if commitment["status"] not in COMMITMENT_OPEN:
                raise WorkflowError("承诺已关闭")
            period_key = self._period(period_key)
            amount = self._amount(amount, "amount")
            if amount <= 0:
                raise ValidationError("计提金额必须大于零")
            shares = self._active_shares(conn, commitment_id)
            allocations = self._allocate_to_shares(amount, shares)

            def create() -> tuple[str, str, dict[str, Any]]:
                batch = uuid.uuid4().hex
                for share, part in allocations:
                    self._occur(conn, share, part=part, period_key=period_key,
                                kind="accrual", invoice_id=None, relieve_reserve=True)
                    fresh = conn.execute("SELECT * FROM exp_shares WHERE share_id=?",
                                         (share["share_id"],)).fetchone()
                    self._ledger(conn, share=fresh, event="accrued", amount=part,
                                 detail={"period_key": period_key, "reason": reason, "batch": batch},
                                 actor_id=actor_id)
                self._audit(conn, actor_id=actor_id, action="expense.accrual.booked",
                            resource_type="accrual_batch", resource_id=batch,
                            detail={"commitment_id": commitment_id, "period_key": period_key,
                                    "amount": amount, "reason": reason})
                return "accrual_batch", batch, {"accrual_batch": batch,
                                                "allocation": [{"product_id": s["product_id"], "amount": a}
                                                               for s, a in allocations]}

            return self._idempotent(conn, request_id=request_id, action="book_accrual",
                                    payload=payload, create=create)

    def register_refund(self, *, request_id: str, actor_id: str, commitment_id: str,
                        invoice_id: str, amount: int, reason: str, period_key: str) -> dict[str, Any]:
        """退款按原发票归属比例回到原承诺各份额，冲减实际发生并恢复承诺占用。"""
        payload = {"actor_id": actor_id, "commitment_id": commitment_id, "invoice_id": invoice_id,
                   "amount": amount, "period_key": period_key}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin", "operator", "reviewer")
            self._row(conn, "SELECT 1 FROM exp_commitments WHERE commitment_id=?",
                      (commitment_id,), "承诺不存在")
            invoice = self._row(conn, "SELECT * FROM exp_invoices WHERE invoice_id=? AND commitment_id=?",
                                (invoice_id, commitment_id), "发票不存在或不属于该承诺")
            if invoice["status"] != "received":
                raise WorkflowError("发票已作废")
            period_key = self._period(period_key)
            amount = self._amount(amount, "amount")
            if amount <= 0:
                raise ValidationError("退款金额必须大于零")
            refunded = conn.execute("SELECT COALESCE(SUM(amount),0) AS s FROM exp_refunds WHERE invoice_id=?",
                                    (invoice_id,)).fetchone()["s"]
            if refunded + amount > invoice["amount"]:
                raise ConflictError("退款累计超过原发票金额")
            parts = conn.execute(
                "SELECT share_id, SUM(amount) AS part FROM exp_accruals WHERE invoice_id=? AND kind='invoice' "
                "GROUP BY share_id", (invoice_id,)).fetchall()
            weights = []
            share_map = {}
            for r in parts:
                share = self._row(conn, "SELECT * FROM exp_shares WHERE share_id=?",
                                  (r["share_id"],), "份额不存在")
                share_map[share["product_id"]] = share
                weights.append((share["product_id"], share["share_id"], max(r["part"], 0)))
            rows = allocation._largest_remainder(amount, weights)
            by_pid = {pid: a for pid, _sid, _w, a, _rem in rows}

            def create() -> tuple[str, str, dict[str, Any]]:
                refund_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO exp_refunds(refund_id,commitment_id,invoice_id,amount,reason,period_key,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (refund_id, commitment_id, invoice_id, amount, self._text(reason, "reason"),
                     period_key, actor_id, self._now()),
                )
                for pid, part in by_pid.items():
                    if part <= 0:
                        continue
                    share = share_map[pid]
                    self._occur(conn, share, part=part, period_key=period_key,
                                kind="refund", invoice_id=invoice_id, relieve_reserve=True)
                    fresh = conn.execute("SELECT * FROM exp_shares WHERE share_id=?",
                                         (share["share_id"],)).fetchone()
                    self._ledger(conn, share=fresh, event="refunded", amount=part,
                                 detail={"invoice_id": invoice_id, "refund_id": refund_id,
                                         "period_key": period_key, "reason": reason}, actor_id=actor_id)
                self._audit(conn, actor_id=actor_id, action="expense.refund.registered",
                            resource_type="refund", resource_id=refund_id,
                            detail={"commitment_id": commitment_id, "invoice_id": invoice_id,
                                    "amount": amount})
                return "refund", refund_id, {"refund_id": refund_id,
                                             "allocation": [{"product_id": pid, "amount": a}
                                                            for pid, a in by_pid.items()]}

            return self._idempotent(conn, request_id=request_id, action="register_refund",
                                    payload=payload, create=create)

    # ---------- 取消与释放 ----------

    def cancel_commitment(self, *, request_id: str, actor_id: str, commitment_id: str,
                          reason: str) -> dict[str, Any]:
        """取消承诺：未发生的占用全部释放；已发生的发票/应计事实保留。"""
        payload = {"actor_id": actor_id, "commitment_id": commitment_id, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin", "operator")
            commitment = self._row(conn, "SELECT * FROM exp_commitments WHERE commitment_id=?",
                                   (commitment_id,), "承诺不存在")
            if commitment["status"] not in COMMITMENT_OPEN:
                raise WorkflowError("承诺已关闭，不能重复取消")
            reason = self._text(reason, "reason")

            def create() -> tuple[str, str, dict[str, Any]]:
                released_total = 0
                rows = conn.execute(
                    "SELECT * FROM exp_shares WHERE commitment_id=? AND status NOT IN ('rejected','released')",
                    (commitment_id,)).fetchall()
                for share in rows:
                    remaining = share["reserved_amount"]
                    if remaining > 0 and share["period_budget_id"]:
                        self._adjust_budget(conn, share["period_budget_id"], reserved_delta=-remaining)
                    new_released = share["released_amount"] + remaining
                    conn.execute(
                        "UPDATE exp_shares SET status='released',reserved_amount=0,"
                        "released_amount=?,updated_at=? WHERE share_id=?",
                        (new_released, self._now(), share["share_id"]))
                    self._ledger(conn, share=share, event="released", amount=remaining,
                                 detail={"reason": reason}, actor_id=actor_id)
                    released_total += remaining
                conn.execute(
                    "UPDATE exp_commitments SET status='cancelled',cancelled_by=?,cancelled_at=?,"
                    "cancel_reason=? WHERE commitment_id=?",
                    (actor_id, self._now(), reason, commitment_id))
                conn.execute("UPDATE exp_campaigns SET status='cancelled',updated_at=? WHERE campaign_id="
                             "(SELECT campaign_id FROM exp_commitments WHERE commitment_id=?)",
                             (self._now(), commitment_id))
                self._audit(conn, actor_id=actor_id, action="expense.commitment.cancelled",
                            resource_type="commitment", resource_id=commitment_id,
                            detail={"reason": reason, "released_total": released_total})
                return "commitment", commitment_id, {"commitment_id": commitment_id,
                                                      "status": "cancelled",
                                                      "released_total": released_total}

            return self._idempotent(conn, request_id=request_id, action="cancel_commitment",
                                    payload=payload, create=create)

    # ---------- 争议冻结 ----------

    def raise_dispute(self, *, request_id: str, actor_id: str, commitment_id: str,
                      reason: str, product_id: str | None = None) -> dict[str, Any]:
        """冻结争议涉及的产品份额；其他份额不受影响、继续结账。"""
        payload = {"actor_id": actor_id, "commitment_id": commitment_id, "reason": reason,
                   "product_id": product_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            commitment = self._row(conn, "SELECT * FROM exp_commitments WHERE commitment_id=?",
                                   (commitment_id,), "承诺不存在")
            if commitment["status"] not in COMMITMENT_OPEN:
                raise WorkflowError("承诺已关闭")
            reason = self._text(reason, "reason")
            sql = ("SELECT * FROM exp_shares WHERE commitment_id=? AND status IN ('confirmed','settled')")
            params: tuple[Any, ...] = (commitment_id,)
            if product_id:
                sql += " AND product_id=?"
                params = (commitment_id, product_id)
            shares = conn.execute(sql, params).fetchall()
            if not shares:
                raise NotFoundError("没有可冻结的已确认份额")
            disputed_amount = sum(s["amount"] - s["settled_amount"] for s in shares)

            def create() -> tuple[str, str, dict[str, Any]]:
                dispute_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO exp_disputes(dispute_id,commitment_id,product_id,disputed_amount,reason,"
                    "status,raised_by,created_at) VALUES(?,?,?,?,?, 'open',?,?)",
                    (dispute_id, commitment_id, product_id, disputed_amount, reason,
                     actor_id, self._now()),
                )
                for s in shares:
                    conn.execute("UPDATE exp_shares SET status='frozen',dispute_id=?,frozen_at=?,"
                                 "frozen_reason=?,updated_at=? WHERE share_id=?",
                                 (dispute_id, self._now(), reason, self._now(), s["share_id"]))
                    self._ledger(conn, share=s, event="frozen", amount=s["amount"] - s["settled_amount"],
                                 detail={"dispute_id": dispute_id, "reason": reason}, actor_id=actor_id)
                self._audit(conn, actor_id=actor_id, action="expense.dispute.raised",
                            resource_type="dispute", resource_id=dispute_id,
                            detail={"commitment_id": commitment_id, "product_id": product_id,
                                    "frozen_shares": [s["share_id"] for s in shares]})
                return "dispute", dispute_id, {"dispute_id": dispute_id, "frozen":
                                               [{"share_id": s["share_id"], "product_id": s["product_id"]}
                                                for s in shares]}

            return self._idempotent(conn, request_id=request_id, action="raise_dispute",
                                    payload=payload, create=create)

    def resolve_dispute(self, *, request_id: str, actor_id: str, dispute_id: str,
                        resolution: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "dispute_id": dispute_id, "resolution": resolution}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin", "reviewer")
            dispute = self._row(conn, "SELECT * FROM exp_disputes WHERE dispute_id=?",
                                (dispute_id,), "争议不存在")
            if dispute["status"] != "open":
                raise WorkflowError("争议已解决")
            resolution = self._text(resolution, "resolution")

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute("UPDATE exp_disputes SET status='resolved',resolution=?,resolved_by=?,"
                             "resolved_at=? WHERE dispute_id=?",
                             (resolution, actor_id, self._now(), dispute_id))
                unfrozen: list[str] = []
                shares = conn.execute(
                    "SELECT * FROM exp_shares WHERE commitment_id=? AND status='frozen' AND dispute_id=?",
                    (dispute["commitment_id"], dispute_id)).fetchall()
                for s in shares:
                    occurred = conn.execute("SELECT COALESCE(SUM(amount),0) AS v FROM exp_accruals "
                                            "WHERE share_id=?", (s["share_id"],)).fetchone()["v"]
                    target = "settled" if 0 < occurred <= s["settled_amount"] else "confirmed"
                    conn.execute("UPDATE exp_shares SET status=?,dispute_id=NULL,frozen_at=NULL,"
                                 "frozen_reason=NULL,updated_at=? WHERE share_id=?",
                                 (target, self._now(), s["share_id"]))
                    self._ledger(conn, share=s, event="unfrozen", amount=0,
                                 detail={"dispute_id": dispute_id, "resolution": resolution},
                                 actor_id=actor_id)
                    unfrozen.append(s["share_id"])
                self._audit(conn, actor_id=actor_id, action="expense.dispute.resolved",
                            resource_type="dispute", resource_id=dispute_id,
                            detail={"resolution": resolution, "unfrozen": unfrozen})
                return "dispute", dispute_id, {"dispute_id": dispute_id, "status": "resolved",
                                               "unfrozen": unfrozen}

            return self._idempotent(conn, request_id=request_id, action="resolve_dispute",
                                    payload=payload, create=create)

    # ---------- 结账 ----------

    def _occurred_net(self, conn, share_id: str) -> int:
        return conn.execute("SELECT COALESCE(SUM(amount),0) AS v FROM exp_accruals WHERE share_id=?",
                            (share_id,)).fetchone()["v"]

    def _settle(self, conn, share, target: int, actor_id: str, batch: bool) -> None:
        conn.execute(
            "INSERT INTO exp_settlements(settlement_id,share_id,period_key,amount,created_by,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (uuid.uuid4().hex, share["share_id"], share["period_key"], target, actor_id, self._now()))
        occurred = self._occurred_net(conn, share["share_id"])
        new_settled = share["settled_amount"] + target
        new_status = "settled" if new_settled >= occurred > 0 else share["status"]
        conn.execute("UPDATE exp_shares SET settled_amount=?,status=?,updated_at=? WHERE share_id=?",
                     (new_settled, new_status, self._now(), share["share_id"]))
        fresh = conn.execute("SELECT * FROM exp_shares WHERE share_id=?", (share["share_id"],)).fetchone()
        self._ledger(conn, share=fresh, event="settled", amount=target,
                     detail={"period_key": share["period_key"], "batch": batch}, actor_id=actor_id)

    def settle_share(self, *, request_id: str, actor_id: str, share_id: str,
                     amount: int | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "share_id": share_id, "amount": amount}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin", "operator", "reviewer")
            share = self._row(conn, "SELECT * FROM exp_shares WHERE share_id=?", (share_id,), "份额不存在")
            if share["status"] == "frozen":
                raise WorkflowError("争议份额已冻结，不能结账")
            if share["status"] in ("draft", "rejected", "released"):
                raise WorkflowError("份额尚未确认，不能结账")
            settleable = self._occurred_net(conn, share_id) - share["settled_amount"]
            if settleable <= 0:
                raise ConflictError("该份额没有待结账的已发生金额")
            target = settleable if amount is None else self._amount(amount, "amount")
            if target <= 0 or target > settleable:
                raise ValidationError(f"结账金额必须在 1 到 {settleable} 之间")

            def create() -> tuple[str, str, dict[str, Any]]:
                self._settle(conn, share, target, actor_id, batch=False)
                self._audit(conn, actor_id=actor_id, action="expense.share.settled",
                            resource_type="share", resource_id=share_id,
                            detail={"amount": target})
                fresh = conn.execute("SELECT settled_amount,status FROM exp_shares WHERE share_id=?",
                                     (share_id,)).fetchone()
                return "settlement", share_id, {"share_id": share_id,
                                                "settled_amount": fresh["settled_amount"],
                                                "status": fresh["status"]}

            return self._idempotent(conn, request_id=request_id, action="settle_share",
                                    payload=payload, create=create)

    def settle_period(self, *, request_id: str, actor_id: str, period_key: str) -> dict[str, Any]:
        """期间结账：按该期间的应计/发票事实净额自动结账，争议冻结份额只跳过、不阻塞其他份额。

        跨期费用按事实所在期间结算（份额可被多个期间分别结账）；
        每份额可结金额 = 该期间应计净额 - 该份额该期间已结账金额。
        """
        payload = {"actor_id": actor_id, "period_key": period_key}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin", "reviewer")
            period_key = self._period(period_key)

            def create() -> tuple[str, str, dict[str, Any]]:
                settled: list[dict[str, Any]] = []
                skipped_frozen: list[dict[str, Any]] = []
                total_amount = 0
                rows = conn.execute(
                    "SELECT s.*, "
                    "COALESCE((SELECT SUM(amount) FROM exp_accruals a WHERE a.share_id=s.share_id "
                    "AND a.period_key=?),0) AS period_occurred, "
                    "COALESCE((SELECT SUM(amount) FROM exp_settlements t WHERE t.share_id=s.share_id "
                    "AND t.period_key=?),0) AS period_settled "
                    "FROM exp_shares s WHERE s.status NOT IN ('draft','rejected','released')",
                    (period_key, period_key)).fetchall()
                for s in rows:
                    settleable = s["period_occurred"] - s["period_settled"]
                    if settleable <= 0:
                        continue
                    if s["status"] == "frozen":
                        skipped_frozen.append({"share_id": s["share_id"], "product_id": s["product_id"],
                                               "pending_amount": settleable})
                        continue
                    conn.execute(
                        "INSERT INTO exp_settlements(settlement_id,share_id,period_key,amount,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?)",
                        (uuid.uuid4().hex, s["share_id"], period_key, settleable, actor_id, self._now()))
                    new_settled = s["settled_amount"] + settleable
                    total_occurred = self._occurred_net(conn, s["share_id"])
                    new_status = "settled" if new_settled >= total_occurred > 0 else s["status"]
                    conn.execute("UPDATE exp_shares SET settled_amount=?,status=?,updated_at=? WHERE share_id=?",
                                 (new_settled, new_status, self._now(), s["share_id"]))
                    fresh = conn.execute("SELECT * FROM exp_shares WHERE share_id=?",
                                         (s["share_id"],)).fetchone()
                    self._ledger(conn, share=fresh, event="settled", amount=settleable,
                                 detail={"period_key": period_key, "batch": True}, actor_id=actor_id)
                    settled.append({"share_id": s["share_id"], "product_id": s["product_id"],
                                    "amount": settleable})
                    total_amount += settleable
                self._audit(conn, actor_id=actor_id, action="expense.period.settled",
                            resource_type="period_settlement", resource_id=period_key,
                            detail={"settled_count": len(settled),
                                    "skipped_frozen": len(skipped_frozen), "total_amount": total_amount})
                return "period_settlement", period_key, {"period_key": period_key, "settled": settled,
                                                          "skipped_frozen": skipped_frozen,
                                                          "total_amount": total_amount}

            return self._idempotent(conn, request_id=request_id, action="settle_period",
                                    payload=payload, create=create)

    # ---------- 超预算例外与授权链 ----------

    def request_exception(self, *, request_id: str, actor_id: str, commitment_id: str,
                          over_amount: int, limit_amount: int, valid_until: str,
                          reason: str, required_levels: int = 2) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "commitment_id": commitment_id, "over_amount": over_amount,
                   "limit_amount": limit_amount, "valid_until": valid_until,
                   "required_levels": required_levels}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin", "operator")
            self._row(conn, "SELECT 1 FROM exp_commitments WHERE commitment_id=?",
                      (commitment_id,), "承诺不存在")
            over_amount = self._amount(over_amount, "over_amount")
            limit_amount = self._amount(limit_amount, "limit_amount")
            if over_amount <= 0 or limit_amount < over_amount:
                raise ValidationError("超额必须为正且限额不得小于超额")
            if not DATE_RE.fullmatch(valid_until or ""):
                raise ValidationError("valid_until 必须是 YYYY-MM-DD")
            if valid_until < self._today():
                raise ValidationError("例外到期日不能早于今天")
            if not 1 <= required_levels <= 3:
                raise ValidationError("授权层级必须在 1 到 3 之间")
            reason = self._text(reason, "reason")

            def create() -> tuple[str, str, dict[str, Any]]:
                exception_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO exp_exceptions(exception_id,commitment_id,over_amount,limit_amount,reason,"
                    "valid_until,required_levels,status,requested_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?, 'requested',?,?)",
                    (exception_id, commitment_id, over_amount, limit_amount, reason, valid_until,
                     required_levels, actor_id, self._now()),
                )
                self._audit(conn, actor_id=actor_id, action="expense.exception.requested",
                            resource_type="exception", resource_id=exception_id,
                            detail={"commitment_id": commitment_id, "over_amount": over_amount,
                                    "limit_amount": limit_amount, "valid_until": valid_until,
                                    "required_levels": required_levels})
                return "exception", exception_id, {"exception_id": exception_id, "status": "requested",
                                                   "required_levels": required_levels}

            return self._idempotent(conn, request_id=request_id, action="request_exception",
                                    payload=payload, create=create)

    def decide_exception(self, *, request_id: str, actor_id: str, exception_id: str,
                         level: int, decision: str, comment: str | None = None) -> dict[str, Any]:
        """逐级授权：必须按级别顺序、每级不同人；第 3 级必须 admin。"""
        payload = {"actor_id": actor_id, "exception_id": exception_id, "level": level, "decision": decision}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            exc = self._row(conn, "SELECT * FROM exp_exceptions WHERE exception_id=?",
                            (exception_id,), "例外不存在")
            if exc["status"] not in ("requested", "approved"):
                raise WorkflowError(f"例外当前状态 {exc['status']}，不能审批")
            if not isinstance(level, int) or not 1 <= level <= 3:
                raise ValidationError("level 必须在 1 到 3 之间")
            if level > exc["required_levels"]:
                raise ValidationError("该例外不需要这一级审批")
            if decision not in ("approved", "rejected"):
                raise ValidationError("decision 必须是 approved/rejected")
            if level == 1:
                self._require_role(actor, "reviewer", "admin")
            elif level == 3:
                self._require_role(actor, "admin")
            else:
                self._require_role(actor, "reviewer", "admin")
            prior = conn.execute(
                "SELECT * FROM exp_exception_approvals WHERE exception_id=? ORDER BY level",
                (exception_id,)).fetchall()
            prior_levels = {p["level"]: p for p in prior}
            if level in prior_levels:
                raise ConflictError("该级别已经审批")
            expected = next((m for m in range(1, exc["required_levels"] + 1) if m not in prior_levels), None)
            if level != expected:
                raise AuthorizationChainError(f"必须先完成第 {expected} 级审批")
            if actor_id == exc["requested_by"] or any(p["approver_actor_id"] == actor_id for p in prior):
                raise AuthorizationChainError("申请人与各级审批人必须相互独立")

            def create() -> tuple[str, str, dict[str, Any]]:
                approval_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO exp_exception_approvals(approval_id,exception_id,level,approver_actor_id,"
                    "decision,comment,created_at) VALUES(?,?,?,?,?,?,?)",
                    (approval_id, exception_id, level, actor_id, decision, comment, self._now()),
                )
                if decision == "rejected":
                    new_status = "rejected"
                    conn.execute("UPDATE exp_exceptions SET status='rejected' WHERE exception_id=?",
                                 (exception_id,))
                elif len(prior_levels) + 1 >= exc["required_levels"]:
                    new_status = "approved"
                    conn.execute("UPDATE exp_exceptions SET status='approved' WHERE exception_id=?",
                                 (exception_id,))
                else:
                    new_status = exc["status"]
                self._audit(conn, actor_id=actor_id, action="expense.exception.decided",
                            resource_type="exception_approval", resource_id=approval_id,
                            detail={"exception_id": exception_id, "level": level, "decision": decision,
                                    "new_status": new_status})
                return "exception_approval", approval_id, {"approval_id": approval_id, "level": level,
                                                           "decision": decision, "status": new_status}

            return self._idempotent(conn, request_id=request_id, action="decide_exception",
                                    payload=payload, create=create)

    # ---------- 全过程查询 ----------

    def expense_trace(self, commitment_id: str) -> dict[str, Any]:
        """展示一笔费用从申请、占用、发生到分摊和释放的全过程。"""
        conn = self.database.connection
        commitment = self._row(conn, "SELECT * FROM exp_commitments WHERE commitment_id=?",
                               (commitment_id,), "承诺不存在")
        campaign = conn.execute("SELECT * FROM exp_campaigns WHERE campaign_id=?",
                                (commitment["campaign_id"],)).fetchone()
        shares = []
        for s in conn.execute("SELECT * FROM exp_shares WHERE commitment_id=? ORDER BY product_id",
                              (commitment_id,)):
            accruals = [{"accrual_id": a["accrual_id"], "invoice_id": a["invoice_id"],
                         "period_key": a["period_key"], "amount": a["amount"], "kind": a["kind"],
                         "created_at": a["created_at"]}
                        for a in conn.execute(
                            "SELECT * FROM exp_accruals WHERE share_id=? ORDER BY created_at,accrual_id",
                            (s["share_id"],))]
            ledger = [{"event": e["event"], "amount": e["amount"],
                       "reserved_after": e["reserved_after"], "occurred_after": e["occurred_after"],
                       "settled_after": e["settled_after"], "actor_id": e["actor_id"],
                       "occurred_at": e["occurred_at"], "detail": json.loads(e["detail_json"])}
                      for e in conn.execute("SELECT * FROM exp_share_ledger WHERE share_id=? ORDER BY entry_id",
                                            (s["share_id"],))]
            shares.append({
                "share_id": s["share_id"], "product_id": s["product_id"],
                "owner_actor_id": s["owner_actor_id"], "amount": s["amount"],
                "reserved_amount": s["reserved_amount"], "status": s["status"],
                "rule_name": s["rule_name"], "stage_snapshot": s["stage_snapshot"],
                "version_id": s["version_id"], "period_key": s["period_key"],
                "settled_amount": s["settled_amount"], "released_amount": s["released_amount"],
                "explanation": json.loads(s["explanation_json"]),
                "accruals": accruals, "ledger": ledger,
            })
        invoices = [dict(i) for i in conn.execute(
            "SELECT invoice_id,invoice_no,vendor,invoice_date,taxable_period,amount,tax_amount,status "
            "FROM exp_invoices WHERE commitment_id=? ORDER BY invoice_date,invoice_id", (commitment_id,))]
        refunds = [dict(r) for r in conn.execute(
            "SELECT refund_id,invoice_id,amount,reason,period_key,created_at FROM exp_refunds "
            "WHERE commitment_id=? ORDER BY created_at", (commitment_id,))]
        disputes = [dict(d) for d in conn.execute(
            "SELECT dispute_id,product_id,disputed_amount,reason,status,resolution,created_at,resolved_at "
            "FROM exp_disputes WHERE commitment_id=? ORDER BY created_at", (commitment_id,))]
        runs = [{"run_id": r["run_id"], "kind": r["kind"], "rule_name": r["rule_name"],
                 "rounding_gap": r["rounding_gap"], "summary": json.loads(r["summary_json"]),
                 "created_at": r["created_at"]}
                for r in conn.execute("SELECT * FROM exp_allocation_runs WHERE commitment_id=? "
                                      "ORDER BY created_at", (commitment_id,))]
        exceptions = [dict(e) for e in conn.execute(
            "SELECT exception_id,over_amount,limit_amount,valid_until,required_levels,status,reason "
            "FROM exp_exceptions WHERE commitment_id=? ORDER BY created_at", (commitment_id,))]
        return {"commitment": dict(commitment), "campaign": dict(campaign) if campaign else None,
                "allocation_runs": runs, "shares": shares, "invoices": invoices, "refunds": refunds,
                "disputes": disputes, "exceptions": exceptions}
