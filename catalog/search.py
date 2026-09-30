"""检索与过滤：名称、标签、负责人组合条件，结果稳定排序。

检索语义：
- 多个条件之间为 AND；标签集合条件为“包含全部给定标签”（子集匹配）。
- 名称/字段名匹配为大小写不敏感的子串包含。
- 结果按名称字典序稳定排序，保证同样输入永远得到同样顺序。
"""

from __future__ import annotations

from dataclasses import dataclass

from catalog.entities import EntityRegistry
from catalog.models import DatasetInfo


@dataclass(frozen=True)
class SearchCriteria:
    """组合检索条件；所有条件之间为 AND。None 表示不限制。"""

    name_contains: str | None = None
    tags: frozenset[str] = frozenset()
    owner: str | None = None
    field_name_contains: str | None = None

    def matches(self, dataset: DatasetInfo) -> bool:
        if self.name_contains is not None and self.name_contains.lower() not in dataset.name.lower():
            return False
        if self.owner is not None and dataset.owner != self.owner:
            return False
        if self.tags and not self.tags <= dataset.tags:  # 必须包含全部给定标签
            return False
        if self.field_name_contains is not None:
            needle = self.field_name_contains.lower()
            if not any(needle in fname.lower() for fname in dataset.fields):
                return False
        return True


def search_datasets(registry: EntityRegistry, criteria: SearchCriteria) -> list[DatasetInfo]:
    """按组合条件过滤，结果按数据集名称稳定排序。"""
    matched = [ds for ds in registry.all_datasets() if criteria.matches(ds)]
    matched.sort(key=lambda ds: ds.name)
    return matched
