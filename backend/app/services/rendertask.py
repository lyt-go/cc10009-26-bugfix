"""渲染任务业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "rendertask"
REQUIRED_FIELDS = ["作业编号", "目标URL", "浏览器版本"]
STATUS_ORDER = ["草稿", "排队中", "渲染中", "已完成", "已超时"]
ACTION_RULES = {"提交排队": "排队中", "启动渲染": "渲染中", "终止渲染": "已超时"}
NEGATIVE_ACTIONS = []


class RendertaskService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("作业编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def list_failures(self) -> list[dict[str, Any]]:
        """网页执行侧的失败URL集合：每个渲染作业一行，展开成逐URL的记录。"""
        rows: list[dict[str, Any]] = []
        for job in store.rows(MODULE):
            retry_state = job.get("重试状态") or ""
            for url in job.get("失败URL集合") or []:
                rows.append({
                    "渲染作业ID": job.get("id"),
                    "作业编号": job.get("作业编号"),
                    "目标URL": url,
                    "失败URL": url,
                    "重试状态": retry_state,
                    "最近失败原因码": job.get("最近失败原因码", ""),
                    "同步版本": job.get("同步版本", 0),
                })
        return rows

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["失败URL集合"] = [str(url) for url in (values.get("失败URL集合") or []) if str(url or "").strip()]
        entry["失败URL数"] = len(entry["失败URL集合"])
        entry["重试状态"] = str(values.get("重试状态") or "")
        entry["同步版本"] = int(values.get("同步版本") or 0)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"渲染作业 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于渲染任务可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"渲染作业已{action}"
