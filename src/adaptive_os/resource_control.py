"""Deterministic selection and bounds for application-managed allocations."""
from dataclasses import dataclass, replace
from .cpu_rule import CpuDecision, RuleState


@dataclass(frozen=True)
class ResourceTask:
    workload_id: str
    importance: str
    identity: int
    original_allocation: int
    current_allocation: int
    intervention_order: int | None = None
    cpu_pct: float | None = None
    original_nice: int = 0
    current_nice: int = 0


@dataclass(frozen=True)
class AllocationDecision(CpuDecision):
    resource: str = 'memory'
    changes: tuple = ()


def allocation_decision(action, tasks, foreground, resource):
    step, minimum, maximum = (16, 16, 512) if resource == 'memory' else (500, 500, 8000)
    target = None
    if action == 'favor' and foreground.current_allocation + step <= maximum:
        eligible = [t for t in tasks if t.current_allocation - step >= minimum]
        if eligible:
            target = min(eligible, key=lambda t: (('Low', 'Normal', 'High').index(t.importance),
                                                  -t.current_allocation, t.workload_id))
    elif action == 'restore':
        eligible = [t for t in tasks if t.current_allocation < t.original_allocation]
        if eligible and foreground.current_allocation - step >= minimum:
            target = min(eligible, key=lambda t: (('High', 'Normal', 'Low').index(t.importance),
                                                  -(t.intervention_order or 0), t.workload_id))
            step = -step
    if target is None:
        return AllocationDecision('hold', reason='hold' if action == 'hold' else 'control limit reached', resource=resource)
    changes = tuple({'workload_id': t.workload_id, 'identity': t.identity,
                     'previous': t.current_allocation, 'requested': t.current_allocation + delta}
                    for t, delta in ((target, -step), (foreground, step)))
    return AllocationDecision(action, target.workload_id, reason='bounded allocation transfer',
                              resource=resource, changes=changes)


def evaluate_resource_rule(window, tasks, state, foreground, resource):
    hold = AllocationDecision('hold', resource=resource)
    if state.disabled or (state.last_window_end is not None and window.end <= state.last_window_end):
        return hold, state
    state = replace(state, last_window_end=window.end)
    if not window.valid or window.miss_rate is None:
        state = replace(state, invalid_windows=state.invalid_windows + 1, overload_windows=0, recovery_windows=0)
        return (AllocationDecision('restore_all', reason='two invalid decision windows', resource=resource),
                replace(state, disabled=True)) if state.invalid_windows >= 2 else (hold, state)
    state = replace(state, valid_seconds=state.valid_seconds + window.end - window.start, invalid_windows=0,
        overload_windows=state.overload_windows + 1 if window.miss_rate > .02 else 0,
        recovery_windows=state.recovery_windows + 1 if window.miss_rate < .005 else 0)
    if state.valid_seconds < 10:
        return replace(hold, reason='initial observation'), state
    if state.last_change is not None and window.end - state.last_change < 30:
        return replace(hold, reason='cooldown'), state
    action = 'favor' if state.overload_windows >= 2 else 'restore' if state.recovery_windows >= 6 else 'hold'
    return allocation_decision(action, tasks, foreground, resource), state
