"""Unprivileged guest controller and complete session recordings."""

import json
import os
from pathlib import Path
import queue
import re
import signal
import sys
import threading
import time
import uuid

from PySide6.QtCore import QLockFile, QStandardPaths, QTimer, Qt
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                              QHeaderView, QMainWindow, QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem,
                              QTabWidget, QVBoxLayout, QWidget)

from ..session_store import SessionStore
from .client import request
from .protocol import PROTOCOL
from .policy import GuestPolicy, control_of, new_helper, pressure
from .readiness import GuestReadiness, fingerprint, save_report, validate_report
from .resources import counters


class GuestWorker(threading.Thread):
    def __init__(self, database, transport=request, *, jev_key=''):
        super().__init__(name='guest-controller', daemon=False)
        self.database, self.transport = database, transport
        self.requests, self.updates = queue.Queue(maxsize=1), queue.Queue(maxsize=256)
        self.stop = threading.Event()
        self.run_id = None
        self.session_id = str(uuid.uuid4())
        self.error = None
        self.policy = GuestPolicy(Path(database).parent / 'readiness', key=jev_key, require_qualification=False)
        self.readiness = None
        self.latest = {}
        self.activity = None

    def show_activity(self, event, source):
        self.activity = {'event': event, 'source': source, 'at_ns': time.time_ns(),
                         'generation': self.latest.get('generation'), 'run_id': self.run_id,
                         'identities': {t['id']: t['identity'] for t in self.latest.get('tasks', [])}}

    def audit(self, **event):
        self.store.record(self.session_id, {'backend': 'guest-linux', **event}, time.monotonic_ns())
        self.store.flush()
        if event.get('kind') == 'guest_model_audit':
            source = 'Jev' if event.get('valid') and not event.get('fallback_reason') else 'Rule fallback'
            if event.get('mode') == 'shadow':
                source = 'Rule (Jev shadow)'
            self.show_activity(event, source)
        elif event.get('kind') == 'guest_policy_fallback':
            self.show_activity(event, 'Rule fallback')

    def command(self, op, _source=None, **fields):
        if op in ('probe', 'cpu_decide', 'resource_decide', 'memory_profile', 'restore', 'end'):
            fields['run_id'] = self.run_id
        if op != 'snapshot':
            self.audit(kind='guest_command', op=op, **fields)
        result = self.transport(op, **fields)
        if op in ('begin', 'begin_cpu', 'begin_control'):
            self.run_id = result['run_id']
            self.policy.previous, self.policy.next_decision = None, 0.
            self.policy.shadow_rows = []
            self.activity = None
        elif op == 'end' or (op == 'snapshot' and result['run_id'] != self.run_id):
            self.run_id = None
            self.policy.cancel()
        if op in ('cpu_decide', 'resource_decide', 'restore', 'end'):
            source = _source or ('Rule' if self.policy.mode == 'rule' else 'Rule fallback')
            self.show_activity({'action': fields.get('action', op), 'acknowledgement': result}, source)
        if op == 'snapshot':
            self.latest = result
            result['controller_policy'] = {'requested': self.policy.mode, 'decisions': self.policy.decisions,
                                          'fallbacks': self.policy.fallbacks,
                                          'fallback_proportion': self.policy.fallbacks / max(1, self.policy.decisions),
                                          'readiness_progress': len(self.readiness.rows) if self.readiness else None,
                                          'activity': self.activity}
        elif self.latest:
            control_of(self.latest)['ready'] = False
        self.audit(kind='guest_snapshot' if op == 'snapshot' else 'guest_ack', op=op, **result)
        return result

    def submit(self, op, **fields):
        self.requests.put_nowait((op, fields))

    def heartbeat(self):
        while not self.stop.wait(1):
            run_id = self.run_id
            if run_id:
                try:
                    self.transport('heartbeat', run_id=run_id)
                except (OSError, RuntimeError, ValueError):
                    # A heartbeat failure cannot authorize a new run or extend a local deadline.
                    pass

    def run(self):
        store = None
        pulse = threading.Thread(target=self.heartbeat, name='guest-heartbeat', daemon=True)
        try:
            store = SessionStore(self.database)
            self.store = store
            store.begin(self.session_id, {'backend': 'guest-linux', 'protocol': PROTOCOL, 'milestone': 5})
            pulse.start()
            next_sample = 0
            while not self.stop.is_set():
                if self.readiness:
                    self.readiness.poll()
                    if self.readiness.done:
                        path = save_report(self.policy.directory, self.readiness.report)
                        self.audit(kind='guest_readiness_result', report=self.readiness.report, path=str(path))
                        self.updates.put_nowait({'op': 'readiness_done', 'result': {'passed': self.readiness.report['passed'], 'path': str(path)}})
                        self.readiness = None
                elif self.latest and self.run_id:
                    try:
                        self.policy.tick(self.latest, self.command, self.audit)
                    except (OSError, RuntimeError, ValueError) as exc:
                        self.audit(kind='guest_policy_error', error=str(exc))
                        self.updates.put_nowait({'op': 'error', 'error': str(exc)})
                try:
                    op, fields = self.requests.get(timeout=.1)
                except queue.Empty:
                    if time.monotonic() < next_sample or self.policy.pending:
                        continue
                    op, fields = 'snapshot', {}
                try:
                    if op == 'policy':
                        if self.readiness:
                            raise ValueError('Finish or cancel readiness first')
                        if self.run_id:
                            self.policy.cancel()
                            self.command('restore', _source='Manual')
                        self.policy.set_mode(fields['mode'], self.latest, fields.get('resource'))
                        self.audit(kind='guest_policy_selected', mode=self.policy.mode)
                        result = {'mode': self.policy.mode}
                    elif op in ('readiness', 'cancel_readiness'):
                        if op == 'cancel_readiness':
                            if self.readiness:
                                self.readiness.finish()
                            result = {'cancelled': True}
                        else:
                            if self.run_id or self.readiness:
                                raise ValueError('Readiness requires no active run or pending check')
                            self.latest = self.command('snapshot')
                            if not self.latest['preflight']['passed']:
                                raise ValueError('Guest preflight must pass before readiness')
                            if not self.policy.key:
                                raise ValueError('Supply Jev credentials through --jev-env-file')
                            self.readiness = GuestReadiness(new_helper(self.policy.key), self.latest, fields['resource'])
                            result = {'started': True, 'contexts': 108, 'resource': fields['resource']}
                    elif op == 'save_shadow':
                        resource = control_of(self.latest).get('resource', 'cpu')
                        report = {'kind': 'guest_shadow', 'backend': 'guest-linux', 'resource': resource,
                                  'fingerprint': fingerprint(self.latest, resource), 'rows': list(self.policy.shadow_rows),
                                  'completed_at_ns': time.time_ns()}
                        report['passed'] = validate_report(report, self.latest, resource, shadow=True)
                        path = save_report(self.policy.directory, report)
                        self.audit(kind='guest_shadow_result', report=report)
                        result = {'passed': report['passed'], 'path': str(path)}
                    elif op in ('history', 'load', 'export'):
                        if self.run_id:
                            raise ValueError('End the control run before browsing recordings')
                        if op == 'history':
                            result = {'sessions': store.sessions()}
                        elif op == 'load':
                            result = store.load(fields['session_id'])
                        else:
                            store.export(fields['session_id'], fields['path'], fields['format'])
                            result = {'path': str(fields['path'])}
                    else:
                        if op != 'snapshot':
                            if self.readiness:
                                raise ValueError('Finish or cancel readiness before changing guest state')
                            if op in ('restore', 'end'):
                                self.policy.cancel()
                                self.policy.mode = 'manual'
                        result = self.command(op, _source='Manual', **fields)
                    self.updates.put_nowait({'op': op, 'result': result})
                except (OSError, RuntimeError, ValueError) as exc:
                    store.record(self.session_id, {'kind': 'guest_error', 'op': op, 'error': str(exc)}, time.monotonic_ns())
                    store.flush()
                    self.updates.put_nowait({'op': 'error', 'error': str(exc), 'failed_op': op,
                                            'policy_mode': self.policy.mode})
                next_sample = time.monotonic() + 1
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.stop.set()
            self.policy.close()
            if self.readiness:
                self.readiness.finish()
                save_report(self.policy.directory, self.readiness.report)
            try:
                if self.run_id:
                    result = self.transport('end', run_id=self.run_id)
                    self.run_id = None
                    if store:
                        store.record(self.session_id, {'kind': 'guest_ack', 'op': 'end', **result}, time.monotonic_ns())
            except Exception as exc:
                self.error = f'Restoration pending at service: {exc}'
            if store:
                try:
                    store.finish(self.session_id, 'failed' if self.error else 'completed')
                    store.close()
                except Exception as exc:
                    self.error = f'Recording could not be saved: {exc}'


class GuestWindow(QMainWindow):
    def __init__(self, worker, jev_key=''):
        super().__init__()
        self.worker, self.tasks, self.busy, self.closing = worker, [], False, False
        self.snapshot, self.importances, self.row_ids = {}, {}, []
        self.last_error = ''
        self.setWindowTitle('Adaptive OS Controller — Guest Linux')
        self.resize(1040, 900)
        font = self.font()
        font.setPointSize(12)
        self.setFont(font)
        tabs = QTabWidget()
        self.setCentralWidget(tabs)
        live = QWidget()
        layout = QVBoxLayout(live)
        self.status = QLabel('Connecting to the guest service…')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.decision = QLabel('Waiting for a control run')
        self.decision.setTextFormat(Qt.TextFormat.PlainText)
        self.decision.setWordWrap(True)
        title_font = self.font()
        title_font.setPointSize(20)
        title_font.setBold(True)
        self.decision.setFont(title_font)
        self.decision_detail = QLabel('Choose the foreground application and begin a run.')
        self.decision_detail.setTextFormat(Qt.TextFormat.PlainText)
        self.decision_detail.setWordWrap(True)
        layout.addWidget(self.decision)
        layout.addWidget(self.decision_detail)
        actions = QHBoxLayout()
        self.buttons = {}
        for label, op, fields in [('Launch Firefox', 'launch', {'app': 'firefox'}),
                                  ('Launch Geany', 'launch', {'app': 'geany'}),
                                  ('Begin run', 'begin_control', {}), ('Favor foreground', 'resource_decide', {}),
                                  ('Restore', 'restore', {}), ('End run', 'end', {})]:
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, op=op, fields=fields: self.send(op, **fields))
            actions.addWidget(button)
            self.buttons[label] = (button, op)
        layout.addLayout(actions)
        roles = QHBoxLayout()
        roles.addWidget(QLabel('Resource'))
        self.resource = QComboBox()
        self.resource.addItems(['cpu', 'memory', 'network'])
        roles.addWidget(self.resource)
        roles.addWidget(QLabel('Foreground (fixed for the run)'))
        self.foreground = QComboBox()
        roles.addWidget(self.foreground)
        layout.addLayout(roles)
        policies = QHBoxLayout()
        self.policy = QComboBox()
        for label, value in [('Manual', 'manual'), ('Rule', 'rule'), ('Jev', 'jev'), ('Jev shadow (Rule applies)', 'shadow')]:
            self.policy.addItem(label, value)
        policies.addWidget(QLabel('Policy'))
        policies.addWidget(self.policy)
        apply_policy = QPushButton('Apply policy')
        apply_policy.clicked.connect(lambda: self.send('policy', mode=self.policy.currentData(), resource=self.resource.currentText()))
        policies.addWidget(apply_policy)
        self.active_policy = QLabel('Active: Manual')
        policies.addWidget(self.active_policy)
        layout.addLayout(policies)
        self.setup = QWidget()
        setup_layout = QVBoxLayout(self.setup)
        self.memory_setup = QWidget()
        memory_layout = QHBoxLayout(self.memory_setup)
        self.profiles = QComboBox()
        self.profiles.addItem('New memory profile (high=max)', None)
        memory_layout.addWidget(self.profiles)
        for label, action in [('Establish headroom', 'establish'), ('Increase headroom', 'grow')]:
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, action=action: self.send('memory_profile', action=action))
            memory_layout.addWidget(button)
        setup_layout.addWidget(self.memory_setup)
        self.table = QTableWidget(0, 14)
        self.table.setHorizontalHeaderLabels(['Application /\ntask', 'Importance', 'Weight', 'CPUs', 'Consumption',
            'Protection\nlow', 'Throttle\nhigh', 'Upload\nshare', 'Download\nshare', 'Memory PSI\n(avg10)', 'Refault /\nscan', 'OOM /\nhigh events',
            'Upload\nMbit/s', 'Download\nMbit/s'])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(68)
        self.table.verticalHeader().hide()
        layout.addWidget(self.table)
        self.limits = QLabel('Parent safety cap: 4 GiB. Close applications in their own windows.')
        self.limits.setWordWrap(True)
        layout.addWidget(self.limits)
        self.setup_toggle = QPushButton('Setup and diagnostics')
        self.setup_toggle.setCheckable(True)
        self.setup_toggle.toggled.connect(self.setup.setVisible)
        layout.addWidget(self.setup_toggle)
        providers = QHBoxLayout()
        providers.addWidget(QLabel('Jev key'))
        key = QLineEdit(jev_key)
        key.setEchoMode(QLineEdit.EchoMode.Password)
        key.setEnabled(False)
        providers.addWidget(key)
        for label, op in [('Check Jev readiness', 'readiness'), ('Cancel check', 'cancel_readiness'), ('Save shadow evidence', 'save_shadow')]:
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, op=op: self.send(op, **({'resource': self.resource.currentText()} if op == 'readiness' else {})))
            providers.addWidget(button)
        setup_layout.addLayout(providers)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        setup_layout.addWidget(self.details)
        layout.addWidget(self.setup)
        self.setup.hide()
        self.resource.currentTextChanged.connect(self.resource_view)
        self.resource_view()
        tabs.addTab(live, 'Guest applications')
        history = QWidget()
        history_layout = QVBoxLayout(history)
        history_actions = QHBoxLayout()
        refresh = QPushButton('Refresh recordings')
        refresh.clicked.connect(lambda: self.send('history'))
        history_actions.addWidget(refresh)
        self.recordings = QComboBox()
        history_actions.addWidget(self.recordings)
        load = QPushButton('Open')
        load.clicked.connect(lambda: self.send('load', session_id=self.recordings.currentData()))
        history_actions.addWidget(load)
        for format in ('json', 'csv'):
            export = QPushButton('Export ' + format.upper())
            export.clicked.connect(lambda checked=False, format=format: self.export(format))
            history_actions.addWidget(export)
        history_layout.addLayout(history_actions)
        self.history_text = QPlainTextEdit()
        self.history_text.setReadOnly(True)
        history_layout.addWidget(self.history_text)
        tabs.addTab(history, 'Recordings')
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(100)

    def resource_view(self):
        resource = self.resource.currentText()
        visible = {'network': {0, 1, 7, 8, 12, 13}, 'cpu': {0, 1, 2, 3, 4},
                   'memory': {0, 1, 4, 5, 6, 9, 10, 11}}[resource]
        for column in range(self.table.columnCount()):
            self.table.setColumnHidden(column, column not in visible)
        self.table.horizontalHeaderItem(7).setText('Upload\nguaranteed share')
        self.table.horizontalHeaderItem(8).setText('Download\nguaranteed share')
        self.memory_setup.setVisible(resource == 'memory')

    def render_activity(self, snapshot):
        activity = snapshot.get('controller_policy', {}).get('activity') or {}
        identities = {t['id']: t.get('identity') for t in snapshot['tasks']}
        if (not activity or activity.get('generation') != snapshot.get('generation')
                or activity.get('run_id') != snapshot.get('run_id') or activity.get('identities') != identities):
            self.decision.setText('Observing the applications' if snapshot.get('run_id') else 'Ready for a control run')
            self.decision_detail.setText('Waiting for a fresh decision.' if snapshot.get('run_id')
                                         else 'Applications remain open. Select the foreground and begin.')
            return
        event, source = activity['event'], activity['source']
        ack = event.get('acknowledgement')
        verified = isinstance(ack, dict) and event.get('kernel_guard_passed', True)
        names = {'favor': 'Favor foreground', 'hold': 'Hold', 'restore': 'Restore', 'end': 'End run'}
        proposed = (event.get('proposal') or {}).get('action', event.get('action')) if source == 'Jev' else event.get('action')
        applied = ack.get('action', event.get('action')) if verified else None
        if verified and 'restoration' in ack:
            applied = 'restore'
        self.decision.setText(f'{source} · {names.get(proposed, "Waiting")}')
        at = time.strftime('%H:%M:%S', time.localtime(activity['at_ns'] / 1e9))
        detail = f'Applied: {names.get(applied, "none")} · Linux acknowledged · {at}' if verified else f'Not applied · {at}'
        if event.get('fallback_reason'):
            detail += '\n' + event['fallback_reason'].replace('_', ' ')
        if event.get('latency_ms') is not None:
            detail += f' · response {event["latency_ms"]:.0f} ms'
        self.decision_detail.setText(detail)

    def send(self, op, **fields):
        if self.busy or self.closing:
            return
        if op in ('begin_cpu', 'begin_control'):
            if len(self.tasks) < 2 or not self.foreground.currentData():
                self.status.setText('Launch at least two applications and select the foreground.')
                return
            fields = {'generation': self.snapshot['generation'], 'foreground_id': self.foreground.currentData(),
                      'tasks': [{'task_id': t['id'], 'identity': t['identity'],
                                 'importance': self.importances.get(t['id'], 'Normal')} for t in self.tasks]}
            if op == 'begin_control':
                fields.update(resource=self.resource.currentText(), scored=False,
                              profile_id=self.profiles.currentData() if self.resource.currentText() == 'memory' else None)
        elif op in ('cpu_decide', 'resource_decide', 'memory_profile'):
            control = control_of(self.snapshot)
            if op == 'memory_profile':
                if control.get('resource') != 'memory':
                    self.status.setText('Begin a Memory run before profiling or adjusting headroom.')
                    return
                fields = {'generation': self.snapshot['generation'], 'observation_id': control.get('observation_id'), **fields}
            else:
                if not control.get('ready'):
                    self.status.setText('Waiting for fresh observations, a suitable profile, or cooldown.')
                    return
                fields = {'generation': self.snapshot['generation'], 'observation_id': control['observation_id'], 'action': 'favor'}
        try:
            self.worker.submit(op, **fields)
            self.busy = True
        except queue.Full:
            self.status.setText('A request is already pending.')

    def export(self, format):
        if not self.recordings.currentData():
            return
        path, _ = QFileDialog.getSaveFileName(self, 'Export guest recording', 'guest-recording.' + format)
        if path:
            self.send('export', session_id=self.recordings.currentData(), path=path, format=format)

    def poll(self):
        while True:
            try:
                event = self.worker.updates.get_nowait()
            except queue.Empty:
                break
            if event['op'] != 'snapshot':
                self.busy = False
                if event['op'] != 'error':
                    self.last_error = ''
            if event['op'] == 'error':
                self.last_error = event['error']
                self.status.setText(event['error'])
                if event.get('failed_op') == 'policy':
                    mode = event.get('policy_mode')
                    index = self.policy.findData(mode)
                    if index >= 0:
                        self.policy.setCurrentIndex(index)
                        self.observed_policy = mode
                        self.active_policy.setText('Active: ' + self.policy.itemText(index))
            elif event['op'] == 'snapshot':
                snapshot = event['result']
                previous = self.snapshot
                self.snapshot = snapshot
                self.tasks = [task for task in snapshot['tasks'] if task]
                control = control_of(snapshot)
                if control:
                    self.resource.setCurrentText(control.get('resource', 'cpu'))
                ids = [t['id'] for t in self.tasks]
                foreground = control.get('foreground_id', self.foreground.currentData())
                if ids != self.row_ids:
                    self.foreground.clear()
                    for task in self.tasks:
                        self.foreground.addItem(task['app'] + ' / ' + task['id'][:8], task['id'])
                if foreground in ids:
                    self.foreground.setCurrentIndex(ids.index(foreground))
                row = self.table.currentRow()
                self.table.setRowCount(len(self.tasks))
                for index, task in enumerate(self.tasks):
                    importance = self.table.cellWidget(index, 1)
                    if index >= len(self.row_ids) or self.row_ids[index] != task['id'] or importance is None:
                        importance = QComboBox()
                        importance.addItems(['Low', 'Normal', 'High'])
                        importance.setCurrentText(self.importances.get(task['id'], 'Normal'))
                        importance.currentTextChanged.connect(lambda value, task_id=task['id']: self.importances.update({task_id: value}))
                        self.table.setCellWidget(index, 1, importance)
                    if control:
                        importance.setCurrentText(control['importance'][task['id']])
                    importance.setEnabled(not self.worker.run_id and not self.busy)
                    expected = control.get('expected', {})
                    shares = [expected.get(direction, {}).get('shares', {}).get(task['id']) for direction in ('upload', 'download')]
                    events = counters(task.get('memory_events', {}))
                    stats = counters(task.get('memory_stat', {}))
                    memory_high = task.get('memory_high', 'max')
                    rates = []
                    old = next((t for t in previous.get('tasks', []) if t['id'] == task['id']
                                and t.get('identity') == task.get('identity')), {})
                    seconds = (snapshot.get('observed_at_ns', 0) - previous.get('observed_at_ns', 0)) / 1e9
                    for direction in ('upload', 'download'):
                        current_bytes = task.get('network', {}).get(direction + '_bytes')
                        old_bytes = old.get('network', {}).get(direction + '_bytes')
                        valid = (control.get('resource') == 'network' and seconds > 0
                                 and previous.get('generation') == snapshot.get('generation')
                                 and previous.get('run_id') == snapshot.get('run_id')
                                 and current_bytes is not None and old_bytes is not None and current_bytes >= old_bytes)
                        rates.append(f'{(current_bytes - old_bytes) * 8 / seconds / 1e6:.2f}' if valid else '—')
                    role = 'Foreground' if task['id'] == foreground else 'Background'
                    memory_pressure = pressure(task.get('memory_pressure'))
                    values = [task['app'].capitalize() + '\n' + role, '', task.get('cpu_weight', '?'), task.get('cpus_effective', '?'),
                              f'{task.get("memory_current", 0) / 1024**2:.1f} MiB',
                              f'{task.get("memory_low", 0) / 1024**2:.0f} MiB',
                              'max' if memory_high == 'max' else f'{int(memory_high) / 1024**2:.0f} MiB',
                              *['unshaped' if share is None else f'{share / 100:.2f}%' for share in shares],
                              f'{memory_pressure:.2f}%' if memory_pressure is not None else '—',
                              f'{stats.get("workingset_refault_file", 0)} / {stats.get("pgscan", 0)}',
                              f'{events.get("oom", 0)} / {events.get("high", 0)}', *rates]
                    for col, value in enumerate(values):
                        if col != 1:
                            item = QTableWidgetItem(str(value))
                            item.setToolTip(task['id'])
                            self.table.setItem(index, col, item)
                self.row_ids = ids
                if 0 <= row < len(self.tasks):
                    self.table.selectRow(row)
                self.details.setPlainText(json.dumps(snapshot, indent=2))
                profile_id = self.profiles.currentData()
                self.profiles.clear()
                self.profiles.addItem('New memory profile (high=max)', None)
                identities = {t['id']: t.get('identity') for t in self.tasks}
                for profile in snapshot.get('memory_profiles', []):
                    if profile.get('valid') and profile.get('identities') == identities:
                        self.profiles.addItem('Profile ' + profile['id'][:8], profile['id'])
                chosen = self.profiles.findData(profile_id)
                if chosen >= 0:
                    self.profiles.setCurrentIndex(chosen)
                self.limits.setText('Network: 20 Mbit/s shared per direction. Shares are guarantees; unused capacity can be borrowed. '
                                    'Mbit/s columns show measured traffic.' if self.resource.currentText() == 'network' else
                                    'Memory: 3 GiB protection during Memory runs; 4 GiB parent safety cap. CPU placement stays fixed.')
                self.render_activity(snapshot)
                mode = snapshot.get('controller_policy', {}).get('requested')
                if self.policy.findData(mode) >= 0:
                    self.active_policy.setText('Active: ' + self.policy.itemText(self.policy.findData(mode)))
                if mode and mode != getattr(self, 'observed_policy', None) and self.policy.findData(mode) >= 0:
                    self.policy.setCurrentIndex(self.policy.findData(mode))
                    self.observed_policy = mode
                readiness = (f' — observations {min(10, control.get("valid_seconds", 0)):.1f}/10 s'
                             f' — cooldown {control.get("cooldown_seconds", 0):.0f} s' if control else '')
                if control.get('resource') == 'memory':
                    readiness += f' — pressure-free profiling {control.get("profile_seconds", 0):.1f}/30 s'
                    if control.get('headroom_required'):
                        readiness += ' — increase headroom; scoring invalid until recalibration'
                check = snapshot.get('controller_policy', {}).get('readiness_progress')
                if check is not None:
                    readiness += f' — Jev readiness {check}/108'
                self.status.setText(('Run active' if control else 'Monitoring; applications stay open') + readiness
                                    + (' — guest preflight failed' if not snapshot['preflight']['passed'] else '')
                                    + (f' — recovery pending: {snapshot["recovery_error"]}' if snapshot['recovery_error'] else '')
                                    + (f' — {self.last_error}' if self.last_error else ''))
            elif event['op'] == 'history':
                self.recordings.clear()
                for session in event['result']['sessions']:
                    self.recordings.addItem(session['started_at'] + ' — ' + session['status'], session['id'])
            elif event['op'] == 'load':
                self.history_text.setPlainText(json.dumps(event['result'], indent=2))
            else:
                if event['op'] == 'policy':
                    mode = event['result']['mode']
                    index = self.policy.findData(mode)
                    self.policy.setCurrentIndex(index)
                    self.observed_policy = mode
                    self.active_policy.setText('Active: ' + self.policy.itemText(index))
                if control_of(self.snapshot):
                    control_of(self.snapshot)['ready'] = False
                self.status.setText(event['op'] + ' acknowledged' + (' — ' + str(event['result']['passed']) if 'passed' in event['result'] else ''))
        for button, op in self.buttons.values():
            enabled = bool(self.worker.run_id) if op in ('cpu_decide', 'resource_decide', 'restore', 'end') else not self.worker.run_id
            if op in ('cpu_decide', 'resource_decide'):
                enabled = enabled and control_of(self.snapshot).get('ready', False)
            if op in ('begin_cpu', 'begin_control'):
                enabled = (enabled and len(self.tasks) >= 2 and 'cpu_weight_control' in self.snapshot.get('capabilities', [])
                           and self.snapshot['preflight']['passed'] and not self.snapshot['recovery_error'])
            button.setEnabled(bool(enabled and not self.busy and not self.closing))
        self.foreground.setEnabled(not self.worker.run_id and not self.busy and not self.closing)
        self.resource.setEnabled(not self.worker.run_id and not self.busy and not self.closing)
        self.profiles.setEnabled(not self.worker.run_id and not self.busy and not self.closing)
        for row in range(self.table.rowCount()):
            self.table.cellWidget(row, 1).setEnabled(not self.worker.run_id and not self.busy and not self.closing)
        if not self.worker.is_alive() and self.worker.ident is not None:
            if self.worker.error:
                self.status.setText(self.worker.error)
            if self.closing:
                self.close()

    def request_shutdown(self, *_):
        self.closing = True
        self.worker.stop.set()
        self.status.setText('Restoring resource settings and saving recordings; applications remain open…')

    def closeEvent(self, event):
        if self.worker.is_alive():
            event.ignore()
            self.request_shutdown()
        else:
            event.accept()


def main(directory=None, jev_key=''):
    if sys.platform != 'linux':
        from .preflight import host_report
        report = host_report(Path.cwd())
        free = report['storage']['free_bytes'] / 1024**3
        print(f'Guest Linux runs inside Ubuntu 24.04 ARM64/Plasma X11 in UTM, using its local guest service. '
              f'Host storage: {free:.1f} GiB free; 180 GiB is budgeted for the VM, recovery copy and headroom. '
              'See docs/guest-linux.md.', file=sys.stderr)
        return 2
    if os.geteuid() == 0:
        print('Run the guest Qt controller as the ordinary desktop user.', file=sys.stderr)
        return 2
    if (os.environ.get('XDG_SESSION_TYPE') != 'x11'
            or 'KDE' not in os.environ.get('XDG_CURRENT_DESKTOP', '').split(':')
            or not re.fullmatch(r':[0-9]{1,2}(?:\.0)?', os.environ.get('DISPLAY', ''))
            or os.environ.get('QT_QPA_PLATFORM', 'xcb') != 'xcb'):
        print('Start the guest controller from a Plasma (X11) terminal with a local DISPLAY and Qt xcb. '
              'Wayland and headless Qt sessions cannot qualify the desktop.', file=sys.stderr)
        return 2
    os.environ['QT_QPA_PLATFORM'] = 'xcb'
    os.sched_setaffinity(0, {0})
    app = QApplication.instance() or QApplication([sys.argv[0]])
    if app.platformName() != 'xcb':
        print('A working Qt X11/xcb display is required; see docs/guest-linux.md.', file=sys.stderr)
        return 2
    app.setApplicationName('Adaptive OS Controller')
    app.setOrganizationName('Adaptive OS')
    directory = (directory or Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))) / 'guest-linux'
    directory.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(directory / 'desktop.lock'))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        print('Another guest controller is using this recording directory.', file=sys.stderr)
        return 1
    worker = GuestWorker(directory / 'sessions-v2.sqlite3', jev_key=jev_key)
    window = GuestWindow(worker, jev_key)
    del jev_key
    previous = {sig: signal.signal(sig, window.request_shutdown) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        window.show()
        worker.start()
        result = app.exec()
    finally:
        worker.stop.set()
        worker.join(timeout=30)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        lock.unlock()
    if worker.error:
        print(worker.error, file=sys.stderr)
    return result or int(bool(worker.error))
