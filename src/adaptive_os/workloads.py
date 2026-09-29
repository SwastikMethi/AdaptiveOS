"""Workload profile and JSONL contract shared by telemetry and orchestration."""

from dataclasses import dataclass
import json
import math
import re

WORKLOAD_IMAGE = "adaptive-os-workloads:dev"


@dataclass(frozen=True)
class WorkloadProfile:
    name: str
    period_ms: float
    work_budget_ms: float

    def __post_init__(self):
        if not self.name or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                                for value in (self.period_ms, self.work_budget_ms)):
            raise ValueError("profile requires a name and finite positive period/budget")
        if self.work_budget_ms >= self.period_ms:
            raise ValueError("work budget must be below the period for a valid solo baseline")


def parse_workload_line(line: str) -> dict:
    event = json.loads(line)
    if not isinstance(event, dict) or type(event.get("schema_version")) is not int or event["schema_version"] != 1:
        raise ValueError("expected a schema_version=1 workload object")
    kind = event.get("type")
    if kind not in ("foreground_tick", "foreground_summary", "background_summary"):
        raise ValueError("unknown workload event type")
    fields = ["sequence", "start_ns", "monotonic_ns", "process_cpu_ns", "work_units"]
    fields += (["scheduled_ns", "wakeup_ns", "completion_ns", "deadline_ns"] if kind == "foreground_tick"
               else ["interval_start_ns", "interval_ns"])
    for field in fields:
        if type(event.get(field)) is not int or event[field] < 0:
            raise ValueError(f"{field} must be a nonnegative integer")
    if not isinstance(event.get("checksum"), str) or not re.fullmatch("[0-9a-f]{64}", event["checksum"]):
        raise ValueError("missing or invalid checksum")
    if event["monotonic_ns"] < event["start_ns"]:
        raise ValueError("event precedes workload start")
    if kind == "foreground_tick":
        if not (event["start_ns"] <= event["scheduled_ns"] <= event["wakeup_ns"] <= event["completion_ns"]
                == event["monotonic_ns"] and event["deadline_ns"] > event["scheduled_ns"]):
            raise ValueError("causally impossible foreground timestamps")
        if (type(event.get("deadline_met")) is not bool or type(event.get("skipped")) is not bool
                or event["deadline_met"] != (event["completion_ns"] <= event["deadline_ns"])):
            raise ValueError("deadline flag does not match timestamps")
    elif (event["interval_ns"] <= 0 or event["interval_start_ns"] < event["start_ns"]
          or event["monotonic_ns"] - event["interval_start_ns"] != event["interval_ns"]):
        raise ValueError("invalid summary interval")
    return event
