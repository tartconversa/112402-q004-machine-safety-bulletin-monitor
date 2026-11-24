"""持续监测调度器测试。"""
from _helpers import NOTICE_CSV_HEADER, DatabaseFixture
from robotnotice.collector import collect_source, register_source
from robotnotice.scheduler import Scheduler


class SchedulerTest(DatabaseFixture):
    def test_调度器按轮数执行并可停止(self):
        feed = self.write_feed(
            "feed.csv",
            NOTICE_CSV_HEADER + "N-1,M,RB-2,1,9,2.x,1,active\n",
        )
        register_source(self.db, "s1", "源", "file", str(feed), "csv_feed")
        sleeps: list[float] = []
        scheduler = Scheduler(
            self.db, self.store, interval_seconds=0.01,
            sleeper=sleeps.append,
        )
        reports = scheduler.run_forever(max_iterations=3)
        self.assertEqual(len(reports), 3)
        self.assertEqual(len(sleeps), 2)  # 最后一轮后不再睡眠

    def test_停止信号中断循环(self):
        feed = self.write_feed(
            "feed.csv",
            NOTICE_CSV_HEADER + "N-1,M,RB-2,1,9,2.x,1,active\n",
        )
        register_source(self.db, "s1", "源", "file", str(feed), "csv_feed")

        class StopAfterFirst:
            def __init__(self):
                self.calls = 0

            def __call__(self, _: float) -> None:
                self.calls += 1
                scheduler.stop()

        scheduler = Scheduler(
            self.db, self.store, interval_seconds=0.01,
            sleeper=StopAfterFirst(),
        )
        reports = scheduler.run_forever(max_iterations=10)
        self.assertEqual(len(reports), 1)

    def test_非法间隔被拒绝(self):
        with self.assertRaises(ValueError):
            Scheduler(self.db, self.store, interval_seconds=0)


if __name__ == "__main__":
    import unittest

    unittest.main()
