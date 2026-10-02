"""
timing.py — High-resolution monotonic stage timer.
Provides accurate, server-side latency breakdown using time.perf_counter().
"""

import time
from typing import Dict, Optional


class StageTimer:
    """
    Monotonic timer tracking stage durations and multi-stage breakdowns.
    """

    def __init__(self):
        self._start_time = time.perf_counter()
        self._end_time: Optional[float] = None
        self._stages: Dict[str, Dict[str, Optional[float]]] = {}

    def __enter__(self):
        self._start_time = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def stop(self) -> float:
        """Stops the overall timer and returns elapsed milliseconds."""
        if self._end_time is None:
            self._end_time = time.perf_counter()
        return self.elapsed_ms

    @property
    def elapsed_ms(self) -> float:
        """Total elapsed time in milliseconds."""
        current = self._end_time if self._end_time is not None else time.perf_counter()
        return round((current - self._start_time) * 1000, 3)

    def start_stage(self, stage_name: str) -> None:
        """Starts timing a named stage."""
        self._stages[stage_name] = {
            "start": time.perf_counter(),
            "end": None,
            "duration_ms": None,
        }

    def stop_stage(self, stage_name: str) -> float:
        """Stops timing a named stage and returns stage duration in milliseconds."""
        now = time.perf_counter()
        stage = self._stages.get(stage_name)
        if stage and stage["start"] is not None:
            stage["end"] = now
            duration = round((now - stage["start"]) * 1000, 3)
            stage["duration_ms"] = duration
            return duration
        return 0.0

    def get_stage_ms(self, stage_name: str) -> Optional[float]:
        """Returns duration in ms for a named stage if completed."""
        stage = self._stages.get(stage_name)
        if stage:
            if stage["duration_ms"] is not None:
                return stage["duration_ms"]
            elif stage["start"] is not None:
                return round((time.perf_counter() - stage["start"]) * 1000, 3)
        return None

    def get_breakdown(self) -> Dict[str, float]:
        """Returns a dictionary of all recorded stage durations in milliseconds."""
        breakdown = {}
        for name, data in self._stages.items():
            if data["duration_ms"] is not None:
                breakdown[f"{name}_ms"] = data["duration_ms"]
            elif data["start"] is not None:
                breakdown[f"{name}_ms"] = round((time.perf_counter() - data["start"]) * 1000, 3)
        breakdown["total_ms"] = self.elapsed_ms
        return breakdown
