"""公共测试夹具：可确定的时钟 + 带编号的服务实例。"""

from __future__ import annotations

import logging

import pytest

from metadata_catalog.audit import AuditLog
from metadata_catalog.service import MetadataCatalogService

logger = logging.getLogger("catalog-tests")


class TickClock:
    """每次取时间递增一个刻度，测试里时间顺序确定可断言。"""

    def __init__(self) -> None:
        self._n = 0

    def __call__(self) -> str:
        self._n += 1
        return f"2026-10-01T00:00:{self._n:02d}Z"


@pytest.fixture()
def clock() -> TickClock:
    return TickClock()


@pytest.fixture()
def svc(clock: TickClock) -> MetadataCatalogService:
    return MetadataCatalogService(audit=AuditLog(clock=clock))


def log_decision(case: str, inp: object, decision: object) -> None:
    """统一打印"输入 -> 判定依据"，满足测试可解释性要求。"""
    logger.info("用例[%s] 输入=%s => 判定=%s", case, inp, decision)
