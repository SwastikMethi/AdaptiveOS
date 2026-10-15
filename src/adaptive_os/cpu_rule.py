"""Pure five-second CPU control rule."""

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class CpuWindow:
    start: float
    end: float
    miss_rate: float | None
    background_cpu: float | None
    valid: bool


@dataclass(frozen=True)
class ManagedTask:
    workload_id: str
    importance: str
    cpu_pct: float
    original_nice: int
    current_nice: int
    identity: int
    intervention_order: int | None = None


@dataclass(frozen=True)
class CpuDecision:
    action: str
    target: str | None = None
    nice: int | None = None
    reason: str = 'hold'


@dataclass(frozen=True)
class RuleState:
    last_window_end: float | None = None
    valid_seconds: float = 0
    overload_windows: int = 0
    recovery_windows: int = 0
    invalid_windows: int = 0
    last_change: float | None = None
    disabled: bool = False


def acknowledge(state, changed_at):
    return replace(state, last_change=changed_at, overload_windows=0, recovery_windows=0)


def evaluate_rule(window, tasks, state):
    if state.disabled or (state.last_window_end is not None and window.end <= state.last_window_end):
        return CpuDecision('hold', reason='disabled' if state.disabled else 'duplicate or old window'), state
    state = replace(state, last_window_end=window.end)
    if not window.valid or window.miss_rate is None or window.background_cpu is None:
        state = replace(state, invalid_windows=state.invalid_windows + 1,
                        overload_windows=0, recovery_windows=0)
        if state.invalid_windows >= 2:
            return CpuDecision('restore_all', reason='two invalid decision windows'), replace(state, disabled=True)
        return CpuDecision('hold', reason='invalid decision window'), state

    duration = max(0, window.end - window.start)
    overload = window.miss_rate > .02 and window.background_cpu >= 20
    recovery = window.miss_rate < .005
    state = replace(state, valid_seconds=state.valid_seconds + duration, invalid_windows=0,
                    overload_windows=state.overload_windows + 1 if overload else 0,
                    recovery_windows=state.recovery_windows + 1 if recovery else 0)
    if not overload and not recovery:
        state = replace(state, overload_windows=0, recovery_windows=0)
        return CpuDecision('hold', reason='between thresholds'), state
    if state.valid_seconds < 10:
        return CpuDecision('hold', reason='initial observation'), state
    if state.last_change is not None and window.end - state.last_change < 30:
        return CpuDecision('hold', reason='cooldown'), state

    if state.overload_windows >= 2:
        next_nice = {0: 10, 10: 19}
        eligible = [item for item in tasks if item.current_nice == item.original_nice
                    and item.original_nice in next_nice]
        if not eligible:
            return CpuDecision('hold', reason='control limit reached'), state
        rank = {'Low': 0, 'Normal': 1, 'High': 2}
        target = min(eligible, key=lambda item: (rank[item.importance], -item.cpu_pct, item.workload_id))
        return CpuDecision('lower', target.workload_id, next_nice[target.original_nice],
                           f'{target.importance} importance under contention'), state

    if state.recovery_windows >= 3:
        changed = [item for item in tasks if item.current_nice != item.original_nice]
        if not changed:
            return CpuDecision('hold', reason='all priorities restored'), state
        rank = {'High': 0, 'Normal': 1, 'Low': 2}
        target = min(changed, key=lambda item: (rank[item.importance],
                     -(item.intervention_order or 0), item.workload_id))
        return CpuDecision('restore', target.workload_id, target.original_nice,
                           f'{target.importance} importance recovery'), state
    return CpuDecision('hold', reason='waiting for consecutive windows'), state
