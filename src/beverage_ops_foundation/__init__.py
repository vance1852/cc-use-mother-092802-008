"""技能赛训协作基础服务的服务端基础包。"""

from .fee_service import FeeService
from .service import DomainService

__all__ = ["DomainService", "FeeService"]
