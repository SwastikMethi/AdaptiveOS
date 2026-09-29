"""Metrics use VM event timestamps; freshness uses only host receipt timestamps."""

from dataclasses import dataclass
import math
import re


def nearest_rank_percentile(values, fraction=0.95):
    if not values or not 0 < fraction <= 1:
        raise ValueError("percentile requires values and 0 < fraction <= 1")
    return sorted(values)[math.ceil(fraction * len(values)) - 1]


def completion_lateness_ms(event):
    return max(0, event["completion_ns"] - event["deadline_ns"]) / 1_000_000


def deadline_miss_rate(deadline_met):
    if not deadline_met:
        raise ValueError("miss rate requires at least one tick")
    return sum(not met for met in deadline_met) / len(deadline_met)


def parse_psi_total(raw):
    match = re.search(r"^some .*\btotal=(\d+)\s*$", raw, re.MULTILINE)
    if not match:
        raise ValueError("missing CPU PSI some total")
    return int(match[1])


@dataclass(frozen=True)
class WindowState:
    foreground_p95_completion_lateness_ms: float
    foreground_deadline_miss_rate: float
    foreground_p95_wakeup_delay_ms: float
    background_work_units_per_second: float
    foreground_cpu_percent: float
    background_cpu_percent: float
    cpu_pressure_some_delta: int | None
    current_mode: str
    elapsed_since_last_change_sec: float
    data_age_sec: float
    valid: bool
    invalid_reasons: tuple[str, ...]


@dataclass(frozen=True)
class Sample:
    start_ns: int
    end_ns: int
    state: WindowState


class WindowAccumulator:
    def __init__(self, period_ns: int, *, background_required: bool = True):
        if type(period_ns) is not int or period_ns <= 0:
            raise ValueError("period must be positive integer nanoseconds")
        self.period_ns = period_ns
        self.background_required = background_required
        self.events = {kind: [] for kind in ("foreground_tick", "foreground_summary", "background_summary")}
        self.received = {}

    def add(self, event: dict, *, received_ns: int) -> bool:
        kind = event["type"]
        stream = self.events[kind]
        if stream:
            previous = stream[-1]
            if event["sequence"] <= previous["sequence"] or event["monotonic_ns"] < previous["monotonic_ns"]:
                return False
            if event["process_cpu_ns"] < previous["process_cpu_ns"] or event["start_ns"] != previous["start_ns"]:
                raise ValueError("CPU counter or workload clock moved backwards")
        stream.append(event)
        self.received[kind] = received_ns
        return True

    def ticks(self, start_ns, end_ns):
        return [event for event in self.events["foreground_tick"] if start_ns <= event["scheduled_ns"] < end_ns]

    def _totals(self, kind, start_ns, end_ns):
        cpu, units, covered = 0.0, 0.0, 0
        previous_cpu, previous_end, previous_seq = 0, None, -1
        for event in self.events[kind]:
            start, end = event["interval_start_ns"], event["monotonic_ns"]
            overlap = max(0, min(end_ns, end) - max(start_ns, start))
            contiguous = event["sequence"] == previous_seq + 1 and (previous_end is None or previous_end == start)
            if contiguous and overlap:
                # Boundary summaries are prorated; the raw counters are also retained.
                fraction = overlap / event["interval_ns"]
                cpu += (event["process_cpu_ns"] - previous_cpu) * fraction
                units += event["work_units"] * fraction
                covered += overlap
            previous_cpu, previous_end, previous_seq = event["process_cpu_ns"], end, event["sequence"]
        return cpu, units, covered

    def build_window(self, start_ns, end_ns, *, now_ns, current_mode="balanced",
                     elapsed_since_last_change_sec=0.0, cpu_pressure_some_delta=None,
                     window_end_host_ns=None) -> WindowState:
        duration = end_ns - start_ns
        if duration <= 0 or duration % self.period_ns:
            raise ValueError("window must contain a positive whole number of periods")
        ticks = self.ticks(start_ns, end_ns)
        reasons = []
        expected = list(range(start_ns, end_ns, self.period_ns))
        if [tick["scheduled_ns"] for tick in ticks] != expected:
            reasons.append("missing foreground ticks")
        fg_cpu, _, fg_covered = self._totals("foreground_summary", start_ns, end_ns)
        bg_cpu, bg_work, bg_covered = self._totals("background_summary", start_ns, end_ns)
        if fg_covered != duration:
            reasons.append("incomplete foreground CPU coverage")
        if self.background_required and bg_covered != duration:
            reasons.append("incomplete background coverage")
        required = ["foreground_tick", "foreground_summary"]
        if self.background_required:
            required.append("background_summary")
        age = max(max(0, (now_ns - self.received[kind]) / 1_000_000_000) if kind in self.received else 3.0
                  for kind in required)
        if window_end_host_ns is not None:
            processing_age = max(0, (now_ns - window_end_host_ns) / 1_000_000_000)
            if processing_age > 2:
                reasons.append("window processed too late")
            age = max(age, processing_age)
        if age > 2:
            reasons.append("stale telemetry")
        if cpu_pressure_some_delta is not None and cpu_pressure_some_delta < 0:
            reasons.append("CPU pressure counter moved backwards")
        return WindowState(
            nearest_rank_percentile([completion_lateness_ms(tick) for tick in ticks]) if ticks else 0.0,
            deadline_miss_rate([tick["deadline_met"] for tick in ticks]) if ticks else 0.0,
            nearest_rank_percentile([(tick["wakeup_ns"] - tick["scheduled_ns"]) / 1_000_000 for tick in ticks]) if ticks else 0.0,
            bg_work * 1_000_000_000 / duration, fg_cpu * 100 / duration, bg_cpu * 100 / duration,
            cpu_pressure_some_delta, current_mode, elapsed_since_last_change_sec, age, not reasons, tuple(reasons))
