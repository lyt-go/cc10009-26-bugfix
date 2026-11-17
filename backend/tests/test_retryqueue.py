"""重试队列回归测试：状态收敛、幂等提交、迟到回调、数据集与跨模块联动。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import threading
import unittest

from app.seed import SEED_DATASETS, SEED_ROWS
from app.store import store
from app.services.retryqueue import MAX_BACKOFF_LEVEL, RetryqueueService


class RetryqueueTestCase(unittest.TestCase):
    def setUp(self) -> None:
        store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
        store._datasets = {name: [dict(row) for row in rows] for name, rows in SEED_DATASETS.items()}
        self.service = RetryqueueService()

    def act(self, entry_id: int, action: str, **kwargs):
        return self.service.run_action(entry_id, action, **kwargs)

    def entry(self, entry_id: int) -> dict:
        return store.find("retryqueue", entry_id)

    def test_cannot_jump_to_terminal(self) -> None:
        # 草稿不能跨级进入最终失败，等待重试也不能直接成功。
        self.assertIsNone(self.act(1, "标记最终失败")[0])
        self.assertIsNone(self.act(1, "重试成功")[0])
        self.assertIsNone(self.act(2, "重试成功")[0])
        self.assertEqual(self.entry(1)["status"], "草稿")
        self.assertEqual(self.entry(2)["status"], "等待重试")

    def test_only_retrying_converges_and_success_reachable(self) -> None:
        self.assertIsNotNone(self.act(1, "加入队列")[0])
        self.assertIsNotNone(self.act(1, "发起重试")[0])
        self.assertEqual(self.entry(1)["status"], "重试中")
        entry, message = self.act(1, "重试成功")
        self.assertIsNotNone(entry)
        self.assertEqual(self.entry(1)["status"], "重试成功")
        self.assertIn("结果", message)

    def test_retrying_cannot_go_back_to_waiting(self) -> None:
        self.assertIsNone(self.act(3, "加入队列")[0])
        self.assertIsNone(self.act(3, "重新加入队列")[0])
        self.assertEqual(self.entry(3)["status"], "重试中")

    def test_fields_persisted(self) -> None:
        entry = self.entry(1)
        for field in ["失败URL", "失败原因码", "首次失败时间", "已重试次数", "下次重试时间", "退避级别"]:
            self.assertIn(field, entry)
        self.act(1, "加入队列")
        self.act(1, "发起重试")
        entry = self.entry(1)
        self.assertEqual(entry["已重试次数"], 1)
        self.assertEqual(entry["当前尝试"], 1)
        # 排队不算一次尝试，只有发起重试才增加计数。
        self.act(3, "标记最终失败")
        self.assertEqual(self.entry(3)["已重试次数"], 1)

    def test_duplicate_success_only_once(self) -> None:
        self.act(1, "加入队列")
        self.act(1, "发起重试")
        self.act(1, "重试成功")
        attempts_before = len(self.service.attempts_of(1))
        results_before = len(self.service.results_of(1))
        self.act(1, "重试成功")
        self.act(1, "重试成功")
        self.assertEqual(len(self.service.attempts_of(1)), attempts_before)
        self.assertEqual(len(self.service.results_of(1)), results_before)
        self.assertEqual(self.entry(1)["status"], "重试成功")

    def test_late_success_cannot_override_final(self) -> None:
        self.act(3, "标记最终失败")
        self.assertEqual(self.entry(3)["status"], "最终失败")
        entry, _ = self.act(3, "重试成功", attempt=1)
        self.assertIsNone(entry)
        self.assertEqual(self.entry(3)["status"], "最终失败")
        results = self.service.results_of(3)
        self.assertEqual(sum(1 for row in results if row["结果状态"] == "最终失败" and not row["冲突标志"]), 1)
        self.assertEqual(sum(1 for row in results if row["结果状态"] == "版本冲突"), 1)
        # 重复的迟到回调不再追加冲突版本。
        self.act(3, "重试成功", attempt=1)
        self.assertEqual(len(self.service.results_of(3)), len(results))

    def test_stale_attempt_keeps_two_versions(self) -> None:
        entry = self.entry(3)
        self.act(3, "重试成功", attempt=0)
        self.assertEqual(entry["status"], "重试中")
        results = self.service.results_of(3)
        # 当前进行中版本 + 旧尝试的冲突版本各一份。
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(1 for row in results if row["冲突标志"]), 1)
        self.act(3, "重试成功", attempt=0)
        self.assertEqual(len(self.service.results_of(3)), 2)

    def test_concurrent_callbacks_single_terminal(self) -> None:
        outcomes = []

        def call(action: str) -> None:
            outcomes.append(self.act(3, action))

        threads = [threading.Thread(target=call, args=(action,))
                   for action in ["重试成功", "标记最终失败", "重试成功", "标记最终失败"]]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        status = self.entry(3)["status"]
        self.assertIn(status, {"重试成功", "最终失败"})
        results = self.service.results_of(3)
        effective = [row for row in results
                     if row["结果状态"] in {"重试成功", "最终失败"} and not row["冲突标志"]]
        self.assertEqual(len(effective), 1)
        self.assertEqual(effective[0]["结果状态"], status)

    def test_concurrent_drain_single_version(self) -> None:
        counters = []

        def drain() -> None:
            counters.append(len(self.service.drain()))

        threads = [threading.Thread(target=drain) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # 只有一条等待重试条目，整段持锁推进，所以只会被推进一次。
        self.assertEqual(sum(counters), 1)
        self.assertEqual(self.entry(2)["已重试次数"], 3)

    def test_backoff_cap_converges_without_skipping(self) -> None:
        entry, missing = self.service.create_entry({
            "条目编号": "RETR-9000", "失败URL": "https://cap.example/1",
            "失败原因码": "TIMEOUT", "退避级别": MAX_BACKOFF_LEVEL,
        })
        self.assertEqual(missing, [])
        assert entry is not None
        self.act(int(entry["id"]), "加入队列")
        self.act(int(entry["id"]), "发起重试")
        stored = self.entry(int(entry["id"]))
        self.assertEqual(stored["status"], "最终失败")
        actions = [row["动作"] for row in self.service.attempts_of(int(entry["id"]))]
        self.assertEqual(actions, ["加入队列", "发起重试", "标记最终失败"])
        self.assertTrue(any(row["失败URL"] == "https://cap.example/1"
                            for row in self.service.list_final_failures()))

    def test_final_and_success_mutually_exclusive(self) -> None:
        self.act(5, "重新加入队列")
        self.act(5, "发起重试")
        self.act(5, "重试成功")
        final_urls = {row["失败URL"] for row in self.service.list_final_failures()}
        success_urls = {row["失败URL"] for row in self.service.list_results(status="重试成功")}
        self.assertEqual(final_urls & success_urls, set())
        self.assertNotIn("https://shop.example.com/item/9003", final_urls)

    def test_execution_side_failure_urls(self) -> None:
        self.act(4, "重新加入队列")
        self.act(4, "发起重试")
        self.act(4, "标记最终失败")
        rows = [row for row in store.rows("rendertask")]
        self.assertTrue(any("https://shop.example.com/item/9002" in (row.get("失败URL集合") or []) for row in rows))
        self.act(4, "重新加入队列")
        self.act(4, "发起重试")
        self.act(4, "重试成功")
        self.assertFalse(any("https://shop.example.com/item/9002" in (row.get("失败URL集合") or []) for row in rows))

    def test_resync_writes_job_rollup(self) -> None:
        self.service.resync_all()
        job = next(row for row in store.rows("crawljob") if row["作业编号"] == "CRAW-0002")
        self.assertIn("重试结果数", job)
        self.assertIn("最近重试结果", job)

    def test_bulk_returns_per_item_outcome(self) -> None:
        results = self.service.bulk_enqueue(["https://x.example/1", "https://x.example/2", ""], job="CRAW-0002")
        self.assertEqual(len(results), 3)
        self.assertTrue(results[0]["ok"])
        self.assertTrue(results[1]["ok"])
        self.assertFalse(results[2]["ok"])
        self.assertIn("尝试次数", results[0])
        self.assertIn("最终处置", results[0])

    def test_rejoin_keeps_history(self) -> None:
        before = len(self.service.attempts_of(5))
        self.act(5, "重新加入队列")
        self.act(5, "发起重试")
        self.assertGreater(len(self.service.attempts_of(5)), before)
        self.assertEqual(self.entry(5)["已重试次数"], 4)


if __name__ == "__main__":
    unittest.main()
