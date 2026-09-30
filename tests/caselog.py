"""用例日志辅助：统一打印输入与判定依据。"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger("catalog-tests")

if not log.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
    )

_UNSET = object()


def log_case(title: str, inputs: dict, basis: str, result=_UNSET) -> None:
    """统一的用例日志格式：标题 / 输入 / 判定依据 / 结果。"""
    log.info("=" * 72)
    log.info("用例: %s", title)
    for k, v in inputs.items():
        log.info("  输入 %-14s = %r", k, v)
    log.info("  判定依据: %s", basis)
    if result is not _UNSET:
        log.info("  结果: %r", result)
