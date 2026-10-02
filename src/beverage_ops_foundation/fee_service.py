"""销售费用承诺与分配领域服务。

管理产品生命周期生效版本、年度与期间额度、活动受益范围、合同承诺、
发票/应计事实，以及共享费用的可解释分摊、逐份额确认、争议冻结、
超预算例外授权和预算台账。所有写操作均在 SQLite 短事务内完成并追加
哈希审计事件。金额一律使用整数分。
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any, Callable

from .allocation import ALLOCATION_RULES, allocate, largest_remainder
from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import (BudgetExceeded, ConflictError, NotFoundError,
                     PermissionDenied, StateError, ValidationError)
from .models import WriteReceipt
from .storage import Database
from .service import IDENTIFIER

STAGES = frozenset({"mature", "new", "nurturing"})
STAGE_LABELS = {"mature": "成熟", "new": "新品", "nurturing": "培育期"}
TEAM_TYPES = frozenset({"brand", "region", "channel"})

# 超预算例外按金额分档，授权链自低到高逐级审批：金额越大链越长（单位：分）。
# 5 万元（含）以下经理一级；20 万元（含）以下经理→总监；以上经理→总监→CFO。
APPROVAL_TIERS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (5_000_000, ("manager",)),
    (20_000_000, ("manager", "director")),
    (10**12, ("manager", "director", "cfo")),
)
OWNER_ROLES = {"brand": "brand_owner", "region": "region_owner", "channel": "channel_owner"}


class FeeService:
    """协调费用承诺、额度占用、分摊草案、确认结账与例外授权。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _today(self) -> date:
        return self.clock.now().date()

    def _id(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _cents(self, value: Any, field: str) -> int:
        if isinstance(value, bool):
            raise ValidationError(f"{field} 必须是正整数分")
        try:
            amount = int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field} 必须是整数分") from exc
        if amount <= 0:
            raise ValidationError(f"{field} 必须大于零")
        return amount

    def _non_negative_cents(self, value: Any, field: str) -> int:
        if isinstance(value, bool):
            raise ValidationError(f"{field} 必须是非负整数分")
        try:
            amount = int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field} 必须是整数分") from exc
        if amount < 0:
            raise ValidationError(f"{field} 不能为负")
        return amount

    def _day(self, value: str, field: str) -> date:
        try:
            return date.fromisoformat(str(value).strip())
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期") from exc

    def _period(self, value: str) -> tuple[int, int, str]:
        text = str(value).strip()
        try:
            year_text, period_text = text.split("-")
            year, period = int(year_text), int(period_text)
        except (ValueError, AttributeError) as exc:
            raise ValidationError("期间必须是 YYYY-MM 格式") from exc
        if not 1 <= period <= 12:
            raise ValidationError("月份必须在 1 到 12 之间")
        return year, period, f"{year:04d}-{period:02d}"

    def _actor(self, connection, actor_id: str):
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require_any(self, actor, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], tuple[str, str, dict[str, Any]]]
                    ) -> WriteReceipt:
        request_id = self._id(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    # ------------------------------------------------------ 产品与生命周期

    def register_product(self, *, request_id: str, actor_id: str, product_id: str,
                         name: str, owner_actor_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "product_id": product_id, "name": name,
                   "owner_actor_id": owner_actor_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_any(actor, "admin", "finance", "brand_owner",
                              "region_owner", "channel_owner", "operator")
            product_id = self._id(product_id, "product_id")
            name = self._text(name, "name")
            owner = self._actor(connection, owner_actor_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO products(product_id,name,owner_actor_id,created_by,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (product_id, name, owner_actor_id, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("产品编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="product.registered",
                             resource_type="product", resource_id=product_id,
                             detail={"name": name, "owner_actor_id": owner_actor_id},
                             occurred_at=self._now())
                return "product", product_id, {"product_id": product_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_product", payload=payload, create=create)

    def _product(self, connection, product_id: str):
        row = connection.execute("SELECT * FROM products WHERE product_id=?", (product_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"产品 {product_id} 不存在")
        return row

    def set_lifecycle(self, *, request_id: str, actor_id: str, product_id: str,
                      stage: str, effective_from: str, note: str = "") -> WriteReceipt:
        """登记一个新生效版本；历史版本永不修改，切换不溯及既往。"""

        payload = {"actor_id": actor_id, "product_id": product_id, "stage": stage,
                   "effective_from": effective_from, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_any(actor, "admin", "finance", "operator")
            product_id = self._id(product_id, "product_id")
            self._product(connection, product_id)
            if stage not in STAGES:
                raise ValidationError("stage 必须是 mature、new 或 nurturing")
            effective_day = self._day(effective_from, "effective_from")
            note = str(note or "").strip()[:500]

            def create() -> tuple[str, str, dict[str, Any]]:
                version_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO lifecycle_versions(version_id,product_id,stage,effective_from,"
                        "note,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (version_id, product_id, stage, effective_day.isoformat(),
                         note, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该产品在同一生效日已有生命周期版本") from exc
                append_event(connection, actor_id=actor_id, action="lifecycle.versioned",
                             resource_type="lifecycle_version", resource_id=version_id,
                             detail={"product_id": product_id, "stage": stage,
                                     "stage_label": STAGE_LABELS[stage],
                                     "effective_from": effective_day.isoformat(), "note": note},
                             occurred_at=self._now())
                return "lifecycle_version", version_id, {"version_id": version_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="set_lifecycle", payload=payload, create=create)

    def _lifecycle_at(self, connection, product_id: str, on_day: date):
        """返回 on_day 当天生效的生命周期版本（effective_from <= on_day 中最新者）。"""

        row = connection.execute(
            "SELECT * FROM lifecycle_versions WHERE product_id=? AND effective_from<=? "
            "ORDER BY effective_from DESC, created_at DESC LIMIT 1",
            (product_id, on_day.isoformat()),
        ).fetchone()
        if row is None:
            raise ValidationError(f"产品 {product_id} 在 {on_day.isoformat()} 尚无生效的生命周期版本")
        return row

    def list_lifecycle_versions(self, product_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        self._product(connection, product_id)
        rows = connection.execute(
            "SELECT * FROM lifecycle_versions WHERE product_id=? ORDER BY effective_from, created_at",
            (product_id,),
        ).fetchall()
        return [{"version_id": r["version_id"], "product_id": r["product_id"], "stage": r["stage"],
                 "stage_label": STAGE_LABELS[r["stage"]], "effective_from": r["effective_from"],
                 "note": r["note"], "created_by": r["created_by"], "created_at": r["created_at"]}
                for r in rows]

    # -------------------------------------------------------------- 额度版本

    def set_annual_budget(self, *, request_id: str, actor_id: str, product_id: str, year: int,
                          amount_cents: int, effective_from: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "product_id": product_id, "year": year,
                   "amount_cents": amount_cents, "effective_from": effective_from}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_any(actor, "admin", "finance")
            product_id = self._id(product_id, "product_id")
            self._product(connection, product_id)
            year = int(year)
            if not 2000 <= year <= 2100:
                raise ValidationError("年份超出范围")
            amount_cents = self._non_negative_cents(amount_cents, "amount_cents")
            effective_day = self._day(effective_from, "effective_from")
            lifecycle = self._lifecycle_at(connection, product_id, effective_day)

            def create() -> tuple[str, str, dict[str, Any]]:
                budget_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO annual_budgets(annual_budget_id,product_id,year,amount_cents,"
                        "stage_snapshot,lifecycle_version_id,effective_from,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (budget_id, product_id, year, amount_cents, lifecycle["stage"],
                         lifecycle["version_id"], effective_day.isoformat(), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("同一生效日的年度额度版本已存在") from exc
                append_event(connection, actor_id=actor_id, action="annual_budget.versioned",
                             resource_type="annual_budget", resource_id=budget_id,
                             detail={"product_id": product_id, "year": year,
                                     "amount_cents": amount_cents,
                                     "stage": lifecycle["stage"],
                                     "effective_from": effective_day.isoformat()},
                             occurred_at=self._now())
                return "annual_budget", budget_id, {"annual_budget_id": budget_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="set_annual_budget", payload=payload, create=create)

    def set_period_budget(self, *, request_id: str, actor_id: str, product_id: str,
                          period: str, amount_cents: int, effective_from: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "product_id": product_id, "period": period,
                   "amount_cents": amount_cents, "effective_from": effective_from}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_any(actor, "admin", "finance")
            product_id = self._id(product_id, "product_id")
            self._product(connection, product_id)
            year, month, _ = self._period(period)
            amount_cents = self._non_negative_cents(amount_cents, "amount_cents")
            effective_day = self._day(effective_from, "effective_from")
            lifecycle = self._lifecycle_at(connection, product_id, effective_day)
            annual = connection.execute(
                "SELECT * FROM annual_budgets WHERE product_id=? AND year=? AND effective_from<=? "
                "ORDER BY effective_from DESC, created_at DESC LIMIT 1",
                (product_id, year, effective_day.isoformat()),
            ).fetchone()
            if annual is None:
                raise ValidationError(f"产品 {product_id} {year} 年度额度尚未设置生效版本")
            period_sum = connection.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS total FROM period_budgets "
                "WHERE product_id=? AND year=? AND effective_from=?",
                (product_id, year, effective_day.isoformat()),
            ).fetchone()["total"]
            if period_sum + amount_cents > annual["amount_cents"]:
                raise ValidationError("同一额度版本下各期间额度合计不能超过年度额度")

            def create() -> tuple[str, str, dict[str, Any]]:
                budget_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO period_budgets(period_budget_id,product_id,year,period,"
                        "amount_cents,stage_snapshot,lifecycle_version_id,effective_from,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (budget_id, product_id, year, month, amount_cents, lifecycle["stage"],
                         lifecycle["version_id"], effective_day.isoformat(), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("同一生效日的期间额度版本已存在") from exc
                append_event(connection, actor_id=actor_id, action="period_budget.versioned",
                             resource_type="period_budget", resource_id=budget_id,
                             detail={"product_id": product_id, "year": year, "period": month,
                                     "amount_cents": amount_cents,
                                     "stage": lifecycle["stage"],
                                     "effective_from": effective_day.isoformat()},
                             occurred_at=self._now())
                return "period_budget", budget_id, {"period_budget_id": budget_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="set_period_budget", payload=payload, create=create)

    def _period_budget_at(self, connection, product_id: str, year: int, period: int, on_day: date):
        row = connection.execute(
            "SELECT * FROM period_budgets WHERE product_id=? AND year=? AND period=? "
            "AND effective_from<=? ORDER BY effective_from DESC, created_at DESC LIMIT 1",
            (product_id, year, period, on_day.isoformat()),
        ).fetchone()
        if row is None:
            raise ValidationError(
                f"产品 {product_id} {year}-{period:02d} 在 {on_day.isoformat()} 无生效的期间额度")
        return row

    def budget_status(self, product_id: str, period: str) -> dict[str, Any]:
        connection = self.database.connection
        product = self._product(connection, product_id)
        year, month, period_text = self._period(period)
        today = self._today()
        budget = self._period_budget_at(connection, product_id, year, month, today)
        # 在自动提交连接上刷新已过有效期的 granted 例外，避免其状态随写事务回滚而丢失。
        self._active_exceptions(connection, product_id, year, month)
        reserved = connection.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS v FROM budget_ledger "
            "WHERE product_id=? AND year=? AND period=? AND direction='reserve'",
            (product_id, year, month),
        ).fetchone()["v"]
        released = connection.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS v FROM budget_ledger "
            "WHERE product_id=? AND year=? AND period=? AND direction IN ('release','refund')",
            (product_id, year, month),
        ).fetchone()["v"]
        consumed = reserved - released
        exceptions = []
        for row in connection.execute(
                "SELECT * FROM budget_exceptions WHERE product_id=? AND year=? AND period=? "
                "AND status='granted' ORDER BY valid_until, exception_id",
                (product_id, year, month)):
            used = connection.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS v FROM exception_usage WHERE exception_id=?",
                (row["exception_id"],),
            ).fetchone()["v"]
            exceptions.append({"exception_id": row["exception_id"], "cap_cents": row["cap_cents"],
                               "used_cents": used, "available_cents": max(0, row["cap_cents"] - used),
                               "valid_from": row["valid_from"], "valid_until": row["valid_until"]})
        exception_available = sum(item["available_cents"] for item in exceptions
                                  if item["valid_from"] <= today.isoformat() <= item["valid_until"])
        lifecycle = self._lifecycle_at(connection, product_id, today)
        return {"product_id": product_id, "product_name": product["name"], "period": period_text,
                "stage": lifecycle["stage"], "stage_label": STAGE_LABELS[lifecycle["stage"]],
                "lifecycle_version_id": lifecycle["version_id"],
                "period_budget_id": budget["period_budget_id"],
                "budget_cents": budget["amount_cents"], "reserved_cents": reserved,
                "released_cents": released, "consumed_cents": consumed,
                "available_cents": max(0, budget["amount_cents"] - consumed),
                "exceptions": exceptions,
                "exception_available_cents": exception_available,
                "total_available_cents": max(0, budget["amount_cents"] - consumed) + exception_available}

    # ------------------------------------------------------------ 预算台账

    def _ledger_totals(self, connection, product_id: str, year: int, period: int) -> tuple[int, int]:
        row = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN direction='reserve' THEN amount_cents ELSE 0 END),0) AS r, "
            "COALESCE(SUM(CASE WHEN direction IN ('release','refund') THEN amount_cents ELSE 0 END),0) AS l "
            "FROM budget_ledger WHERE product_id=? AND year=? AND period=?",
            (product_id, year, period),
        ).fetchone()
        return row["r"], row["l"]

    def _active_exceptions(self, connection, product_id: str, year: int, period: int):
        """返回当前有效的授权例外，并把过期的 granted 标记为 expired。"""

        today_text = self._today().isoformat()
        active = []
        rows = connection.execute(
            "SELECT * FROM budget_exceptions WHERE product_id=? AND year=? AND period=? "
            "AND status IN ('granted','requested')",
            (product_id, year, period),
        ).fetchall()
        for row in rows:
            if row["status"] == "granted" and row["valid_until"] < today_text:
                connection.execute(
                    "UPDATE budget_exceptions SET status='expired' WHERE exception_id=?",
                    (row["exception_id"],),
                )
                append_event(connection, actor_id="system", action="exception.expired",
                             resource_type="budget_exception", resource_id=row["exception_id"],
                             detail={"product_id": product_id, "valid_until": row["valid_until"]},
                             occurred_at=self._now())
                continue
            if row["status"] == "granted" and row["valid_from"] <= today_text:
                active.append(row)
        active.sort(key=lambda r: (r["valid_until"], r["exception_id"]))
        return active

    def _funding_gap(self, connection, product_id: str, year: int, period: int, need: int) -> int:
        """计算在不写台账的情况下当前仍缺多少额度（基础额度 + 有效例外）。"""

        if need <= 0:
            return 0
        reserved, released = self._ledger_totals(connection, product_id, year, period)
        budget = self._period_budget_at(connection, product_id, year, period, self._today())["amount_cents"]
        remaining = need - min(need, max(0, budget - reserved + released))
        if remaining <= 0:
            return 0
        for exception in self._active_exceptions(connection, product_id, year, period):
            used = connection.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS v FROM exception_usage WHERE exception_id=?",
                (exception["exception_id"],),
            ).fetchone()["v"]
            remaining -= min(remaining, exception["cap_cents"] - used)
            if remaining <= 0:
                return 0
        return remaining

    def _fund(self, connection, product_id: str, year: int, period: int, need: int,
              commitment_id: str, share_id: str, actor_id: str) -> None:
        """预算占用瀑布：先使用期间基础额度，不足时按最早到期顺序消耗例外授权。"""

        if need <= 0:
            return
        reserved, released = self._ledger_totals(connection, product_id, year, period)
        base_available = self._period_budget_at(
            connection, product_id, year, period, self._today())["amount_cents"] - reserved + released
        remaining = need
        base_take = min(max(0, base_available), remaining)
        remaining -= base_take
        exception_takes: list[tuple[str, int]] = []
        if remaining > 0:
            for exception in self._active_exceptions(connection, product_id, year, period):
                used = connection.execute(
                    "SELECT COALESCE(SUM(amount_cents),0) AS v FROM exception_usage WHERE exception_id=?",
                    (exception["exception_id"],),
                ).fetchone()["v"]
                free = exception["cap_cents"] - used
                if free <= 0:
                    continue
                take = min(free, remaining)
                exception_takes.append((exception["exception_id"], take))
                remaining -= take
                if remaining == 0:
                    break
        if remaining > 0:
            raise BudgetExceeded(
                f"产品 {product_id} {year}-{period:02d} 额度不足，尚缺 {remaining} 分，"
                "需申请超预算例外")
        if base_take:
            connection.execute(
                "INSERT INTO budget_ledger(entry_id,product_id,year,period,direction,amount_cents,"
                "commitment_id,share_id,exception_id,actor_id,created_at) "
                "VALUES(?,?,?,?,'reserve',?,?,?,?,?,?)",
                (uuid.uuid4().hex, product_id, year, period, base_take,
                 commitment_id, share_id, None, actor_id, self._now()),
            )
        for exception_id, take in exception_takes:
            connection.execute(
                "INSERT INTO budget_ledger(entry_id,product_id,year,period,direction,amount_cents,"
                "commitment_id,share_id,exception_id,actor_id,created_at) "
                "VALUES(?,?,?,?,'reserve',?,?,?,?,?,?)",
                (uuid.uuid4().hex, product_id, year, period, take,
                 commitment_id, share_id, exception_id, actor_id, self._now()),
            )
            connection.execute(
                "INSERT INTO exception_usage(exception_id,commitment_id,share_id,amount_cents,created_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(exception_id,commitment_id,share_id) "
                "DO UPDATE SET amount_cents=amount_cents+excluded.amount_cents",
                (exception_id, commitment_id, share_id, take, self._now()),
            )

    def _release(self, connection, share, year: int, period: int, amount: int,
                 direction: str, actor_id: str) -> None:
        """释放份额占用：先回补该份额消耗的例外授权，再回补基础额度。"""

        if amount <= 0:
            return
        remaining = amount
        usages = connection.execute(
            "SELECT rowid AS rid, exception_id, amount_cents FROM exception_usage "
            "WHERE commitment_id=? AND share_id=? ORDER BY exception_id",
            (share["commitment_id"], share["share_id"]),
        ).fetchall()
        for usage in usages:
            if remaining <= 0:
                break
            take = min(usage["amount_cents"], remaining)
            if take == usage["amount_cents"]:
                connection.execute("DELETE FROM exception_usage WHERE rowid=?", (usage["rid"],))
            else:
                connection.execute(
                    "UPDATE exception_usage SET amount_cents=amount_cents-? WHERE rowid=?",
                    (take, usage["rid"]),
                )
            connection.execute(
                "INSERT INTO budget_ledger(entry_id,product_id,year,period,direction,amount_cents,"
                "commitment_id,share_id,exception_id,actor_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, share["product_id"], year, period, direction, take,
                 share["commitment_id"], share["share_id"], usage["exception_id"],
                 actor_id, self._now()),
            )
            remaining -= take
        if remaining > 0:
            connection.execute(
                "INSERT INTO budget_ledger(entry_id,product_id,year,period,direction,amount_cents,"
                "commitment_id,share_id,exception_id,actor_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, share["product_id"], year, period, direction,
                 remaining, share["commitment_id"], share["share_id"], None, actor_id, self._now()),
            )

    # -------------------------------------------------------------- 活动

    def create_activity(self, *, request_id: str, actor_id: str, activity_id: str,
                        team_type: str, name: str, starts_on: str, ends_on: str,
                        beneficiary_product_ids: list[str]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "activity_id": activity_id, "team_type": team_type,
                   "name": name, "starts_on": starts_on, "ends_on": ends_on,
                   "beneficiary_product_ids": beneficiary_product_ids}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_any(actor, "admin", "finance", "brand_owner",
                              "region_owner", "channel_owner")
            activity_id = self._id(activity_id, "activity_id")
            if team_type not in TEAM_TYPES:
                raise ValidationError("team_type 必须是 brand、region 或 channel")
            if actor["role"] not in ("admin", "finance") and actor["role"] != OWNER_ROLES[team_type]:
                raise PermissionDenied("只能创建本团队类型的活动")
            name = self._text(name, "name")
            start_day = self._day(starts_on, "starts_on")
            end_day = self._day(ends_on, "ends_on")
            if end_day < start_day:
                raise ValidationError("活动结束日期不能早于开始日期")
            if not beneficiary_product_ids:
                raise ValidationError("活动至少要声明一个受益产品")
            products = [self._product(connection, pid) for pid in beneficiary_product_ids]
            if len({p["product_id"] for p in products}) != len(products):
                raise ValidationError("受益产品不能重复")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO activities(activity_id,team_type,owner_actor_id,name,"
                        "starts_on,ends_on,status,created_by,created_at) VALUES(?,?,?,?,?,?, 'active',?,?)",
                        (activity_id, team_type, actor_id, name,
                         start_day.isoformat(), end_day.isoformat(), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("活动编号已经存在") from exc
                for ordinal, product in enumerate(products):
                    connection.execute(
                        "INSERT INTO activity_beneficiaries(activity_id,product_id,weight) VALUES(?,?,100)",
                        (activity_id, product["product_id"]),
                    )
                append_event(connection, actor_id=actor_id, action="activity.created",
                             resource_type="activity", resource_id=activity_id,
                             detail={"team_type": team_type, "name": name,
                                     "products": [p["product_id"] for p in products]},
                             occurred_at=self._now())
                return "activity", activity_id, {"activity_id": activity_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_activity", payload=payload, create=create)

    def _activity(self, connection, activity_id: str):
        row = connection.execute("SELECT * FROM activities WHERE activity_id=?", (activity_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"活动 {activity_id} 不存在")
        return row

    def _beneficiaries(self, connection, activity_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM activity_beneficiaries WHERE activity_id=? ORDER BY rowid",
            (activity_id,),
        ).fetchall()
        return [{"product_id": r["product_id"], "ordinal": i, "weight": r["weight"]}
                for i, r in enumerate(rows)]

    def cancel_activity(self, *, request_id: str, actor_id: str, activity_id: str,
                        reason: str) -> WriteReceipt:
        """取消活动并把全部未结账占用释放回原承诺对应额度。"""

        payload = {"actor_id": actor_id, "activity_id": activity_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            activity = self._activity(connection, activity_id)
            if activity["status"] == "cancelled":
                raise StateError("活动已经取消")
            if actor["role"] not in ("admin", "finance") and actor["actor_id"] != activity["owner_actor_id"]:
                raise PermissionDenied("只能取消自己负责的活动")
            reason = self._text(reason, "reason", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE activities SET status='cancelled' WHERE activity_id=?", (activity_id,)
                )
                released_shares = []
                for commitment in connection.execute(
                        "SELECT * FROM commitments WHERE activity_id=?", (activity_id,)):
                    if commitment["status"] == "cancelled":
                        continue
                    shares = connection.execute(
                        "SELECT * FROM commitment_shares WHERE commitment_id=?",
                        (commitment["commitment_id"],),
                    ).fetchall()
                    all_done = True
                    for share in shares:
                        if share["status"] == "settled":
                            continue
                        net_happened = share["accrued_cents"] - share["refunded_cents"]
                        # 已发生未结账部分继续保留占用，只释放未发生部分。
                        retain = min(share["reserved_cents"],
                                     max(share["settled_cents"], net_happened))
                        releasable = share["reserved_cents"] - retain
                        if releasable > 0:
                            self._release(connection, share, commitment["year"],
                                          commitment["period"], releasable, "release", actor_id)
                        connection.execute(
                            "UPDATE commitment_shares SET reserved_cents=? WHERE share_id=?",
                            (retain, share["share_id"]),
                        )
                        if net_happened > 0:
                            # 已发生费用仍需结账，争议份额继续冻结，其余回到草案/确认状态。
                            all_done = False
                        elif share["status"] != "disputed":
                            connection.execute(
                                "UPDATE commitment_shares SET status='released' WHERE share_id=?",
                                (share["share_id"],),
                            )
                        released_shares.append({"share_id": share["share_id"],
                                                "released_cents": releasable,
                                                "happened_cents": net_happened})
                    if all_done:
                        connection.execute(
                            "UPDATE commitments SET status='cancelled' WHERE commitment_id=?",
                            (commitment["commitment_id"],),
                        )
                    append_event(connection, actor_id=actor_id, action="commitment.released_by_cancel",
                                 resource_type="commitment", resource_id=commitment["commitment_id"],
                                 detail={"reason": reason, "shares": released_shares},
                                 occurred_at=self._now())
                append_event(connection, actor_id=actor_id, action="activity.cancelled",
                             resource_type="activity", resource_id=activity_id,
                             detail={"reason": reason}, occurred_at=self._now())
                return "activity", activity_id, {"activity_id": activity_id,
                                                  "released_shares": len(released_shares)}

            return self._idempotent(connection, request_id=request_id,
                                    action="cancel_activity", payload=payload, create=create)

    # -------------------------------------------------------------- 承诺

    def create_commitment(self, *, request_id: str, actor_id: str, activity_id: str,
                          commitment_id: str, contract_ref: str, amount_cents: int,
                          period: str, allocation_rule: str,
                          allocation_inputs: dict[str, Any] | None = None,
                          description: str = "") -> WriteReceipt:
        """申请合同承诺：按受益范围生成计划份额并逐产品占用期间额度。"""

        payload = {"actor_id": actor_id, "activity_id": activity_id,
                   "commitment_id": commitment_id, "contract_ref": contract_ref,
                   "amount_cents": amount_cents, "period": period,
                   "allocation_rule": allocation_rule, "allocation_inputs": allocation_inputs,
                   "description": description}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            activity = self._activity(connection, activity_id)
            if activity["status"] != "active":
                raise StateError("活动已取消，不能新增承诺")
            if actor["role"] not in ("admin", "finance") and actor["actor_id"] != activity["owner_actor_id"]:
                raise PermissionDenied("只能为自己负责的活动登记承诺")
            commitment_id = self._id(commitment_id, "commitment_id")
            contract_ref = self._text(contract_ref, "contract_ref")
            amount_cents = self._cents(amount_cents, "amount_cents")
            year, month, period_text = self._period(period)
            if allocation_rule not in ALLOCATION_RULES:
                raise ValidationError("分摊规则不受支持")
            allocation_inputs = allocation_inputs or {}
            beneficiaries = self._beneficiaries(connection, activity_id)
            today = self._today()
            # 申请时即按规则形成计划份额，作为预算占用依据（事实发生时再出草案）。
            plan = allocate(allocation_rule, amount_cents, beneficiaries, allocation_inputs)
            for item in plan["items"]:
                lifecycle = self._lifecycle_at(connection, item["product_id"], today)
                item["lifecycle_version_id"] = lifecycle["version_id"]
                item["stage_snapshot"] = lifecycle["stage"]

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO commitments(commitment_id,activity_id,contract_ref,description,"
                        "amount_cents,currency,year,period,allocation_rule,status,created_by,created_at) "
                        "VALUES(?,?,?,?,?, 'CNY',?,?,?, 'reserved',?,?)",
                        (commitment_id, activity_id, contract_ref, str(description or "")[:500],
                         amount_cents, year, month, allocation_rule, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("承诺编号已经存在") from exc
                share_summary = []
                for item in plan["items"]:
                    share_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO commitment_shares(share_id,commitment_id,product_id,ordinal,"
                        "weight,lifecycle_version_id,stage_snapshot,planned_cents,draft_cents,"
                        "confirmed_cents,reserved_cents,status) VALUES(?,?,?,?,?,?,?,?,?,0,?, 'planned')",
                        (share_id, commitment_id, item["product_id"], item["ordinal"],
                         item["weight"], item["lifecycle_version_id"], item["stage_snapshot"],
                         item["cents"], item["cents"], item["cents"]),
                    )
                    # 逐产品进行额度占用瀑布校验。
                    self._fund(connection, item["product_id"], year, month, item["cents"],
                               commitment_id, share_id, actor_id)
                    share_summary.append({"share_id": share_id, "product_id": item["product_id"],
                                          "planned_cents": item["cents"],
                                          "stage": item["stage_snapshot"]})
                append_event(connection, actor_id=actor_id, action="commitment.created",
                             resource_type="commitment", resource_id=commitment_id,
                             detail={"activity_id": activity_id, "contract_ref": contract_ref,
                                     "amount_cents": amount_cents, "period": period_text,
                                     "rule": allocation_rule, "plan": plan,
                                     "shares": share_summary},
                             occurred_at=self._now())
                return "commitment", commitment_id, {"commitment_id": commitment_id,
                                                      "shares": share_summary}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_commitment", payload=payload, create=create)

    def _commitment(self, connection, commitment_id: str):
        row = connection.execute(
            "SELECT * FROM commitments WHERE commitment_id=?", (commitment_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"承诺 {commitment_id} 不存在")
        return row

    def _shares(self, connection, commitment_id: str) -> list:
        return connection.execute(
            "SELECT * FROM commitment_shares WHERE commitment_id=? ORDER BY ordinal",
            (commitment_id,),
        ).fetchall()

    # -------------------------------------------------------------- 分摊草案

    def generate_allocation(self, *, request_id: str, actor_id: str, commitment_id: str,
                            allocation_inputs: dict[str, Any] | None = None) -> WriteReceipt:
        """按可解释规则生成（或重算）分摊草案；已有份额确认后不允许重算。"""

        payload = {"actor_id": actor_id, "commitment_id": commitment_id,
                   "allocation_inputs": allocation_inputs or {}}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            commitment = self._commitment(connection, commitment_id)
            if commitment["status"] not in ("reserved", "allocated"):
                raise StateError("承诺已结账或已取消，不能重新分摊")
            shares = self._shares(connection, commitment_id)
            if any(s["status"] in ("confirmed", "disputed", "settled") for s in shares):
                raise StateError("已有份额被确认或进入争议，不能整体重算草案")
            self._require_any(actor, "admin", "finance", "operator")
            result = allocate(commitment["allocation_rule"], commitment["amount_cents"],
                              [{"product_id": s["product_id"], "ordinal": s["ordinal"],
                                "weight": s["weight"]} for s in shares],
                              allocation_inputs or {})

            def create() -> tuple[str, str, dict[str, Any]]:
                allocation_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO allocation_runs(allocation_id,commitment_id,rule,inputs_json,"
                    "total_cents,residual_product_id,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (allocation_id, commitment_id, result["rule"],
                     canonical_json(result["inputs"]), result["total_cents"],
                     result["residual_product_id"], actor_id, self._now()),
                )
                items_by_product = {item["product_id"]: item for item in result["items"]}
                for share in shares:
                    item = items_by_product[share["product_id"]]
                    connection.execute(
                        "UPDATE commitment_shares SET draft_cents=?, basis_value=?, ratio_bp=?, "
                        "status='draft' WHERE share_id=?",
                        (item["cents"], item["basis_value"], item["ratio_bp"], share["share_id"]),
                    )
                connection.execute(
                    "UPDATE commitments SET status='allocated' WHERE commitment_id=?",
                    (commitment_id,),
                )
                append_event(connection, actor_id=actor_id, action="share.allocation_drafted",
                             resource_type="commitment", resource_id=commitment_id,
                             detail={"allocation_id": allocation_id, "rule": result["rule"],
                                     "inputs": result["inputs"], "items": result["items"],
                                     "residual_product_id": result["residual_product_id"]},
                             occurred_at=self._now())
                return "allocation", allocation_id, {"allocation_id": allocation_id,
                                                      "items": result["items"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="generate_allocation", payload=payload, create=create)

    def confirm_share(self, *, request_id: str, actor_id: str, commitment_id: str,
                      product_id: str, confirmed_cents: int | None = None) -> WriteReceipt:
        """业务负责人只能确认自己负责产品的份额。"""

        payload = {"actor_id": actor_id, "commitment_id": commitment_id,
                   "product_id": product_id, "confirmed_cents": confirmed_cents}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            commitment = self._commitment(connection, commitment_id)
            product = self._product(connection, product_id)
            share = connection.execute(
                "SELECT * FROM commitment_shares WHERE commitment_id=? AND product_id=?",
                (commitment_id, product_id),
            ).fetchone()
            if share is None:
                raise NotFoundError("该产品不在承诺受益范围内")
            if actor["role"] != "admin" and actor["actor_id"] != product["owner_actor_id"]:
                raise PermissionDenied("只能确认自己负责产品的份额")
            if share["status"] not in ("draft",):
                raise StateError("份额不在待确认状态")
            amount = share["draft_cents"] if confirmed_cents is None \
                else self._non_negative_cents(confirmed_cents, "confirmed_cents")
            if amount > share["reserved_cents"]:
                raise BudgetExceeded("确认金额超过该份额已占用额度，请先申请超预算例外并调整承诺")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE commitment_shares SET confirmed_cents=?, status='confirmed', "
                    "confirmed_at=? WHERE share_id=?",
                    (amount, self._now(), share["share_id"]),
                )
                append_event(connection, actor_id=actor_id, action="share.confirmed",
                             resource_type="commitment", resource_id=commitment_id,
                             detail={"share_id": share["share_id"], "product_id": product_id,
                                     "confirmed_cents": amount, "draft_cents": share["draft_cents"],
                                     "basis_value": share["basis_value"], "ratio_bp": share["ratio_bp"]},
                             occurred_at=self._now())
                return "share", share["share_id"], {"share_id": share["share_id"],
                                                     "confirmed_cents": amount}

            return self._idempotent(connection, request_id=request_id,
                                    action="confirm_share", payload=payload, create=create)

    def raise_dispute(self, *, request_id: str, actor_id: str, commitment_id: str,
                      product_id: str, reason: str) -> WriteReceipt:
        """仅冻结争议产品自己的份额，其余份额继续结账。"""

        payload = {"actor_id": actor_id, "commitment_id": commitment_id,
                   "product_id": product_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            product = self._product(connection, product_id)
            share = connection.execute(
                "SELECT * FROM commitment_shares WHERE commitment_id=? AND product_id=?",
                (commitment_id, product_id),
            ).fetchone()
            if share is None:
                raise NotFoundError("该产品不在承诺受益范围内")
            if actor["role"] not in ("admin", "finance") and actor["actor_id"] != product["owner_actor_id"]:
                raise PermissionDenied("只能对自己负责产品的份额提出争议")
            if share["status"] in ("settled", "released"):
                raise StateError("已结账或已释放的份额不能再争议")
            if share["status"] == "disputed":
                raise StateError("份额已在争议中")
            reason = self._text(reason, "reason", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE commitment_shares SET status='disputed', dispute_reason=? WHERE share_id=?",
                    (reason, share["share_id"]),
                )
                append_event(connection, actor_id=actor_id, action="share.disputed",
                             resource_type="commitment", resource_id=commitment_id,
                             detail={"share_id": share["share_id"], "product_id": product_id,
                                     "reason": reason}, occurred_at=self._now())
                return "share", share["share_id"], {"share_id": share["share_id"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="raise_dispute", payload=payload, create=create)

    def resolve_dispute(self, *, request_id: str, actor_id: str, commitment_id: str,
                        product_id: str, resolution: str,
                        confirmed_cents: int | None = None) -> WriteReceipt:
        """解决争议：份额恢复确认状态（不影响其他份额）。"""

        payload = {"actor_id": actor_id, "commitment_id": commitment_id,
                   "product_id": product_id, "resolution": resolution,
                   "confirmed_cents": confirmed_cents}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_any(actor, "admin", "finance")
            share = connection.execute(
                "SELECT * FROM commitment_shares WHERE commitment_id=? AND product_id=?",
                (commitment_id, product_id),
            ).fetchone()
            if share is None:
                raise NotFoundError("该产品不在承诺受益范围内")
            if share["status"] != "disputed":
                raise StateError("份额不在争议状态")
            resolution_text = self._text(resolution, "resolution", 500)
            amount = share["confirmed_cents"] if confirmed_cents is None \
                else self._non_negative_cents(confirmed_cents, "confirmed_cents")
            # 争议前已确认的份额恢复确认；尚在草案阶段的回到草案由负责人重新确认。
            target_status = "confirmed" if share["confirmed_at"] else "draft"

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE commitment_shares SET status=?, dispute_reason='', "
                    "confirmed_cents=? WHERE share_id=?",
                    (target_status, amount, share["share_id"]),
                )
                append_event(connection, actor_id=actor_id, action="share.dispute_resolved",
                             resource_type="commitment", resource_id=commitment_id,
                             detail={"share_id": share["share_id"], "product_id": product_id,
                                     "resolution": resolution_text, "confirmed_cents": amount,
                                     "restored_status": target_status},
                             occurred_at=self._now())
                return "share", share["share_id"], {"share_id": share["share_id"],
                                                     "status": target_status}

            return self._idempotent(connection, request_id=request_id,
                                    action="resolve_dispute", payload=payload, create=create)

    # ------------------------------------------------------ 应计、发票、退款

    def record_accrual(self, *, request_id: str, actor_id: str, commitment_id: str,
                       amount_cents: int, period: str, occurred_on: str,
                       description: str = "") -> WriteReceipt:
        """登记应计事实；跨期应计只打标，仍回到原承诺，不改动历史。"""

        payload = {"actor_id": actor_id, "commitment_id": commitment_id,
                   "amount_cents": amount_cents, "period": period,
                   "occurred_on": occurred_on, "description": description}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_any(actor, "admin", "finance", "operator")
            commitment = self._commitment(connection, commitment_id)
            if commitment["status"] not in ("allocated", "settled"):
                raise StateError("承诺尚未形成分摊草案，不能登记应计")
            amount_cents = self._cents(amount_cents, "amount_cents")
            year, month, period_text = self._period(period)
            occurred_day = self._day(occurred_on, "occurred_on")
            cross_period = 0 if (year == commitment["year"] and month == commitment["period"]) else 1
            shares = self._shares(connection, commitment_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                accrual_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO accrual_facts(accrual_id,commitment_id,year,period,amount_cents,"
                    "cross_period,description,occurred_on,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (accrual_id, commitment_id, year, month, amount_cents, cross_period,
                     str(description or "")[:500], occurred_day.isoformat(), actor_id, self._now()),
                )
                weights = [(share["share_id"],
                            max(1, share["draft_cents"] or share["planned_cents"]),
                            share["ordinal"], share["product_id"]) for share in shares]
                parts = largest_remainder(amount_cents, weights)
                allocated_map = {item["product_id"]: item["cents"] for item in parts}
                details = []
                for share in shares:
                    part = allocated_map[share["share_id"]]
                    connection.execute(
                        "INSERT INTO accrual_allocations(accrual_id,share_id,amount_cents) VALUES(?,?,?)",
                        (accrual_id, share["share_id"], part),
                    )
                    connection.execute(
                        "UPDATE commitment_shares SET accrued_cents=accrued_cents+? WHERE share_id=?",
                        (part, share["share_id"]),
                    )
                    details.append({"share_id": share["share_id"], "product_id": share["product_id"],
                                    "accrued_cents": part, "frozen": share["status"] == "disputed"})
                append_event(connection, actor_id=actor_id, action="accrual.recorded",
                             resource_type="commitment", resource_id=commitment_id,
                             detail={"accrual_id": accrual_id, "amount_cents": amount_cents,
                                     "period": period_text, "cross_period": bool(cross_period),
                                     "allocations": details}, occurred_at=self._now())
                return "accrual", accrual_id, {"accrual_id": accrual_id, "allocations": details}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_accrual", payload=payload, create=create)

    def record_invoice(self, *, request_id: str, actor_id: str, commitment_id: str,
                       invoice_no: str, amount_cents: int, period: str, issued_on: str,
                       accrual_id: str | None = None) -> WriteReceipt:
        """登记发票事实；跨期发票标注 cross_period，累计开票不得超过累计应计净额。"""

        payload = {"actor_id": actor_id, "commitment_id": commitment_id, "invoice_no": invoice_no,
                   "amount_cents": amount_cents, "period": period, "issued_on": issued_on,
                   "accrual_id": accrual_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_any(actor, "admin", "finance", "operator")
            commitment = self._commitment(connection, commitment_id)
            invoice_no = self._text(invoice_no, "invoice_no")
            amount_cents = self._cents(amount_cents, "amount_cents")
            year, month, period_text = self._period(period)
            issued_day = self._day(issued_on, "issued_on")
            cross_period = 0 if (year == commitment["year"] and month == commitment["period"]) else 1
            totals = connection.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS invoiced, "
                "COALESCE(SUM(refunded_cents),0) AS refunded FROM invoice_facts WHERE commitment_id=?",
                (commitment_id,),
            ).fetchone()
            accrued = connection.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS v FROM accrual_facts WHERE commitment_id=?",
                (commitment_id,),
            ).fetchone()["v"]
            if totals["invoiced"] + amount_cents - totals["refunded"] > accrued:
                raise StateError("累计开票净额不能超过累计应计金额")
            if accrual_id:
                if connection.execute("SELECT 1 FROM accrual_facts WHERE accrual_id=? AND commitment_id=?",
                                      (accrual_id, commitment_id)).fetchone() is None:
                    raise NotFoundError("关联的应计事实不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                invoice_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO invoice_facts(invoice_id,commitment_id,accrual_id,invoice_no,"
                        "year,period,amount_cents,cross_period,refunded_cents,issued_on,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,0,?,?,?)",
                        (invoice_id, commitment_id, accrual_id, invoice_no, year, month,
                         amount_cents, cross_period, issued_day.isoformat(), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("同一承诺下发票号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="invoice.recorded",
                             resource_type="commitment", resource_id=commitment_id,
                             detail={"invoice_id": invoice_id, "invoice_no": invoice_no,
                                     "amount_cents": amount_cents, "period": period_text,
                                     "cross_period": bool(cross_period), "accrual_id": accrual_id},
                             occurred_at=self._now())
                return "invoice", invoice_id, {"invoice_id": invoice_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_invoice", payload=payload, create=create)

    def record_refund(self, *, request_id: str, actor_id: str, commitment_id: str,
                      amount_cents: int, reason: str,
                      allocations: dict[str, int], invoice_id: str | None = None) -> WriteReceipt:
        """退款回到原承诺：按份额回冲并释放对应预算占用。"""

        payload = {"actor_id": actor_id, "commitment_id": commitment_id,
                   "amount_cents": amount_cents, "reason": reason,
                   "allocations": allocations, "invoice_id": invoice_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_any(actor, "admin", "finance", "operator")
            commitment = self._commitment(connection, commitment_id)
            amount_cents = self._cents(amount_cents, "amount_cents")
            reason = self._text(reason, "reason", 500)
            if not allocations:
                raise ValidationError("退款必须指定各产品份额的回冲金额")
            parts: list[tuple[object, int]] = []
            total = 0
            for product_id, part in allocations.items():
                share = connection.execute(
                    "SELECT * FROM commitment_shares WHERE commitment_id=? AND product_id=?",
                    (commitment_id, product_id),
                ).fetchone()
                if share is None:
                    raise NotFoundError(f"产品 {product_id} 不在承诺受益范围内")
                part_cents = self._cents(part, f"allocations.{product_id}")
                if part_cents > share["accrued_cents"] - share["refunded_cents"]:
                    raise StateError(f"产品 {product_id} 退款金额超过其应计净额")
                parts.append((share, part_cents))
                total += part_cents
            if total != amount_cents:
                raise ValidationError("各份额退款金额合计必须等于退款总额")
            invoice = None
            if invoice_id:
                invoice = connection.execute(
                    "SELECT * FROM invoice_facts WHERE invoice_id=? AND commitment_id=?",
                    (invoice_id, commitment_id),
                ).fetchone()
                if invoice is None:
                    raise NotFoundError("发票不存在")
                if invoice["refunded_cents"] + amount_cents > invoice["amount_cents"]:
                    raise StateError("该发票累计退款不能超过发票金额")

            def create() -> tuple[str, str, dict[str, Any]]:
                refund_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO refund_facts(refund_id,commitment_id,invoice_id,amount_cents,"
                    "reason,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (refund_id, commitment_id, invoice_id, amount_cents, reason,
                     actor_id, self._now()),
                )
                details = []
                for share, part_cents in parts:
                    connection.execute(
                        "INSERT INTO refund_allocations(refund_id,share_id,amount_cents) VALUES(?,?,?)",
                        (refund_id, share["share_id"], part_cents),
                    )
                    connection.execute(
                        "UPDATE commitment_shares SET refunded_cents=refunded_cents+? WHERE share_id=?",
                        (part_cents, share["share_id"]),
                    )
                    # 已结账份额：退款形成预算释放；未结账份额：直接回冲占用。
                    if share["status"] == "settled":
                        self._release(connection, share, commitment["year"], commitment["period"],
                                      part_cents, "refund", actor_id)
                    else:
                        outstanding = min(part_cents,
                                          share["reserved_cents"] - share["settled_cents"])
                        if outstanding > 0:
                            self._release(connection, share, commitment["year"],
                                          commitment["period"], outstanding, "refund", actor_id)
                    details.append({"share_id": share["share_id"], "product_id": share["product_id"],
                                    "refunded_cents": part_cents})
                if invoice is not None:
                    connection.execute(
                        "UPDATE invoice_facts SET refunded_cents=refunded_cents+? WHERE invoice_id=?",
                        (amount_cents, invoice_id),
                    )
                append_event(connection, actor_id=actor_id, action="refund.recorded",
                             resource_type="commitment", resource_id=commitment_id,
                             detail={"refund_id": refund_id, "amount_cents": amount_cents,
                                     "reason": reason, "invoice_id": invoice_id,
                                     "allocations": details}, occurred_at=self._now())
                return "refund", refund_id, {"refund_id": refund_id, "allocations": details}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_refund", payload=payload, create=create)

    # ---------------------------------------------------------------- 结账

    def settle_commitment(self, *, request_id: str, actor_id: str,
                          commitment_id: str) -> WriteReceipt:
        """结掉全部已确认且无争议的份额；争议份额保持冻结，不阻塞其他份额。"""

        payload = {"actor_id": actor_id, "commitment_id": commitment_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_any(actor, "admin", "finance")
            commitment = self._commitment(connection, commitment_id)
            if commitment["status"] == "cancelled":
                raise StateError("承诺已取消")
            shares = self._shares(connection, commitment_id)
            if commitment["status"] == "cancelled":
                raise StateError("承诺已取消")
            candidates = [s for s in shares if s["status"] == "confirmed"]
            if not candidates:
                raise StateError("没有已确认的份额可以结账（争议份额保持冻结）")
            # 逐份额预检补差额度，缺额度的份额不阻塞其他份额结账。
            eligible = []
            blocked = []
            for share in candidates:
                net = share["accrued_cents"] - share["refunded_cents"] - share["settled_cents"]
                top_up = max(0, net - (share["reserved_cents"] - share["settled_cents"]))
                if top_up and self._funding_gap(connection, share["product_id"],
                                                commitment["year"], commitment["period"],
                                                top_up) > 0:
                    blocked.append({"share_id": share["share_id"], "product_id": share["product_id"],
                                   "shortfall_cents": top_up})
                else:
                    eligible.append(share)
            if not eligible:
                raise BudgetExceeded("已确认份额均缺少有效额度或例外授权，无法结账")

            def create() -> tuple[str, str, dict[str, Any]]:
                settled = []
                for share in eligible:
                    net = share["accrued_cents"] - share["refunded_cents"] - share["settled_cents"]
                    outstanding_reserved = share["reserved_cents"] - share["settled_cents"]
                    if net > outstanding_reserved:
                        top_up = net - outstanding_reserved
                        self._fund(connection, share["product_id"], commitment["year"],
                                   commitment["period"], top_up, commitment_id,
                                   share["share_id"], actor_id)
                        connection.execute(
                            "UPDATE commitment_shares SET reserved_cents=reserved_cents+? WHERE share_id=?",
                            (top_up, share["share_id"]),
                        )
                    connection.execute(
                        "UPDATE commitment_shares SET settled_cents=settled_cents+?, status='settled' "
                        "WHERE share_id=?",
                        (net, share["share_id"]),
                    )
                    # 释放结账后剩余的占用（实际发生小于承诺时预算退回）。
                    refreshed = connection.execute(
                        "SELECT * FROM commitment_shares WHERE share_id=?", (share["share_id"],)
                    ).fetchone()
                    residual = refreshed["reserved_cents"] - refreshed["settled_cents"]
                    if residual > 0:
                        self._release(connection, refreshed, commitment["year"],
                                      commitment["period"], residual, "release", actor_id)
                        connection.execute(
                            "UPDATE commitment_shares SET reserved_cents=settled_cents WHERE share_id=?",
                            (share["share_id"],),
                        )
                    settled.append({"share_id": share["share_id"], "product_id": share["product_id"],
                                    "settled_cents": net})
                remaining = connection.execute(
                    "SELECT COUNT(*) AS c FROM commitment_shares WHERE commitment_id=? "
                    "AND status!='settled'", (commitment_id,),
                ).fetchone()["c"]
                if remaining == 0 and commitment["status"] != "settled":
                    connection.execute(
                        "UPDATE commitments SET status='settled' WHERE commitment_id=?",
                        (commitment_id,),
                    )
                append_event(connection, actor_id=actor_id, action="share.settled",
                             resource_type="commitment", resource_id=commitment_id,
                             detail={"settled": settled, "blocked": blocked,
                                     "frozen_remaining": remaining},
                             occurred_at=self._now())
                return "commitment", commitment_id, {"commitment_id": commitment_id,
                                                      "settled": settled, "blocked": blocked,
                                                      "frozen_remaining": remaining}

            return self._idempotent(connection, request_id=request_id,
                                    action="settle_commitment", payload=payload, create=create)

    # ------------------------------------------------------------ 例外授权

    def request_exception(self, *, request_id: str, actor_id: str, product_id: str,
                          period: str, cap_cents: int, valid_from: str, valid_until: str,
                          reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "product_id": product_id, "period": period,
                   "cap_cents": cap_cents, "valid_from": valid_from,
                   "valid_until": valid_until, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_any(actor, "admin", "finance", "brand_owner",
                              "region_owner", "channel_owner", "operator")
            product_id = self._id(product_id, "product_id")
            self._product(connection, product_id)
            year, month, _ = self._period(period)
            cap_cents = self._cents(cap_cents, "cap_cents")
            start_day = self._day(valid_from, "valid_from")
            end_day = self._day(valid_until, "valid_until")
            if end_day < start_day:
                raise ValidationError("例外有效期结束日不能早于开始日")
            reason = self._text(reason, "reason", 500)
            required_roles = self._approval_chain(cap_cents)

            def create() -> tuple[str, str, dict[str, Any]]:
                exception_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO budget_exceptions(exception_id,product_id,year,period,cap_cents,"
                    "valid_from,valid_until,reason,status,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,'requested',?,?)",
                    (exception_id, product_id, year, month, cap_cents,
                     start_day.isoformat(), end_day.isoformat(), reason, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="exception.requested",
                             resource_type="budget_exception", resource_id=exception_id,
                             detail={"product_id": product_id, "period": f"{year:04d}-{month:02d}",
                                     "cap_cents": cap_cents,
                                     "valid_from": start_day.isoformat(),
                                     "valid_until": end_day.isoformat(),
                                     "required_chain": list(required_roles), "reason": reason},
                             occurred_at=self._now())
                return "budget_exception", exception_id, {"exception_id": exception_id,
                                                           "required_chain": list(required_roles)}

            return self._idempotent(connection, request_id=request_id,
                                    action="request_exception", payload=payload, create=create)

    def _approval_chain(self, cap_cents: int) -> list[str]:
        for threshold, roles in APPROVAL_TIERS:
            if cap_cents <= threshold:
                return list(roles)
        return ["cfo"]

    def _exception(self, connection, exception_id: str):
        row = connection.execute(
            "SELECT * FROM budget_exceptions WHERE exception_id=?", (exception_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("例外授权不存在")
        return row

    def decide_exception(self, *, request_id: str, actor_id: str, exception_id: str,
                         decision: str, comment: str = "") -> WriteReceipt:
        """按授权链逐级审批：上一级通过后下一级才能处理，任一级拒绝即终结。"""

        payload = {"actor_id": actor_id, "exception_id": exception_id,
                   "decision": decision, "comment": comment}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            exception = self._exception(connection, exception_id)
            if exception["status"] != "requested":
                raise StateError("该例外不在待审批状态")
            chain = self._approval_chain(exception["cap_cents"])
            done = connection.execute(
                "SELECT COUNT(*) AS c FROM exception_approvals WHERE exception_id=? AND decision='approved'",
                (exception_id,),
            ).fetchone()["c"]
            if done >= len(chain):
                raise StateError("授权链已完成")
            required_role = chain[done]
            if actor["role"] != "admin" and actor["role"] != required_role:
                raise PermissionDenied(f"当前审批级次要求 {required_role} 角色")
            if decision not in ("approved", "rejected"):
                raise ValidationError("decision 必须是 approved 或 rejected")
            comment = str(comment or "").strip()[:500]

            def create() -> tuple[str, str, dict[str, Any]]:
                sequence = done + 1
                connection.execute(
                    "INSERT INTO exception_approvals(exception_id,sequence,approver_actor_id,"
                    "required_role,decision,comment,decided_at) VALUES(?,?,?,?,?,?,?)",
                    (exception_id, sequence, actor_id, required_role, decision,
                     comment, self._now()),
                )
                new_status = "requested"
                if decision == "rejected":
                    new_status = "rejected"
                elif sequence == len(chain):
                    new_status = "granted"
                connection.execute(
                    "UPDATE budget_exceptions SET status=? WHERE exception_id=?",
                    (new_status, exception_id),
                )
                append_event(connection, actor_id=actor_id,
                             action=f"exception.{decision}",
                             resource_type="budget_exception", resource_id=exception_id,
                             detail={"sequence": sequence, "required_role": required_role,
                                     "comment": comment, "new_status": new_status},
                             occurred_at=self._now())
                return "budget_exception", exception_id, {"exception_id": exception_id,
                                                           "status": new_status,
                                                           "sequence": sequence}

            return self._idempotent(connection, request_id=request_id,
                                    action="decide_exception", payload=payload, create=create)

    def revoke_exception(self, *, request_id: str, actor_id: str,
                         exception_id: str, reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "exception_id": exception_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_any(actor, "admin", "cfo", "finance")
            exception = self._exception(connection, exception_id)
            if exception["status"] != "granted":
                raise StateError("只有已授予的例外可以撤销")
            reason = self._text(reason, "reason", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE budget_exceptions SET status='revoked' WHERE exception_id=?",
                    (exception_id,),
                )
                append_event(connection, actor_id=actor_id, action="exception.revoked",
                             resource_type="budget_exception", resource_id=exception_id,
                             detail={"reason": reason}, occurred_at=self._now())
                return "budget_exception", exception_id, {"exception_id": exception_id,
                                                           "status": "revoked"}

            return self._idempotent(connection, request_id=request_id,
                                    action="revoke_exception", payload=payload, create=create)

    # ---------------------------------------------------------------- 查询

    def get_commitment(self, commitment_id: str) -> dict[str, Any]:
        connection = self.database.connection
        commitment = self._commitment(connection, commitment_id)
        activity = connection.execute(
            "SELECT * FROM activities WHERE activity_id=?", (commitment["activity_id"],)
        ).fetchone()
        shares = []
        for share in self._shares(connection, commitment_id):
            product = connection.execute(
                "SELECT * FROM products WHERE product_id=?", (share["product_id"],)
            ).fetchone()
            ledger = connection.execute(
                "SELECT direction, COALESCE(SUM(amount_cents),0) AS amount FROM budget_ledger "
                "WHERE share_id=? GROUP BY direction", (share["share_id"],),
            ).fetchall()
            shares.append({
                "share_id": share["share_id"], "product_id": share["product_id"],
                "product_name": product["name"], "owner_actor_id": product["owner_actor_id"],
                "ordinal": share["ordinal"], "stage_snapshot": share["stage_snapshot"],
                "stage_label": STAGE_LABELS[share["stage_snapshot"]],
                "lifecycle_version_id": share["lifecycle_version_id"],
                "planned_cents": share["planned_cents"], "draft_cents": share["draft_cents"],
                "basis_value": share["basis_value"], "ratio_bp": share["ratio_bp"],
                "confirmed_cents": share["confirmed_cents"], "reserved_cents": share["reserved_cents"],
                "accrued_cents": share["accrued_cents"], "settled_cents": share["settled_cents"],
                "refunded_cents": share["refunded_cents"], "status": share["status"],
                "dispute_reason": share["dispute_reason"],
                "ledger": {row["direction"]: row["amount"] for row in ledger},
            })
        accruals = [dict(r) for r in connection.execute(
            "SELECT * FROM accrual_facts WHERE commitment_id=? ORDER BY occurred_on, accrual_id",
            (commitment_id,))]
        invoices = [dict(r) for r in connection.execute(
            "SELECT * FROM invoice_facts WHERE commitment_id=? ORDER BY issued_on, invoice_id",
            (commitment_id,))]
        refunds = [dict(r) for r in connection.execute(
            "SELECT * FROM refund_facts WHERE commitment_id=? ORDER BY created_at, refund_id",
            (commitment_id,))]
        return {"commitment_id": commitment_id, "activity_id": commitment["activity_id"],
                "activity_name": activity["name"], "activity_status": activity["status"],
                "team_type": activity["team_type"], "contract_ref": commitment["contract_ref"],
                "description": commitment["description"], "amount_cents": commitment["amount_cents"],
                "year": commitment["year"], "period": commitment["period"],
                "allocation_rule": commitment["allocation_rule"], "status": commitment["status"],
                "created_by": commitment["created_by"], "created_at": commitment["created_at"],
                "shares": shares,
                "accruals": [{"accrual_id": r["accrual_id"], "amount_cents": r["amount_cents"],
                              "period": f"{r['year']:04d}-{r['period']:02d}",
                              "cross_period": bool(r["cross_period"]),
                              "occurred_on": r["occurred_on"], "description": r["description"]}
                             for r in accruals],
                "invoices": [{"invoice_id": r["invoice_id"], "invoice_no": r["invoice_no"],
                              "amount_cents": r["amount_cents"],
                              "refunded_cents": r["refunded_cents"],
                              "period": f"{r['year']:04d}-{r['period']:02d}",
                              "cross_period": bool(r["cross_period"]),
                              "issued_on": r["issued_on"], "accrual_id": r["accrual_id"]}
                             for r in invoices],
                "refunds": [{"refund_id": r["refund_id"], "amount_cents": r["amount_cents"],
                             "invoice_id": r["invoice_id"], "reason": r["reason"]}
                            for r in refunds]}

    def commitment_trace(self, commitment_id: str) -> dict[str, Any]:
        """组装一笔费用从申请、占用、发生、分摊、确认、结账到释放的全过程。"""

        snapshot = self.get_commitment(commitment_id)
        rows = self.database.connection.execute(
            "SELECT * FROM audit_events WHERE resource_type='commitment' AND resource_id=? "
            "ORDER BY sequence", (commitment_id,),
        ).fetchall()
        timeline = [{"sequence": r["sequence"], "event_id": r["event_id"], "actor_id": r["actor_id"],
                     "action": r["action"], "detail": json.loads(r["detail_json"]),
                     "occurred_at": r["occurred_at"]} for r in rows]
        return {"commitment": snapshot, "timeline": timeline}
