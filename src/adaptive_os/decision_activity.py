"""Read-only presentation of decision evidence. This module never schedules control."""
from collections import OrderedDict
from dataclasses import dataclass, field
import json
import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)


REASONS = {
    'helper_unavailable': 'Local model helper is not ready',
    'helper_busy': 'Previous model request has not finished',
    'helper_failed': 'Model helper failed',
    'dispatch_failed': 'Could not dispatch the request',
    'deadline': 'One-second response deadline exceeded',
    'authentication': 'Provider authentication failed',
    'rate_limit': 'Provider rate limit / backoff',
    'unavailable': 'Provider unavailable',
    'provider_failed': 'Provider returned an error',
    'invalid_proposal': 'Response failed validation',
    'cancelled': 'Cancelled when policy stopped',
    'superseded': 'Replaced by a newer measurement window',
    'late_response': 'Late response discarded',
}


def number(value, suffix='', digits=1):
    return f'{value:.{digits}f}{suffix}' if type(value) in (int, float) and math.isfinite(value) else 'Unknown'


def elapsed(seconds):
    if seconds is None or not math.isfinite(seconds):
        return '—'
    return f'{max(0, int(seconds)) // 60:02d}:{max(0, int(seconds)) % 60:02d}'


@dataclass
class DecisionEntry:
    cycle_id: str
    events: list = field(default_factory=list)

    def add(self, event):
        if event not in self.events:
            self.events.append(event)

    def last(self, kind):
        return next((e for e in reversed(self.events) if e.get('kind') == kind), {})

    @property
    def acknowledgement(self):
        command = self.last('decision_command') or self.last('priority_requested')
        if not command:
            return {}
        for event in reversed(self.events):
            if event.get('request_id') != command.get('request_id'):
                continue
            if event.get('kind') == 'priority_outcome':
                return event if event.get('confirmed') else {}
            if event.get('kind') == 'decision_acknowledgement':
                return event
        return {}

    @property
    def acknowledged_at(self):
        return self.acknowledgement.get('acknowledged_at')

    def columns(self, origin_ns, names):
        legacy = self.last('cpu_decision')
        meta = self.last('rule_reference') or legacy or (self.events[0] if self.events else {})
        call = self.last('model_proposal')
        proposal = call.get('proposal', {})
        result = self.last('decision_result')
        manual = self.last('priority_requested')
        command = self.last('decision_command') or manual
        decision = result.get('decision') or call.get('guarded_decision') or meta.get('decision', {}) or legacy
        action = proposal.get('action') or decision.get('action')
        provider = meta.get('requested', manual.get('source', 'rule' if legacy else 'unknown')).title()
        resource = meta.get('resource', manual.get('resource', 'cpu'))
        fallback = self.last('policy_fallback')
        if fallback:
            provider += ' → Rule'
        elif meta.get('effective') == 'rule' and meta.get('requested') not in (None, 'rule'):
            provider = 'Rule fallback'
        if not proposal and result.get('source') == 'rule' and fallback:
            action = 'Rule: ' + (action or 'hold')
        if manual.get('source') in ('manual', 'restoration'):
            action = 'Restore' if manual['source'] == 'restoration' else 'Set priority'
        rejection = next((e for e in reversed(self.events) if e.get('kind') == 'model_rejection'
                          and e.get('reason') not in ('late_response', 'stale_request', 'late_helper_failed')), {})
        ack = self.acknowledgement
        if ack:
            change = ack.get('decision', {})
            target = change.get('target') or manual.get('workload_id')
            nice = change.get('nice', manual.get('nice'))
            old = manual.get('before_nice')
            transition = f'{old} → {nice}' if old is not None else f'→ {nice}'
            outcome = f'{names.get(target, target or "Background")[:32]}: nice {transition} confirmed'
            if resource != 'cpu':
                changes = change.get('changes', manual.get('changes', ack.get('changes', [])))
                divisor, unit = (100, '%') if resource == 'network' else (1, ' MiB')
                outcome = '; '.join(f'{names.get(c["workload_id"], "Workload")[:24]}: '
                    f'{c["previous"] / divisor:g} → {c["requested"] / divisor:g}{unit}' for c in changes) + ' confirmed'
        elif self.last('priority_outcome'):
            outcome = 'Change failed: ' + self.last('priority_outcome').get('reason', 'not confirmed')
        elif meta.get('shadow'):
            outcome = 'Shadow only · no priority changes'
        elif command:
            outcome = 'Queued for confirmation' if manual.get('queued') and not self.last('priority_dispatched') else 'Awaiting confirmation'
        elif proposal and decision.get('action') != proposal.get('action'):
            outcome = 'Blocked: ' + decision.get('reason', 'controller guard')
        elif decision.get('action') == 'restore_all':
            outcome = 'Restoration required · see restoration entries'
        elif decision.get('action') in ('lower', 'favor', 'restore'):
            outcome = 'Command not sent'
        elif rejection and not self.last('model_dispatched') and not proposal:
            outcome = 'Request not sent · ' + REASONS.get(rejection.get('reason'), rejection.get('reason', 'unavailable'))
        elif result or proposal or legacy or (meta.get('requested') == 'rule' and decision):
            outcome = 'No priority change'
            if rejection:
                outcome += ' · ' + REASONS.get(rejection.get('reason'), rejection.get('reason', ''))
        elif rejection:
            outcome = REASONS.get(rejection.get('reason'), rejection.get('reason', 'Rejected'))
        else:
            outcome = 'Request in progress' if self.last('model_dispatched') else 'Preparing decision'
        at = next((e.get('sandbox_estimate_ns') for e in self.events if e.get('sandbox_estimate_ns') is not None), None)
        if at is None:
            end = meta.get('window', {}).get('end')
            at = end * 1e9 if end is not None else legacy.get('window_end_ns')
        stamp = elapsed((at - origin_ns) / 1e9) if at is not None and origin_ns is not None else '—'
        latency = number(call.get('latency_ms'), ' ms', 0) if call else '—'
        if resource != 'cpu':
            provider = resource.title() + ' · ' + provider
            outcome = outcome.replace('priority changes', 'allocation changes').replace('priority change', 'allocation change')
            if manual:
                action = 'Favor foreground' if manual.get('action') == 'favor' else 'Restore'
            elif action == 'favor':
                action = 'Favor foreground'
        return [stamp, provider, (action or '—').capitalize(), outcome, latency]

    def details(self, origin_ns, names):
        context_event = self.last('decision_context')
        context = context_event.get('context', {})
        reference = self.last('rule_reference') or self.last('cpu_decision')
        local_rule = not context_event and (reference.get('requested') == 'rule' or
                     reference.get('effective') == 'rule' or reference.get('kind') == 'cpu_decision')
        if not context:
            measured = reference.get('window', reference)
            context = {'window': {'miss_rate': measured.get('miss_rate'),
                'p95_lateness_ms': reference.get('foreground_p95_ms'), 'valid': measured.get('valid')},
                'backgrounds': [dict(label=names.get(t['workload_id'], t['workload_id']),
                    importance=t['importance'], cpu_pct=t.get('cpu_pct'),
                    work_per_second=reference.get('background_work', {}).get(t['workload_id']),
                    original_nice=t['original_nice'], current_nice=t['current_nice'])
                    for t in reference.get('tasks', [])]}
        window = context.get('window', {})
        call = self.last('model_proposal')
        proposal = call.get('proposal', {})
        dispatch = self.last('model_dispatched')
        rejected = [e for e in self.events if e.get('kind') == 'model_rejection']
        state = ('No model call — Rule evaluated locally' if local_rule else
                 'Dispatched to helper (does not prove a network call)' if dispatch else
                 'Dispatch not recorded in this older recording' if call else 'Request not sent')
        lines = [f'Call: {state}',
                 f'Measurement window: {context_event.get("decision_window", self.last("rule_reference").get("decision_window", "Unknown"))}',
                 f'Foreground: misses {number(window.get("miss_rate") * 100 if window.get("miss_rate") is not None else None, "%")} · '
                 f'p95 lateness {number(window.get("p95_lateness_ms"), " ms")} · valid measurements: {window.get("valid", "Unknown")}']
        resource = context.get('resource', reference.get('resource', self.last('priority_requested').get('resource', 'cpu')))
        result = self.last('decision_result')
        lines.append('Guard result: ' + str(result.get('guard_result', 'Not recorded')))
        lines.append('Next decision window ends at: ' + number(result.get('next_window_end'), ' s', 3))
        if resource != 'cpu':
            lines.insert(0, f'Resource: {resource.title()} · application-managed allocations · CPU priorities fixed')
            changes = (self.last('decision_command').get('decision', {}).get('changes') or
                       self.last('priority_requested').get('changes', []))
            lines.append('Previous / requested allocation: ' + json.dumps(changes))
            lines.append('Confirmed allocation: ' + (json.dumps(changes) if self.acknowledgement else 'Not confirmed'))
            effect = self.last('resource_effect')
            lines.append('Measured effects (observational): ' +
                         (json.dumps({'before': effect.get('before'), 'after': effect.get('after')}) if effect
                          else 'Waiting for a complete window after acknowledgement'))
        processes = context_event.get('binding', {}).get('processes', [])
        for i, bg in enumerate(context.get('backgrounds', [])):
            key = processes[i][0] if i < len(processes) else None
            lines.append(f'{bg.get("label", "Background")} / {names.get(key, key or "anonymous background")}: '
                         f'CPU {number(bg.get("cpu_pct"), "%")} · work {number(bg.get("work_per_second"), "/s")} · '
                         f'importance {bg.get("importance", "Unknown")} · nice original/current '
                         f'{bg.get("original_nice", "?")}/{bg.get("current_nice", "?")}')
        lines += ['Available actions: ' + (', '.join(context['available_actions']) if 'available_actions' in context else 'Not recorded'),
                  'Recent windows: ' + (json.dumps(context['trend'], ensure_ascii=False) if 'trend' in context else 'Not recorded'),
                  f'Response: {proposal.get("action", "No usable model response")} · model {proposal.get("model", "Unknown")} · '
                  f'device {context_event.get("device", "Unknown")} · input tokens {proposal.get("input_tokens", "Unknown")}',
                  'Probabilities: ' + json.dumps(proposal.get('probabilities', {})) +
                  f' · model confidence {number(proposal.get("confidence"), digits=3)} (not decision accuracy)',
                  f'End-to-end response/validation: {number(call.get("latency_ms"), " ms")} · '
                  f'provider execution (may include network): {number(proposal.get("inference_ms"), " ms")}',
                  'Controller result: ' + json.dumps(result.get('result', call.get('guarded_decision', {}))),
                  'Reference Rule proposal (not proof of an applied change): ' +
                  json.dumps(reference.get('decision', reference if reference.get('kind') == 'cpu_decision' else {}))]
        for e in rejected:
            lines.append(f'Rejection: {REASONS.get(e.get("reason"), e.get("reason", "Unknown"))} [{e.get("reason")}] · '
                         f'Failure detection / late arrival: {number(e.get("latency_ms"), " ms")}')
        for kind, label in (('decision_context', 'Request prepared'), ('model_dispatched', 'Helper dispatch'),
                            ('model_proposal', 'Response validated'), ('decision_command', 'Command sent'),
                            ('priority_requested', 'Priority request'), ('priority_dispatched', 'Queued command sent'),
                            ('decision_acknowledgement', 'Acknowledged'), ('priority_outcome', 'Control outcome')):
            e = self.last(kind)
            if e:
                lines.append(f'{label}: host monotonic ns {e.get("host_monotonic_ns", "Unknown")} · '
                             f'request {e.get("request_id", "Unknown")}')
        if self.acknowledgement:
            lines.append('Confirmed change: ' + self.columns(origin_ns, names)[3])
            lines.append(f'Linux acknowledgement time: {number(self.acknowledged_at, " s", 6)}')
        if self.last('priority_outcome').get('reason'):
            lines.append('Control failure: ' + self.last('priority_outcome')['reason'])
        lines.append('Model rationale: not supplied by this typed-choice interface. Measured effects are observational.')
        return '\n'.join(lines)


def policy_status(policy, *, now, selected, running, history, anchor=None,
                  telemetry_receipt=None, telemetry_stale=False):
    view = dict(active='Manual', next_call='No scheduled model calls', next_check='Policy disabled',
                cooldown='No automatic changes', explanation='')
    if history:
        return dict(view, active='Recorded session · read-only', next_call='Recorded calls',
                    next_check='No live countdown', cooldown='Recorded changes only')
    if not running or not policy.active:
        return dict(view, next_check='Stopped' if not running else 'Policy disabled')
    provider = policy.requested.title()
    latched = policy.effective == 'rule' and policy.requested != 'rule'
    fallback = bool(policy.fallback_reason)
    view['active'] = 'Shadow only · no priority changes' if policy.shadow else (
        'Rule fallback' if fallback or latched else policy.effective.title())
    if fallback:
        view['explanation'] = f'{provider}: {REASONS.get(policy.fallback_reason, policy.fallback_reason)}. '
        view['explanation'] += ('Model calls paused until explicitly re-enabled.' if latched
                                else 'Rule handles this check; another model attempt is expected next window.')
        if policy.shadow:
            view['explanation'] += ' Shadow mode still applies no priority changes.'
    stale = (telemetry_stale or telemetry_receipt is None or now - telemetry_receipt / 1e9 > 3)
    linux_now = (anchor[0] / 1e9 + now - anchor[1] / 1e9) if anchor else None
    if stale:
        next_check = 'Waiting for valid telemetry'
    elif policy.window is None or linux_now is None:
        next_check = 'Waiting for first measurement window'
    else:
        remaining = policy.window.end + 5 - linux_now
        next_check = f'Expected in ~{remaining:.1f} s' if remaining > 0 else 'Waiting for next telemetry window'
    view['next_check'] = ('Rule check: ' if latched or policy.requested == 'rule' else 'Decision check: ') + next_check
    if latched:
        view['next_call'] = f'{provider}: Paused'
    elif policy.requested == 'rule':
        view['next_call'] = 'Rule runs locally · no model call'
    elif policy.outstanding:
        left = max(0, 1 - (now - policy.outstanding[2]))
        view['next_call'] = f'{provider}: Request in progress · deadline in {left:.1f} s'
    elif policy.expired:
        view['next_call'] = f'{provider}: Waiting for expired helper request'
    else:
        view['next_call'] = f'{provider}: {next_check}'
    if policy.shadow:
        view['cooldown'] = 'Shadow · changes disabled'
    elif policy.command:
        view['cooldown'] = 'Awaiting priority acknowledgement'
    elif stale:
        view['cooldown'] = 'Waiting for valid telemetry'
    elif policy.state.valid_seconds < 10:
        view['cooldown'] = f'Initial observation: {policy.state.valid_seconds:.0f}/10 valid seconds'
    elif policy.state.last_change is not None and linux_now is not None and policy.state.last_change + 30 > linux_now:
        view['cooldown'] = f'{policy.state.last_change + 30 - linux_now:.0f} s cooldown remaining'
    else:
        view['cooldown'] = 'Eligible at next valid decision · guards still apply'
    if getattr(policy, 'resource', 'cpu') != 'cpu':
        view = {key: value.replace('priority', 'allocation') for key, value in view.items()}
    return view


class DecisionPanel(QFrame):
    page_requested = Signal(object)
    entry_selected = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('decisionActivity')
        self.setAccessibleName('Decision activity')
        self.setStyleSheet('#decisionActivity { border: 1px solid #cbd5e1; border-radius: 8px; }')
        self.live = OrderedDict()
        self.displayed = OrderedDict()
        self.items = {}
        self.names = {}
        self.origin_ns = None
        self.following = True
        self.page = {}
        self.latest_change = 'No confirmed priority changes'
        self.latest_ack_time = -math.inf
        self.live_discarded = False
        self.dirty = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)
        self.controls = QHBoxLayout()
        title = QLabel('Decision activity')
        font = title.font()
        font.setPointSize(15)
        font.setBold(True)
        title.setFont(font)
        self.controls.addWidget(title)
        self.controls.addStretch()
        self.controls.addWidget(QLabel('Selected policy'))
        layout.addLayout(self.controls)
        grid = QGridLayout()
        self.status_labels = {}
        for col, (key, caption) in enumerate((('active', 'CURRENTLY CONTROLLING'),
                                              ('next_call', 'NEXT CALL / EVALUATION'),
                                              ('cooldown', 'RESOURCE CHANGES'))):
            label = QLabel(caption)
            label.setStyleSheet('color: #64748b; font-size: 10px; font-weight: 600')
            grid.addWidget(label, 0, col)
            value = QLabel('—')
            value.setWordWrap(True)
            value.setTextFormat(Qt.TextFormat.PlainText)
            value.setAccessibleName(caption.title())
            grid.addWidget(value, 1, col)
            self.status_labels[key] = value
            grid.setColumnStretch(col, 1)
        layout.addLayout(grid)
        self.explanation = QLabel()
        self.explanation.setWordWrap(True)
        self.explanation.setTextFormat(Qt.TextFormat.PlainText)
        self.explanation.setStyleSheet('color: #985c00;')
        self.explanation.hide()
        layout.addWidget(self.explanation)
        self.timeline = QTreeWidget()
        self.timeline.setHeaderLabels(['Time', 'Provider', 'Decision', 'Outcome', 'Response'])
        self.timeline.setAccessibleName('Decision timeline; expand a decision for call details')
        self.timeline.setRootIsDecorated(True)
        self.timeline.setAlternatingRowColors(True)
        self.timeline.setWordWrap(True)
        self.timeline.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.timeline.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.timeline.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.timeline.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.timeline.header().setStretchLastSection(False)
        self.timeline.header().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 72), (1, 105), (2, 130), (4, 90)):
            self.timeline.setColumnWidth(col, width)
        self.timeline.setMinimumHeight(95)
        self.timeline.itemExpanded.connect(self.freeze)
        self.timeline.itemSelectionChanged.connect(self.select)
        layout.addWidget(self.timeline, 1)
        footer = QHBoxLayout()
        self.older = QPushButton('Older')
        self.newer = QPushButton('Newer')
        self.latest = QPushButton('Jump to latest')
        for button in (self.older, self.newer, self.latest):
            footer.addWidget(button)
        self.older.clicked.connect(lambda: self.request_page('before_cycle'))
        self.newer.clicked.connect(lambda: self.request_page('after_cycle'))
        self.latest.clicked.connect(self.jump_latest)
        self.note = QLabel('Every decision is recorded. Expand a row for details.')
        self.note.setWordWrap(True)
        footer.addWidget(self.note, 1)
        layout.addLayout(footer)
        self.clear()

    def clear(self):
        self.live.clear()
        self.displayed.clear()
        self.items.clear()
        self.timeline.clear()
        self.page = {}
        self.following = True
        self.latest_change = 'No confirmed priority changes'
        self.latest_ack_time = -math.inf
        self.live_discarded = False
        self.older.setEnabled(False)
        self.newer.setEnabled(False)
        self.note.setText('Every decision is recorded. Expand a row for details.')

    def ingest(self, event):
        key = event.get('cycle_id')
        if not key or event.get('kind') in ('policy_started', 'policy_stopped', 'provider_configuration'):
            return
        entry = self.live.get(key)
        if entry is None:
            entry = DecisionEntry(key)
            self.live[key] = entry
            if len(self.live) > 100:
                self.live.popitem(last=False)
                self.live_discarded = True
        entry.add(event)
        if key in self.displayed and self.displayed[key] is not entry:
            self.displayed[key].add(event)
        if self.following:
            self.displayed = OrderedDict(reversed(list(self.live.items())))
        elif key not in self.displayed:
            self.note.setText('New decisions available · Jump to latest to follow live.')
        if event.get('kind') in ('priority_outcome', 'decision_acknowledgement'):
            self.update_latest_change(entry)
        self.dirty = True

    def update_latest_change(self, entry):
        if entry.acknowledgement and (
                entry.acknowledged_at is None or entry.acknowledged_at >= self.latest_ack_time):
            if entry.acknowledged_at is not None:
                self.latest_ack_time = entry.acknowledged_at
            self.latest_change = entry.columns(self.origin_ns, self.names)[3] + ' · ' + elapsed(
                entry.acknowledged_at - self.origin_ns / 1e9 if entry.acknowledged_at is not None and self.origin_ns is not None else None)

    def render(self):
        if not self.dirty:
            return
        self.dirty = False
        self.timeline.blockSignals(True)
        for key in list(self.items):
            if key not in self.displayed:
                item = self.items.pop(key)
                self.timeline.takeTopLevelItem(self.timeline.indexOfTopLevelItem(item))
        for index, (key, entry) in enumerate(self.displayed.items()):
            item = self.items.get(key)
            if item is None:
                item = QTreeWidgetItem()
                item.setData(0, Qt.ItemDataRole.UserRole, key)
                self.timeline.insertTopLevelItem(index, item)
                child = QTreeWidgetItem(item)
                child.setFirstColumnSpanned(True)
                self.items[key] = item
            for col, value in enumerate(entry.columns(self.origin_ns, self.names)):
                item.setText(col, value)
                item.setToolTip(col, value)
            item.child(0).setText(0, entry.details(self.origin_ns, self.names))
            item.setForeground(3, QColor('#147d55' if entry.acknowledgement else '#64748b'))
        self.timeline.blockSignals(False)
        if self.following:
            self.older.setEnabled(self.live_discarded or self.page.get('has_older', False))
            self.newer.setEnabled(False)

    def update_status(self, status):
        self.status_labels['active'].setText(status['active'])
        self.status_labels['next_call'].setText(status['next_call'] + '\n' + status['next_check'])
        self.status_labels['cooldown'].setText(status['cooldown'] + '\n' + self.latest_change)
        for label in self.status_labels.values():
            label.setAccessibleName(label.text())
        self.explanation.setText(status['explanation'])
        self.explanation.setVisible(bool(status['explanation']))
        self.fit_height()

    def fit_height(self):
        # QSplitter uses minimumSizeHint, which omits wrapped label heights.
        # Count actual height-for-width minima so the timeline cannot cover its footer.
        layout = self.layout()
        margins = layout.contentsMargins()
        width = max(1, self.width() - margins.left() - margins.right() - 2 * self.frameWidth())
        items = [layout.itemAt(i) for i in range(layout.count()) if not layout.itemAt(i).isEmpty()]
        height = sum(max(item.minimumSize().height(), item.heightForWidth(width)) for item in items)
        height += margins.top() + margins.bottom() + 2 * self.frameWidth() + max(0, len(items)-1) * layout.spacing()
        self.setMinimumHeight(height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'timeline'):
            self.fit_height()

    def freeze(self, *_):
        self.following = False

    def select(self):
        item = self.timeline.currentItem()
        if item:
            item = item.parent() or item
            self.freeze()
            self.entry_selected.emit(self.displayed.get(item.data(0, Qt.ItemDataRole.UserRole)))

    def request_page(self, direction):
        if not self.displayed:
            return
        self.following = False
        key = next(reversed(self.displayed)) if direction == 'before_cycle' else next(iter(self.displayed))
        self.note.setText('Loading recorded decisions…')
        self.page_requested.emit({direction: key})

    def jump_latest(self):
        self.following = True
        self.page_requested.emit({})

    def set_page(self, page, *, live=False):
        self.names.update(page.get('workload_names', {}))
        self.page = page
        previous, self.displayed = self.displayed, OrderedDict()
        for cycle in page.get('cycles', []):
            entry = DecisionEntry(cycle['cycle_id'])
            for event in cycle['events']:
                entry.add(event)
            # Acknowledgements can arrive after any page's database snapshot.
            for cached in (previous.get(entry.cycle_id), self.live.get(entry.cycle_id)):
                if cached:
                    for event in cached.events:
                        entry.add(event)
            self.displayed[entry.cycle_id] = entry
            self.update_latest_change(entry)
        if live:
            # Keep events received while the worker was reading this page.
            recorded = OrderedDict(reversed(list(self.displayed.items())))
            for key, entry in self.live.items():
                if key not in recorded:
                    recorded[key] = entry
            self.live = OrderedDict(list(recorded.items())[-100:])
            self.displayed = OrderedDict(reversed(list(self.live.items())))
        self.following = live
        self.older.setEnabled(page.get('has_older', False))
        self.newer.setEnabled(page.get('has_newer', False))
        self.note.setText(f'{len(self.displayed)} decisions on this page · expand a row for details.')
        self.dirty = True
        self.render()
