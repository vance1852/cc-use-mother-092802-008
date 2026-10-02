"""费用域的业务异常，复用基础服务的 HTTP 状态约定。"""

from __future__ import annotations


class ExpenseError(Exception):
    code = "expense_error"
    status = 400


class ValidationError(ExpenseError):
    code = "validation_error"


class NotFoundError(ExpenseError):
    code = "not_found"
    status = 404


class PermissionDenied(ExpenseError):
    code = "permission_denied"
    status = 403


class ConflictError(ExpenseError):
    code = "conflict"
    status = 409


class BudgetExceeded(ExpenseError):
    """占用超过期间可用额度，需走超预算例外。"""

    code = "budget_exceeded"
    status = 422


class AllocationError(ExpenseError):
    """分摊规则无法形成自洽草案。"""

    code = "allocation_error"


class WorkflowError(ExpenseError):
    """当前状态不允许该动作（如对已取消承诺登记发票）。"""

    code = "workflow_error"
    status = 409


class AuthorizationChainError(ExpenseError):
    """超预算例外的授权链不完整或越权。"""

    code = "authorization_chain_error"
    status = 403
