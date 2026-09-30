"""公共夹具。用例日志辅助见 caselog.log_case。"""

from __future__ import annotations

import pytest

from catalog import CatalogService


@pytest.fixture
def svc():
    return CatalogService()
