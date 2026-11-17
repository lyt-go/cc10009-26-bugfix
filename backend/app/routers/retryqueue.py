"""重试队列接口：维护重试条目，覆盖加入队列、发起重试、重试成功、标记最终失败等动作。

重试结果、尝试记录和最终失败清单各有独立读接口，方便网页执行侧和结果记录侧对齐；
导出接口放在动态路由之前注册，避免 /export 被 /{entry_id} 当成整数解析。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, BulkRetryPayload, EntryPayload, PageResult
from app.services.retryqueue import RetryqueueService

router = APIRouter(prefix="/api/retryqueue", tags=["重试队列"])

service = RetryqueueService()

LIST_FIELDS = ["条目编号", "失败URL", "失败原因码", "首次失败时间", "已重试次数", "下次重试时间", "退避级别", "最终处置"]
STATUSES = ["草稿", "等待重试", "重试中", "重试成功", "最终失败"]


@router.get("/export")
def export_entries(
    status: str | None = None,
    url: str | None = None,
    job: str | None = None,
) -> dict[str, Any]:
    """导出重试队列清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(status=status, url=url, job=job, page=1, size=10000)
    return {"module": "retryqueue", "total": total, "items": items}


@router.get("/stats", response_model=dict)
def stats() -> dict[str, Any]:
    """重试队列指标：条目总数、等待重试、重试中、成功率与数据集规模。"""
    return service.stats()


@router.get("/results", response_model=PageResult[dict])
def list_results(
    status: str | None = Query(default=None, description="重试成功、最终失败、版本冲突、进行中"),
    url: str | None = None,
    job: str | None = None,
    page: int = 1,
    size: int = 200,
) -> PageResult[dict]:
    """重试结果数据集：每个尝试一条，迟到冲突版本单独保留。"""
    if size > 1000:
        raise HTTPException(status_code=400, detail="每页最多 1000 条，请缩小分页范围")
    rows = service.list_results(status=status, url=url, job=job)
    start = max(page - 1, 0) * size
    return PageResult(items=rows[start:start + size], total=len(rows), page=page, size=size)


@router.get("/final-failures", response_model=PageResult[dict])
def list_final_failures(
    url: str | None = None,
    job: str | None = None,
    page: int = 1,
    size: int = 200,
) -> PageResult[dict]:
    """最终失败清单：与成功结果按失败URL互斥，同一URL不会两边同时出现。"""
    if size > 1000:
        raise HTTPException(status_code=400, detail="每页最多 1000 条，请缩小分页范围")
    rows = service.list_final_failures(url=url, job=job)
    start = max(page - 1, 0) * size
    return PageResult(items=rows[start:start + size], total=len(rows), page=page, size=size)


@router.post("/bulk", response_model=ActionResult)
def bulk_enqueue(payload: BulkRetryPayload) -> ActionResult:
    """一批失败URL一次提交整组重试，逐条返回尝试次数与最终处置。"""
    urls = [item for item in payload.urls if str(item or "").strip()]
    if not urls:
        return ActionResult(ok=False, message="请至少提供一个失败URL")
    results = service.bulk_enqueue(urls, reason=payload.reason, job=payload.job, auto_retry=payload.auto_retry)
    accepted = sum(1 for item in results if item.get("ok"))
    return ActionResult(ok=accepted > 0, message=f"已登记 {accepted}/{len(results)} 条重试条目", items=results)


@router.post("/drain", response_model=ActionResult)
def drain() -> ActionResult:
    """并发执行重试队列：同一轮里同一条目只会被推进一次。"""
    processed = service.drain()
    return ActionResult(ok=True, message=f"本轮推进 {len(processed)} 条重试条目", items=processed)


@router.post("/resync", response_model=ActionResult)
def resync() -> ActionResult:
    """把重试结果同步到网页执行侧清单与抓取作业清单。"""
    summary = service.resync_all()
    return ActionResult(ok=True, message="重试结果已同步到网页执行侧清单与抓取作业清单", summary=summary)


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按条目编号或失败URL检索"),
    status: str | None = Query(default=None, description="草稿、等待重试、重试中、重试成功、最终失败"),
    url: str | None = None,
    job: str | None = None,
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按条目编号、失败URL与状态过滤重试队列列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, url=url, job=job, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}/attempts", response_model=PageResult[dict])
def list_attempts(entry_id: int) -> PageResult[dict]:
    """读取单条重试条目的全部尝试记录，按记录顺序回溯每次状态变化。"""
    rows = service.list_attempts(entry_id)
    if rows is None:
        raise HTTPException(status_code=404, detail=f"重试条目 {entry_id} 不存在或已归档")
    return PageResult(items=rows, total=len(rows), page=1, size=len(rows) or 1)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条重试条目明细，附带尝试记录与重试结果。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"重试条目 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条重试条目，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="重试条目已登记", entry=entry)


def _as_attempt(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条重试条目执行状态动作；不允许的流转与重复提交会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    attempt = _as_attempt(payload.values.get("attempt"))
    result_id = payload.values.get("result_id")
    entry, message = service.run_action(
        entry_id,
        action,
        attempt=attempt,
        result_id=str(result_id) if result_id else None,
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
