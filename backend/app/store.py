"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

除了业务模块表，这里还保存重试链路产生的数据集（重试结果、最终失败清单）。
数据集不属于业务模块，不计入运营概览的模块数，但仍然走同一把锁，保证并发下只有
一个版本能看到中间状态。
"""
from __future__ import annotations

import threading
from typing import Any

from app.seed import SEED_DATASETS, SEED_ROWS

RETRY_ATTEMPT_DATASET = "retryattempt"
RETRY_RESULT_DATASET = "retryresult"
RETRY_FINAL_DATASET = "finalfailure"


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._datasets: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_DATASETS.items()
        }
        # 所有写操作（状态流转 + 数据集写入 + 网页执行侧清单回写）共用一把可重入锁，
        # 并发回调只可能有一个先拿到锁，后到的按版本校验成为幂等提交或冲突记录。
        self.lock = threading.RLock()

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def dataset(self, name: str) -> list[dict[str, Any]]:
        """读写重试链路数据集；数据集不参与模块概览统计。"""
        return self._datasets.setdefault(name, [])

    def dataset_names(self) -> list[str]:
        return sorted(self._datasets)

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
