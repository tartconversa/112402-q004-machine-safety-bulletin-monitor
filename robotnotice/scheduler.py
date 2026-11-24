"""持续监测调度器：按固定间隔轮询全部启用源。

设计为可在独立进程中运行；每轮采集后返回报告，调用方可决定是否
紧接着执行适用范围同步（采集与处置保持解耦，默认只采集不派单）。
"""
from __future__ import annotations

import time
from collections.abc import Callable

from .collector import CollectionReport, collect_all
from .evidence import EvidenceStore
from .storage import Database


class Scheduler:
    def __init__(
        self,
        db: Database,
        store: EvidenceStore,
        interval_seconds: float,
        sleeper: Callable[[float], None] = time.sleep,
        actor: str = "scheduler",
    ):
        if interval_seconds <= 0:
            raise ValueError("轮询间隔必须为正数")
        self.db = db
        self.store = store
        self.interval_seconds = interval_seconds
        self.sleeper = sleeper
        self.actor = actor
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run_once(self) -> list[CollectionReport]:
        return collect_all(self.db, self.store, self.actor)

    def run_forever(self, max_iterations: int | None = None) -> list[CollectionReport]:
        """循环轮询；测试时可用 max_iterations 限定轮数。"""
        all_reports: list[CollectionReport] = []
        iterations = 0
        while not self._stopped:
            reports = self.run_once()
            all_reports.extend(reports)
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
            if not self._stopped:
                self.sleeper(self.interval_seconds)
        return all_reports
