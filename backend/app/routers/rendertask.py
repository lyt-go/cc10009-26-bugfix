"""渲染任务接口：维护渲染作业，并作为网页执行侧读取重试结果、维护失败URL集合。

网页执行侧在这里落库：重试结果进来后，失败URL集合随之增减；成功结果和最终失败
清单不会同时包含同一 URL。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.rendertask import RendertaskService
from app.services.retryqueue import RetryqueueService

router = APIRouter(prefix="/api/rendertask", tags=["渲染任务"])

service = RendertaskService()
retry_service = RetryqueueService()

LIST_FIELDS = ["作业编号", "目标URL", "浏览器版本", "视口宽度", "视口高度", "等待策略", "脚本注入", "超时阈值秒"]
STATUSES = ["草稿", "排队中", "渲染中", "已完成", "已超时"]


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出渲染任务清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "rendertask", "total": total, "items": items}


@router.get("/failures", response_model=dict)
def list_failure_urls() -> dict[str, Any]:
    """网页执行侧的失败URL集合：按渲染作业汇总，附带每条的最近重试状态。"""
    rows = service.list_failures()
    return {"total": len(rows), "items": rows}


@router.get("/retry-results", response_model=PageResult[dict])
def list_retry_results(
    status: str | None = None,
    url: str | None = None,
    page: int = 1,
    size: int = 200,
) -> PageResult[dict]:
    """网页执行侧读取重试结果：只读数据集，不反向改重试队列。"""
    rows = retry_service.list_results(status=status, url=url)
    start = max(page - 1, 0) * size
    return PageResult(items=rows[start:start + size], total=len(rows), page=page, size=size)


@router.post("/sync", response_model=ActionResult)
def sync_from_retry() -> ActionResult:
    """结果记录：把重试结果同步到网页执行侧清单，相关清单随之联动。"""
    summary = retry_service.resync_all()
    return ActionResult(ok=True, message="重试结果已同步到网页执行侧清单与抓取作业清单", summary=summary)


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按作业编号检索"),
    status: str | None = Query(default=None, description="草稿、排队中、渲染中、已完成、已超时"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按作业编号与状态过滤渲染任务列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条渲染作业明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"渲染作业 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条渲染作业，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="渲染作业已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条渲染作业执行提交排队、启动渲染、终止渲染；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
