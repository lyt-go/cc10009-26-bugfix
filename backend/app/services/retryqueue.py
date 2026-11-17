"""重试队列业务规则：状态收敛、尝试记录、重试结果与最终失败清单都在这里。

状态收敛规则（修掉「跨级进入最终失败」和「成功状态不可达」）：

    草稿 ──加入队列──> 等待重试 ──发起重试──> 重试中 ──重试成功──> 重试成功
                              ^                 │
                              └──发起重试（失败后重新排队，退避级别+1）
                                                │
                                ──标记最终失败──> 最终失败

只有「重试中」能收敛为「重试成功」或「最终失败」；「添加/发起」不会跨级直达终态。
终态确认后，迟到的成功回调只会被记成冲突版本，不会把状态改回去。

数据集：
    retryattempt   每次状态变化的尝试记录（追加写，可追溯）
    retryresult    每个尝试的结果（同一尝试的迟到版本会以「版本冲突」再保留一份）
    finalfailure   最终失败清单（与成功结果按 URL 互斥）
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any

from app.store import (
    RETRY_ATTEMPT_DATASET,
    RETRY_FINAL_DATASET,
    RETRY_RESULT_DATASET,
    store,
)

MODULE = "retryqueue"
EXECUTION_MODULE = "rendertask"
JOB_MODULE = "crawljob"
REQUIRED_FIELDS = ["条目编号", "失败URL", "失败原因码"]
STATUS_ORDER = ["草稿", "等待重试", "重试中", "重试成功", "最终失败"]
TERMINAL_STATUSES = {"重试成功", "最终失败"}
DRAFT, WAITING, RETRYING, SUCCEEDED, FAILED_FINAL = STATUS_ORDER

ACTION_RULES = {
    "加入队列": WAITING,
    "发起重试": RETRYING,
    "重试成功": SUCCEEDED,
    "标记最终失败": FAILED_FINAL,
    "重新加入队列": WAITING,
}
NEGATIVE_ACTIONS = ["标记最终失败"]

# 允许的流转边：不在表里的动作一律拒绝，避免跨级进入终态。
# 关键约束：重试中只能收敛为「重试成功」或「最终失败」，不能回到等待/草稿；
# 终态之后只有「重新加入队列」能开新一轮，历史尝试与结果全部保留。
# 每个状态映射到「可执行动作 → 目标状态」，动作级粒度比只看目标状态更严格。
ALLOWED_ACTIONS: dict[str, dict[str, str]] = {
    DRAFT: {"加入队列": WAITING},
    WAITING: {"发起重试": RETRYING},
    RETRYING: {"重试成功": SUCCEEDED, "标记最终失败": FAILED_FINAL},
    SUCCEEDED: {"重新加入队列": WAITING},
    FAILED_FINAL: {"重新加入队列": WAITING},
}

# 退避上限：到顶之后不再排下一次重试，下一次调度直接收敛为最终失败。
MAX_BACKOFF_LEVEL = 5

RESULT_SUCCESS = "重试成功"
RESULT_FINAL = "最终失败"
RESULT_CONFLICT = "版本冲突"
RESULT_RUNNING = "进行中"


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


class RetryqueueService:
    # ------------------------------------------------------------------ 读取
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        url: str | None = None,
        job: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("条目编号", "")) or keyword in str(row.get("失败URL", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if url:
            rows = [row for row in rows if url in str(row.get("失败URL", ""))]
        if job:
            rows = [row for row in rows if str(row.get("作业编号", "")) == job]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def stats(self) -> dict[str, Any]:
        rows = store.rows(MODULE)
        total = len(rows)
        waiting = sum(1 for row in rows if row.get("status") == WAITING)
        retrying = sum(1 for row in rows if row.get("status") == RETRYING)
        succeeded = sum(1 for row in rows if row.get("status") == SUCCEEDED)
        failed = sum(1 for row in rows if row.get("status") == FAILED_FINAL)
        settled = succeeded + failed
        return {
            "条目总数": total,
            "等待重试数": waiting,
            "重试中数": retrying,
            "重试成功数": succeeded,
            "最终失败数": failed,
            "重试成功率": round(succeeded / settled, 4) if settled else 0,
            "尝试记录数": len(store.dataset(RETRY_ATTEMPT_DATASET)),
            "最终失败清单数": len(store.dataset(RETRY_FINAL_DATASET)),
        }

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        detail = dict(entry)
        detail["尝试记录"] = self.attempts_of(entry_id)
        detail["重试结果"] = self.results_of(entry_id)
        return detail

    def attempts_of(self, entry_id: int) -> list[dict[str, Any]]:
        return [
            dict(row) for row in store.dataset(RETRY_ATTEMPT_DATASET)
            if _as_int(row.get("条目ID")) == entry_id
        ]

    def results_of(self, entry_id: int) -> list[dict[str, Any]]:
        return [
            dict(row) for row in store.dataset(RETRY_RESULT_DATASET)
            if _as_int(row.get("条目ID")) == entry_id
        ]

    def list_attempts(self, entry_id: int) -> list[dict[str, Any]] | None:
        if store.find(MODULE, entry_id) is None:
            return None
        return self.attempts_of(entry_id)

    def list_results(self, *, status: str | None = None, url: str | None = None,
                     job: str | None = None) -> list[dict[str, Any]]:
        rows = [dict(row) for row in store.dataset(RETRY_RESULT_DATASET)]
        if status:
            rows = [row for row in rows if row.get("结果状态") == status]
        if url:
            rows = [row for row in rows if url in str(row.get("失败URL", ""))]
        if job:
            rows = [row for row in rows if str(row.get("作业编号", "")) == job]
        return rows

    def list_final_failures(self, *, url: str | None = None, job: str | None = None) -> list[dict[str, Any]]:
        rows = [dict(row) for row in store.dataset(RETRY_FINAL_DATASET)]
        if url:
            rows = [row for row in rows if url in str(row.get("失败URL", ""))]
        if job:
            rows = [row for row in rows if str(row.get("作业编号", "")) == job]
        return rows

    # ------------------------------------------------------------------ 登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.lock:
            entry = self._new_entry(values)
        return entry, []

    def _new_entry(self, values: dict[str, Any]) -> dict[str, Any]:
        rows = store.rows(MODULE)
        now = _now()
        entry: dict[str, Any] = {"id": store.next_id(MODULE)}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["作业编号"] = str(values.get("作业编号") or "").strip()
        entry["首次失败时间"] = str(values.get("首次失败时间") or "").strip() or now
        entry["已重试次数"] = _as_int(values.get("已重试次数"), 0)
        entry["退避级别"] = _as_int(values.get("退避级别"), 0)
        entry["下次重试时间"] = str(values.get("下次重试时间") or "").strip() or now
        entry["最终处置"] = ""
        entry["status"] = DRAFT
        entry["pending"] = True
        entry["abnormal"] = False
        entry["当前尝试"] = 0
        entry["轮次"] = 1
        entry["版本"] = 0
        rows.append(entry)
        return entry

    def _next_code(self) -> str:
        numbers = []
        for row in store.rows(MODULE):
            tail = str(row.get("条目编号") or "").rsplit("-", 1)[-1]
            if tail.isdigit():
                numbers.append(int(tail))
        return f"RETR-{(max(numbers, default=0) + 1):04d}"

    def bulk_enqueue(self, urls: list[str], *, reason: str = "", job: str = "",
                     auto_retry: bool = False) -> list[dict[str, Any]]:
        """一批失败URL一次提交整组重试，逐条返回尝试和最终处置。"""
        results: list[dict[str, Any]] = []
        for raw_url in urls:
            url = str(raw_url or "").strip()
            if not url:
                results.append({"ok": False, "message": "失败URL为空，已跳过", "失败URL": url,
                                "尝试次数": 0, "最终处置": ""})
                continue
            with store.lock:
                entry = self._new_entry({
                    "条目编号": self._next_code(),
                    "失败URL": url,
                    "失败原因码": reason or "批量重试",
                    "作业编号": job,
                })
                entry, message = self._transition(entry, "加入队列", actor="批量重试")
                if entry is not None and auto_retry:
                    entry, message = self._transition(entry, "发起重试", actor="批量重试")
            results.append({
                "ok": entry is not None,
                "message": message,
                "条目ID": entry.get("id") if entry else None,
                "条目编号": entry.get("条目编号") if entry else None,
                "失败URL": url,
                "尝试次数": entry.get("已重试次数", 0) if entry else 0,
                "当前尝试": entry.get("当前尝试", 0) if entry else 0,
                "当前状态": entry.get("status") if entry else None,
                "最终处置": entry.get("最终处置", "") if entry else "",
            })
        return results

    # ------------------------------------------------------------------ 流转
    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        actor: str = "值班管理员",
        attempt: int | None = None,
        result_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于重试队列可执行范围"
        with store.lock:
            # 锁内取数：并发回调里先拿到锁的那个完成流转，后到的按终态/版本判断。
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"重试条目 {entry_id} 不存在或已归档"
            return self._transition(entry, action, actor=actor, attempt=attempt, result_id=result_id)

    def _transition(
        self,
        entry: dict[str, Any],
        action: str,
        *,
        actor: str,
        attempt: int | None = None,
        result_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        current = str(entry.get("status") or DRAFT)
        target = ACTION_RULES[action]

        if action not in ALLOWED_ACTIONS.get(current, {}):
            if current in TERMINAL_STATUSES and action in {"重试成功", "标记最终失败"}:
                return self._handle_late_result(entry, action, actor, attempt, result_id, current)
            return None, f"状态「{current}」不允许执行「{action}」"
        target = ALLOWED_ACTIONS[current][action]

        stale = self._stale_attempt(entry, attempt, action)
        if stale is not None:
            # 节点离线后旧尝试才返回：状态保持当前值，迟到版本与当前版本各留一份结果。
            conflict = self._write_conflict_result(entry, stale, action, actor)
            self._record_attempt(
                entry, attempt_number=stale, action=action, before=current,
                after=current, actor=actor, result_status=RESULT_CONFLICT,
                note=f"迟到的旧尝试（当前尝试 {entry.get('当前尝试', 0)}），状态未改变，冲突版本 {conflict['结果编号']}",
            )
            return None, (
                f"尝试 {stale} 已过期，当前尝试为 {entry.get('当前尝试', 0)}，"
                f"迟到结果已保留为冲突版本 {conflict['结果编号']}，当前状态未改变"
            )

        before = current

        if action == "发起重试":
            level = _as_int(entry.get("退避级别"), 0)
            attempt_no = _as_int(entry.get("当前尝试"), 0) + 1
            entry["当前尝试"] = attempt_no
            entry["已重试次数"] = _as_int(entry.get("已重试次数"), 0) + 1
            entry["版本"] = _as_int(entry.get("版本"), 0) + 1
            entry["status"] = RETRYING
            entry["pending"] = True
            entry["abnormal"] = False
            entry["最终处置"] = ""
            entry["上次尝试开始时间"] = _now()
            entry["下次重试时间"] = self._next_retry_time(level)
            self._record_attempt(
                entry, attempt_number=attempt_no, action=action, before=before, after=RETRYING,
                actor=actor, result_status=RESULT_RUNNING,
                note=f"第 {attempt_no} 次尝试已派发，退避级别 {level}",
            )
            self._write_result(
                entry, attempt_no, RESULT_RUNNING, actor, _now(),
                f"第 {attempt_no} 次尝试已派发，退避级别 {level}", conflict=False,
            )
            if level >= MAX_BACKOFF_LEVEL:
                # 退避已到上限：先进入重试中，再在同一原子步骤收敛为最终失败，不跨级。
                return self._finalize(entry, action="标记最终失败", actor=actor,
                                      reason=f"退避级别 {level} 已达上限", before=RETRYING)
            return entry, f"重试条目已发起重试（第 {attempt_no} 次尝试）"

        if action == "重试成功":
            return self._succeed(entry, actor=actor, before=before)

        if action == "标记最终失败":
            return self._finalize(entry, action=action, actor=actor, reason="人工标记最终失败", before=before)

        # 加入队列 / 重新加入队列
        # 加入队列保留登记时带来的历史退避级别；重新加入在此基础上再进一级。
        if action == "重新加入队列":
            entry["退避级别"] = min(_as_int(entry.get("退避级别"), 0) + 1, MAX_BACKOFF_LEVEL)
        else:
            entry["退避级别"] = min(_as_int(entry.get("退避级别"), 0), MAX_BACKOFF_LEVEL)
        entry["版本"] = _as_int(entry.get("版本"), 0) + 1
        entry["status"] = WAITING
        entry["pending"] = True
        entry["abnormal"] = False
        entry["最终处置"] = ""
        entry["下次重试时间"] = self._next_retry_time(_as_int(entry.get("退避级别"), 0))
        if action == "重新加入队列":
            # 重新加入：保留历史尝试与结果作为依据，只清掉当前最终失败清单里的这一条。
            entry["轮次"] = _as_int(entry.get("轮次"), 1) + 1
            entry["重新加入时间"] = _now()
            self._drop_final(entry)
            self._sync_execution(entry)
        # 排队不是一次尝试：不增加尝试次数，只记一条排队记录指向当前尝试。
        attempt_no = _as_int(entry.get("当前尝试"), 0)
        self._record_attempt(
            entry, attempt_number=attempt_no, action=action, before=before, after=WAITING,
            actor=actor, result_status="已排队",
            note=f"退避级别 {entry['退避级别']}，等待第 {attempt_no + 1} 次重试",
        )
        return entry, f"重试条目已{action}"

    def _next_retry_time(self, level: int) -> str:
        seconds = min(2 ** max(level, 0), 300)
        return (datetime.now() + timedelta(seconds=seconds)).replace(microsecond=0).isoformat(sep=" ")

    def _stale_attempt(self, entry: dict[str, Any], attempt: int | None, action: str) -> int | None:
        if attempt is None or action not in {"重试成功", "标记最终失败"}:
            return None
        current = _as_int(entry.get("当前尝试"), 0)
        return attempt if attempt < current else None

    # ------------------------------------------------------------------ 终态
    def _succeed(self, entry: dict[str, Any], *, actor: str, before: str) -> tuple[dict[str, Any] | None, str]:
        if entry.get("status") != RETRYING:
            return None, f"状态「{entry.get('status')}」不允许重试成功，只有「重试中」可以收敛为成功"
        attempt_no = _as_int(entry.get("当前尝试"), 1)
        now = _now()
        entry["status"] = SUCCEEDED
        entry["pending"] = False
        entry["abnormal"] = False
        entry["最终处置"] = RESULT_SUCCESS
        entry["下次重试时间"] = ""
        entry["成功时间"] = now
        entry["版本"] = _as_int(entry.get("版本"), 0) + 1
        result = self._write_result(entry, attempt_no, RESULT_SUCCESS, actor, now, RESULT_SUCCESS, conflict=False)
        self._record_attempt(
            entry, attempt_number=attempt_no, action="重试成功", before=before, after=SUCCEEDED,
            actor=actor, result_status=RESULT_SUCCESS, note=f"成功回调已确认，结果 {result['结果编号']}",
        )
        self._drop_final(entry)
        self._sync_execution(entry)
        self._refresh_job(entry)
        return entry, f"重试条目已重试成功（第 {attempt_no} 次尝试，结果 {result['结果编号']}）"

    def _finalize(
        self,
        entry: dict[str, Any],
        *,
        action: str,
        actor: str,
        reason: str,
        before: str,
    ) -> tuple[dict[str, Any] | None, str]:
        if entry.get("status") in TERMINAL_STATUSES:
            return None, f"重试条目已处于终态「{entry.get('status')}」，不能再标记最终失败"
        attempt_no = _as_int(entry.get("当前尝试"), 1)
        now = _now()
        entry["status"] = FAILED_FINAL
        entry["pending"] = False
        entry["abnormal"] = True
        entry["最终处置"] = reason or RESULT_FINAL
        entry["最终失败时间"] = now
        entry["下次重试时间"] = ""
        entry["版本"] = _as_int(entry.get("版本"), 0) + 1
        result = self._write_result(entry, attempt_no, RESULT_FINAL, actor, now, reason or RESULT_FINAL, conflict=False)
        self._record_attempt(
            entry, attempt_number=attempt_no, action=action, before=before, after=FAILED_FINAL,
            actor=actor, result_status=RESULT_FINAL, note=f"{reason or RESULT_FINAL}，结果 {result['结果编号']}",
        )
        self._write_final(entry, result, attempt_no, reason or RESULT_FINAL)
        self._sync_execution(entry)
        self._refresh_job(entry)
        return entry, f"重试条目已{action}，最终失败清单已生成（{result['结果编号']}）"

    def _write_conflict_result(
        self,
        entry: dict[str, Any],
        attempt_number: int,
        action: str,
        actor: str,
    ) -> dict[str, Any]:
        """旧尝试的迟到结果：每个尝试最多保留一份冲突版本，重复到达不再追加。"""
        results = store.dataset(RETRY_RESULT_DATASET)
        entry_id = _as_int(entry.get("id"))
        existing = next(
            (
                row for row in results
                if _as_int(row.get("条目ID")) == entry_id
                and _as_int(row.get("尝试序号")) == attempt_number
                and row.get("冲突标志")
            ),
            None,
        )
        if existing is not None:
            return existing
        label = "重试成功" if action == "重试成功" else "最终失败"
        return self._write_result(
            entry, attempt_number, RESULT_CONFLICT, actor, _now(),
            f"迟到尝试 {attempt_number} 的「{label}」版本，当前状态为「{entry.get('status')}」，"
            f"与当前状态并存，不覆盖",
            conflict=True,
        )

    def _handle_late_result(
        self,
        entry: dict[str, Any],
        action: str,
        actor: str,
        attempt: int | None,
        result_id: str | None,
        current: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """终态之后到达的回调：完全相同的重复直接忽略，版本不同的迟到结果作为冲突版本保留。

        同一尝试、同一回调内容连续提交只生效一次（按回执内容判重）；
        旧尝试在退避上限之后返回、或终态已确认后到达的相反结果，保留为一份冲突版本，
        不会把状态来回改写，因此重试结果上最多只多出一份冲突版本。
        """
        entry_id = _as_int(entry.get("id"))
        requested_status = SUCCEEDED if action == "重试成功" else FAILED_FINAL
        attempt_no = _as_int(attempt, _as_int(entry.get("当前尝试"), 1))
        results = store.dataset(RETRY_RESULT_DATASET)
        live = [
            row for row in results
            if _as_int(row.get("条目ID")) == entry_id
            and _as_int(row.get("尝试序号")) == attempt_no
            and not row.get("冲突标志")
        ]
        effective = next((row for row in live if row.get("结果状态") == requested_status), None)
        if effective is not None and (result_id is None or effective.get("结果编号") == result_id):
            return entry, f"重复提交已忽略：结果 {effective['结果编号']} 已生效，未新增尝试或结果"

        existing_conflict = next(
            (row for row in results
             if _as_int(row.get("条目ID")) == entry_id
             and _as_int(row.get("尝试序号")) == attempt_no
             and row.get("冲突标志")),
            None,
        )
        if existing_conflict is not None:
            return entry, (
                f"重复的迟到回调已忽略：冲突版本 {existing_conflict['结果编号']} 已保留，"
                f"终态「{entry.get('status')}」未被覆盖"
            )

        conflict = self._write_conflict_result(entry, attempt_no, action, actor)
        self._record_attempt(
            entry, attempt_number=attempt_no, action=action, before=current, after=current,
            actor=actor, result_status=RESULT_CONFLICT,
            note=f"迟到回调已保留为冲突版本 {conflict['结果编号']}，终态未被覆盖",
        )
        return None, (
            f"重试条目已处于终态「{entry.get('status')}」，迟到结果已保留为冲突版本 "
            f"{conflict['结果编号']}，未覆盖当前状态"
        )

    # ------------------------------------------------------------------ 数据集
    def _record_attempt(
        self,
        entry: dict[str, Any],
        *,
        attempt_number: int,
        action: str,
        before: str,
        after: str,
        actor: str,
        result_status: str,
        note: str,
    ) -> dict[str, Any]:
        attempts = store.dataset(RETRY_ATTEMPT_DATASET)
        row = {
            "尝试编号": f"ATMP-{len(attempts) + 1:04d}",
            "条目ID": entry.get("id"),
            "条目编号": entry.get("条目编号"),
            "作业编号": entry.get("作业编号", ""),
            "失败URL": entry.get("失败URL"),
            "轮次": entry.get("轮次", 1),
            "尝试序号": attempt_number,
            "尝试版本": _as_int(entry.get("版本"), 0),
            "动作": action,
            "状态前": before,
            "状态后": after,
            "退避级别": entry.get("退避级别"),
            "已重试次数": entry.get("已重试次数"),
            "结果状态": result_status,
            "记录时间": _now(),
            "回执人": actor,
            "说明": note,
        }
        attempts.append(row)
        return row

    def _write_result(
        self,
        entry: dict[str, Any],
        attempt_number: int,
        status: str,
        actor: str,
        at: str,
        note: str,
        *,
        conflict: bool,
    ) -> dict[str, Any]:
        results = store.dataset(RETRY_RESULT_DATASET)
        entry_id = _as_int(entry.get("id"))
        # 同一尝试的正常结果只保留一份；迟到/冲突版本另起一行，保证两份版本都能查到。
        existing = next(
            (
                row for row in results
                if _as_int(row.get("条目ID")) == entry_id
                and _as_int(row.get("尝试序号")) == attempt_number
                and not row.get("冲突标志")
            ),
            None,
        )
        if existing is not None and not conflict:
            existing.update({
                "结果状态": status,
                "当前状态": entry.get("status"),
                "最终处置": note,
                "完成时间": at,
                "回执人": actor,
                "尝试版本": _as_int(entry.get("版本"), 0),
                "失败原因码": entry.get("失败原因码"),
                "首次失败时间": entry.get("首次失败时间"),
            })
            return existing
        row = {
            "结果编号": f"RSLT-{len(results) + 1:04d}",
            "条目ID": entry_id,
            "条目编号": entry.get("条目编号"),
            "作业编号": entry.get("作业编号", ""),
            "失败URL": entry.get("失败URL"),
            "轮次": entry.get("轮次", 1),
            "尝试序号": attempt_number,
            "尝试版本": _as_int(entry.get("版本"), 0),
            "结果状态": status,
            "当前状态": entry.get("status"),
            "冲突标志": bool(conflict),
            "冲突说明": note if conflict else "",
            "最终处置": note,
            "失败原因码": entry.get("失败原因码"),
            "首次失败时间": entry.get("首次失败时间"),
            "退避级别": entry.get("退避级别"),
            "已重试次数": entry.get("已重试次数"),
            "完成时间": at,
            "回执人": actor,
        }
        results.append(row)
        return row

    def _write_final(self, entry: dict[str, Any], result: dict[str, Any], attempt: int, reason: str) -> dict[str, Any]:
        finals = store.dataset(RETRY_FINAL_DATASET)
        url = str(entry.get("失败URL") or "")
        # 不变式：最终失败清单不能与（同一轮或更晚轮次的）成功结果同时包含同一 URL。
        if self._url_has_success(url, _as_int(entry.get("轮次"), 1)):
            finals[:] = [row for row in finals if str(row.get("失败URL")) != url]
            return {}
        existing = next((row for row in finals if _as_int(row.get("条目ID")) == _as_int(entry.get("id"))), None)
        payload = {
            "清单编号": existing.get("清单编号") if existing else f"FAIL-{len(finals) + 1:04d}",
            "条目ID": entry.get("id"),
            "条目编号": entry.get("条目编号"),
            "作业编号": entry.get("作业编号", ""),
            "失败URL": url,
            "失败原因码": entry.get("失败原因码"),
            "首次失败时间": entry.get("首次失败时间"),
            "已重试次数": entry.get("已重试次数"),
            "最终尝试序号": attempt,
            "最终处置": reason,
            "最终失败时间": entry.get("最终失败时间"),
            "关联结果编号": result.get("结果编号"),
        }
        if existing is None:
            finals.append(payload)
            return payload
        existing.update(payload)
        return existing

    def _drop_final(self, entry: dict[str, Any]) -> None:
        finals = store.dataset(RETRY_FINAL_DATASET)
        finals[:] = [
            row for row in finals
            if _as_int(row.get("条目ID")) != _as_int(entry.get("id"))
            and str(row.get("失败URL")) != str(entry.get("失败URL") or "")
        ]

    def _url_has_success(self, url: str, round_number: int) -> bool:
        """同一条目在同一轮次里成功过，就不再把它放进最终失败清单。"""
        return any(
            str(row.get("失败URL")) == url
            and row.get("结果状态") == RESULT_SUCCESS
            and not row.get("冲突标志")
            and _as_int(row.get("轮次"), 1) >= round_number
            for row in store.dataset(RETRY_RESULT_DATASET)
        )

    def _set_execution_status(self, url: str, status: str) -> None:
        """只更新网页执行侧该 URL 对应行的重试状态，不重建其它行。"""
        for row in store.rows(EXECUTION_MODULE):
            if str(row.get("目标URL") or "") == url:
                if row.get("重试状态") != status:
                    row["重试状态"] = status
                    row["同步版本"] = _as_int(row.get("同步版本"), 0) + 1

    # ------------------------------------------------------------------ 联动
    def _sync_execution(self, entry: dict[str, Any]) -> None:
        """把重试结果同步到网页执行侧的失败URL集合。

        等待重试、重试中、最终失败都算「仍在失败集合里」；只有重试成功才把该 URL
        从失败URL集合撤下，保证最终失败清单不会和成功结果同时包含同一 URL。
        """
        url = str(entry.get("失败URL") or "")
        if not url:
            return
        status = entry.get("status")
        rows = store.rows(EXECUTION_MODULE)
        matches = [row for row in rows if str(row.get("目标URL") or "") == url]

        if status == SUCCEEDED:
            for row in rows:
                collection = [item for item in (row.get("失败URL集合") or []) if item != url]
                if collection != list(row.get("失败URL集合") or []):
                    row["失败URL集合"] = collection
                    row["失败URL数"] = len(collection)
                    row["同步版本"] = _as_int(row.get("同步版本"), 0) + 1
            for row in matches:
                if row.get("重试状态") != RESULT_SUCCESS:
                    row["重试状态"] = RESULT_SUCCESS
                    row["同步版本"] = _as_int(row.get("同步版本"), 0) + 1
            return

        # 等待重试 / 重试中 / 最终失败：URL 都在失败集合里。
        target = matches[0] if matches else None
        if target is None:
            target = {
                "id": store.next_id(EXECUTION_MODULE),
                "status": "渲染中",
                "pending": status != FAILED_FINAL,
                "abnormal": status == FAILED_FINAL,
                "作业编号": str(entry.get("作业编号") or f"{entry.get('条目编号')}-EXEC"),
                "目标URL": url,
                "失败URL集合": [],
            }
            rows.append(target)
        for row in rows:
            if row is not target:
                row["失败URL集合"] = [item for item in (row.get("失败URL集合") or []) if item != url]
                row["失败URL数"] = len(row["失败URL集合"])
        collection = [item for item in (target.get("失败URL集合") or []) if item != url]
        collection.append(url)
        target["失败URL集合"] = collection
        target["失败URL数"] = len(collection)
        target["最近失败原因码"] = entry.get("失败原因码")
        if status == FAILED_FINAL:
            target["重试状态"] = RESULT_FINAL
            target["abnormal"] = True
        elif status == RETRYING:
            target["重试状态"] = "重试中"
        elif status == WAITING:
            target["重试状态"] = "等待重试"
        target["同步版本"] = _as_int(target.get("同步版本"), 0) + 1

    def _refresh_job(self, entry: dict[str, Any]) -> None:
        """把重试结果回写到抓取作业清单，保持作业侧与重试侧一致。"""
        job_code = str(entry.get("作业编号") or "").strip()
        if not job_code:
            return
        job = next((row for row in store.rows(JOB_MODULE) if str(row.get("作业编号")) == job_code), None)
        if job is None:
            return
        results = [
            row for row in store.dataset(RETRY_RESULT_DATASET)
            if str(row.get("作业编号")) == job_code and not row.get("冲突标志")
        ]
        finals = self.list_final_failures(job=job_code)
        job["重试结果数"] = len(results)
        job["成功URL数"] = sum(1 for row in results if row.get("结果状态") == RESULT_SUCCESS)
        job["失败URL数"] = len({row.get("失败URL") for row in finals})
        job["最近重试结果"] = results[-1].get("结果状态") if results else ""
        job["同步版本"] = _as_int(job.get("同步版本"), 0) + 1

    def resync_all(self) -> dict[str, int]:
        """结果记录/处理结果统一回写：按当前数据集重建网页执行侧失败URL集合与作业清单。"""
        with store.lock:
            for entry in store.rows(MODULE):
                self._sync_execution(entry)
                self._refresh_job(entry)
            return {
                "执行侧清单同步条目": len(store.rows(EXECUTION_MODULE)),
                "作业清单同步条目": len(store.rows(JOB_MODULE)),
                "最终失败清单数": len(store.dataset(RETRY_FINAL_DATASET)),
            }

    # ------------------------------------------------------------------ 队列执行
    def drain(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """并发执行重试队列：整段持锁推进，两个执行器也不会给同一条目加两次尝试。"""
        processed: list[dict[str, Any]] = []
        with store.lock:
            waiting = [
                row for row in store.rows(MODULE)
                if row.get("status") == WAITING
            ]
            waiting.sort(key=lambda item: (_as_int(item.get("退避级别"), 0), _as_int(item.get("id"), 0)))
            for row in waiting[:limit]:
                entry, message = self._transition(row, "发起重试", actor="重试执行器")
                if entry is not None:
                    processed.append({
                        "条目ID": entry.get("id"),
                        "条目编号": entry.get("条目编号"),
                        "尝试序号": entry.get("当前尝试"),
                        "status": entry.get("status"),
                        "message": message,
                    })
        return processed
