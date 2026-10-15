"""Native Qt Widgets playground; no web server, browser, or webview."""

import argparse
import json
from pathlib import Path
import queue
import shlex
import signal
import sys
import time
import uuid

from PySide6.QtCore import QLockFile, QStandardPaths, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog,
    QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSpinBox, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .charts import COLORS, TimeSeriesChart
from .cpu_rule import CpuWindow, ManagedTask, RuleState, acknowledge, evaluate_rule
from .resource_control import ResourceTask, allocation_decision
from .resource_experiments import resource_calibration_key, resource_comparison, summarize_resource
from .experiments import (calibration_key, comparison_report, inference_summary,
                          qualifies_calibration, summarize_trial, trial_plan)
from .desktop_worker import SessionWorker
from .decision_controller import PolicyController
from .decision_activity import DecisionPanel, policy_status
from .model_runtime import HelperProcess, runtime_paths
from .model_readiness import (ReadinessRun, configuration_fingerprint, frozen_contexts,
                              readiness_matches, latest_reports, live_readiness_matches)
from .sandbox.protocol import MAX_WORKLOADS, PRESETS, PRIORITIES, VERSION, validate_config


def workload(preset, name=None, config=None):
    return {'id': str(uuid.uuid4()), 'name': name or PRESETS[preset], 'preset': preset,
            'config': validate_config(preset, config or {}), 'status': 'stopped',
            'nice': 0, 'sample': {}, 'stale': False, 'receipt_ns': 0,
            'role': 'Foreground' if preset == 'periodic' else ('Background' if preset == 'cpu' else 'Unmanaged'),
            'importance': 'Normal'}


def format_number(value, digits=1):
    return '—' if value is None else f'{value:,.{digits}f}'


class WorkloadDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Add workload')
        self.setMinimumWidth(350)
        form = QFormLayout(self)
        self.name = QLineEdit()
        self.name.setMaxLength(80)
        self.preset = QComboBox()
        for key, label in PRESETS.items():
            if key not in ('cache', 'network'):
                self.preset.addItem(label, key)
        self.budget = QDoubleSpinBox()
        self.budget.setRange(.1, 19.9)
        self.budget.setValue(4)
        self.budget.setSuffix(' ms / 20 ms period')
        self.memory = QSpinBox()
        self.memory.setRange(1, 128)
        self.memory.setValue(64)
        self.memory.setSuffix(' MiB')
        form.addRow('&Name', self.name)
        form.addRow('&Preset', self.preset)
        form.addRow('CPU &budget', self.budget)
        form.addRow('&Memory buffer', self.memory)
        note = QLabel('All workloads compete on one Linux CPU. File I/O uses a 64 MiB scratch file. Up to 8 workloads; memory buffers total ≤512 MiB.')
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.preset.currentIndexChanged.connect(self.update_fields)
        self.update_fields()

    def update_fields(self):
        preset = self.preset.currentData()
        self.budget.setEnabled(preset == 'periodic')
        self.memory.setEnabled(preset == 'memory')
        self.name.setPlaceholderText(PRESETS[preset])

    def result_workload(self):
        preset = self.preset.currentData()
        config = {'budget_ms': self.budget.value()} if preset == 'periodic' else (
            {'mib': self.memory.value()} if preset == 'memory' else {})
        result = workload(preset, self.name.text().strip() or PRESETS[preset], config)
        result['role'] = 'Unmanaged'
        return result


class MainWindow(QMainWindow):
    def __init__(self, worker, *, allow_unqualified_jev_demo=False):
        super().__init__()
        self.worker = worker
        self.allow_unqualified_jev_demo = allow_unqualified_jev_demo
        self.workloads = {}
        self.pending = {}
        self.active = False
        self.resource = 'cpu'
        self.resource_supported = False
        self.resource_restoring = False
        self.allocation_history = []
        self.resource_effects = []
        self.busy = False
        self.priority_supported = False
        self.preflight_complete = False
        self.preflight_reason = 'Priority preflight pending'
        self.auto_active = False
        self.policy = PolicyController(audit=lambda **event: self.audit(**event))
        self.model_helper = None
        self.loaded_provider = None
        self.loaded_fingerprint = None
        self.readiness_run = None
        self.readiness = {}
        self.rule_state = RuleState()
        self.intervention_order = 0
        self.last_missing_bucket = None
        self.deferred_stop = None
        self.experiment = None
        self.qualified_config = None
        self.qualified_resources = {}
        self.session_id = None
        self.history_mode = False
        self.workload_offset = self.workload_total = 0
        self.origin_ns = None
        self.clock_anchor = None
        self.decision_page_token = None
        self.closing = False
        self.shutdown_requested = False
        self.quit_submitted = False
        self.last_worker_error = None
        self.leftovers = []
        self.setWindowTitle('Adaptive OS Controller')
        self.resize(1280, 900)
        self.setMinimumSize(1000, 740)
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(20, 16, 20, 12)
        heading = QLabel('Adaptive OS Controller')
        self.heading = heading
        font = heading.font()
        font.setPointSize(23)
        font.setWeight(QFont.Weight.DemiBold)
        heading.setFont(font)
        outer.addWidget(heading)
        intro = QLabel('One Linux sandbox. Choose CPU priorities, managed cache budgets, or loopback bandwidth shares.')
        self.intro = intro
        intro.setWordWrap(True)
        if self.allow_unqualified_jev_demo:
            intro.setText(intro.text() + '\nExperimental live Jev demo: unqualified Memory/Network control enabled.')
        outer.addWidget(intro)
        toolbar = QHBoxLayout()
        outer.addLayout(toolbar)
        self.prepare_button = self.button(toolbar, 'Prepare image', lambda: self.action('prepare'))
        self.start_button = self.button(toolbar, 'Start sandbox', self.start_session)
        self.stop_button = self.button(toolbar, 'Stop sandbox', self.stop_session_requested)
        toolbar.addSpacing(16)
        self.add_button = self.button(toolbar, 'Add workload', self.add_dialog)
        self.start_all_button = self.button(toolbar, 'Start all', self.start_all)
        self.stop_all_button = self.button(toolbar, 'Stop all', self.stop_all)
        toolbar.addStretch()
        self.recover_button = self.button(toolbar, 'Recover…', self.recover)
        self.recover_button.hide()
        self.status = QLabel('Checking Docker Desktop… History is available without Docker.')
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status.setAccessibleName('Sandbox status')
        outer.addWidget(self.status)
        resource_bar = QHBoxLayout()
        resource_bar.addWidget(QLabel('Resource'))
        self.resource_choice = QComboBox()
        for key in ('cpu', 'memory', 'network'):
            self.resource_choice.addItem(key.upper() if key == 'cpu' else key.title(), key)
        self.resource_choice.setAccessibleName('Resource mode')
        resource_bar.addWidget(self.resource_choice)
        resource_bar.addWidget(QLabel('Demo backgrounds'))
        self.resource_backgrounds = QSpinBox()
        self.resource_backgrounds.setRange(1, 7)
        self.resource_backgrounds.setValue(3)
        self.resource_backgrounds.setAccessibleName('Resource demo backgrounds')
        resource_bar.addWidget(self.resource_backgrounds)
        self.link_mbps = QSpinBox()
        self.link_mbps.setRange(1, 100)
        self.link_mbps.setValue(20)
        self.link_mbps.setSuffix(' Mbit/s link')
        self.link_mbps.setAccessibleName('Loopback link bandwidth')
        resource_bar.addWidget(self.link_mbps)
        self.favor_button = self.button(resource_bar, 'Favor foreground', lambda: self.manual_allocation('favor'))
        self.restore_button = self.button(resource_bar, 'Restore allocation', lambda: self.manual_allocation('restore'))
        resource_bar.addStretch()
        outer.addLayout(resource_bar)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        playground = QWidget()
        play_layout = QVBoxLayout(playground)
        play_layout.setContentsMargins(0, 10, 0, 0)
        self.play_split = QSplitter(Qt.Orientation.Vertical)
        self.play_split.setChildrenCollapsible(False)
        play_layout.addWidget(self.play_split, 1)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.play_split.addWidget(split)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
        self.workload_pager = QWidget()
        pages = QHBoxLayout(self.workload_pager)
        pages.setContentsMargins(0, 0, 0, 0)
        self.workload_previous = self.button(pages, 'Previous workloads', lambda: self.history_page(-1))
        self.workload_page_label = QLabel()
        pages.addWidget(self.workload_page_label)
        self.workload_next = self.button(pages, 'Next workloads', lambda: self.history_page(1))
        pages.addStretch()
        self.workload_pager.hide()
        left_layout.addWidget(self.workload_pager)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(['Workload', 'Preset', 'State', 'CPU %', 'RSS MiB', 'Throughput', 'Priority'])
        # Qt Cocoa 6.11's selected-row accessibility traversal can invalidate
        # sibling cell interfaces. One selected cell still identifies a workload.
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(95)
        self.table.setMaximumHeight(180)
        self.table.setAccessibleName('Competing workloads')
        self.table.itemSelectionChanged.connect(self.show_details)
        left_layout.addWidget(self.table)
        self.cpu_chart = TimeSeriesChart('Shared CPU', '%')
        self.memory_chart = TimeSeriesChart('Resident memory', 'MiB')
        self.allocation_chart = TimeSeriesChart('Assigned allocation', 'MiB')
        self.network_chart = TimeSeriesChart('Delivered bandwidth', 'Mbit/s')
        self.metric_tabs = QTabWidget()
        self.metric_tabs.addTab(self.cpu_chart, 'CPU')
        self.metric_tabs.addTab(self.memory_chart, 'Memory')
        self.metric_tabs.addTab(self.allocation_chart, 'Allocation')
        self.metric_tabs.addTab(self.network_chart, 'Network')
        left_layout.addWidget(self.metric_tabs, 1)
        left_layout.addWidget(QLabel('CPU 100% = one logical CPU. Gaps mean missing or stale data. First 5 seconds are warm-up.'))
        split.addWidget(left)
        details = QWidget()
        right = QVBoxLayout(details)
        self.detail_title = QLabel('Workload details')
        right.addWidget(self.detail_title)
        form = QFormLayout()
        self.edit_name = QLineEdit()
        self.edit_name.setMaxLength(80)
        form.addRow('&Name', self.edit_name)
        self.edit_budget = QDoubleSpinBox()
        self.edit_budget.setRange(.1, 19.9)
        self.edit_budget.setSuffix(' ms')
        form.addRow('CPU &budget', self.edit_budget)
        self.edit_memory = QSpinBox()
        self.edit_memory.setRange(1, 128)
        self.edit_memory.setSuffix(' MiB')
        form.addRow('&Buffer', self.edit_memory)
        self.priority = QComboBox()
        for nice, label in PRIORITIES.items():
            self.priority.addItem(f'{label} (nice {nice})', nice)
        self.priority.currentIndexChanged.connect(self.change_priority)
        form.addRow('CPU &priority', self.priority)
        self.role = QComboBox()
        self.role.addItems(['Foreground', 'Background', 'Unmanaged'])
        self.role.currentIndexChanged.connect(self.update_policy_fields)
        form.addRow('Auto &role', self.role)
        self.importance = QComboBox()
        self.importance.addItems(['Low', 'Normal', 'High'])
        self.importance.currentIndexChanged.connect(self.update_policy_fields)
        form.addRow('&Importance', self.importance)
        right.addLayout(form)
        config_buttons = QHBoxLayout()
        self.save_button = self.button(config_buttons, 'Save settings', self.save_settings)
        self.remove_button = self.button(config_buttons, 'Remove', self.remove_selected)
        right.addLayout(config_buttons)
        detail_buttons = QHBoxLayout()
        self.run_button = self.button(detail_buttons, 'Start workload', self.start_selected)
        self.halt_button = self.button(detail_buttons, 'Stop workload', self.stop_selected)
        right.addLayout(detail_buttons)
        self.decision_panel = DecisionPanel()
        self.decision_panel.page_requested.connect(self.request_decision_page)
        self.decision_panel.entry_selected.connect(self.select_decision)
        auto_controls = self.decision_panel.controls
        self.policy_choice = QComboBox()
        for label, value in (('Manual', 'manual'), ('Auto: Rule', 'rule'), ('Shadow: Jev', 'shadow:jev'),
                             ('Shadow: Laya', 'shadow:laya'), ('Auto: Jev', 'jev'), ('Auto: Laya', 'laya')):
            self.policy_choice.addItem(label, value)
        self.policy_choice.setCurrentIndex(1)
        self.policy_choice.setAccessibleName('Decision policy')
        auto_controls.addWidget(self.policy_choice)
        self.auto_button = self.button(auto_controls, 'Auto: Rule', lambda: self.set_auto(not self.auto_active))
        self.controller_status = self.decision_panel.status_labels['active']
        self.metrics = QLabel('Select a workload to see its latest measurements.')
        self.metrics.setWordWrap(True)
        self.metrics.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        right.addWidget(self.metrics)
        self.throughput_chart = TimeSeriesChart('Selected throughput', 'hashes/s')
        self.latency_chart = TimeSeriesChart('p95 completion lateness', 'ms')
        self.metric_tabs.addTab(self.throughput_chart, 'Selected throughput')
        self.metric_tabs.addTab(self.latency_chart, 'Selected latency')
        # Cocoa's splitter can compress a tab below its hint and clip its chart.
        self.metric_tabs.setMinimumHeight(self.metric_tabs.minimumSizeHint().height())
        note = QLabel('Each run controls one resource. Memory uses workload cache capacity; Network uses an owned loopback link. CPU priorities stay fixed in these modes.')
        note.setWordWrap(True)
        right.addWidget(note)
        right.addStretch()
        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        detail_scroll.setMinimumWidth(270)
        detail_scroll.setMaximumWidth(365)
        detail_scroll.setWidget(details)
        split.addWidget(detail_scroll)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        self.play_split.addWidget(self.decision_panel)
        self.play_split.setSizes([400, 300])
        self.tabs.addTab(playground, 'Live playground')
        self.make_experiments_tab()
        self.make_history_tab()
        self.make_providers_tab()
        self.log_toggle = QPushButton('Show raw event / build log')
        self.log_toggle.setCheckable(True)
        outer.addWidget(self.log_toggle)
        self.events = QPlainTextEdit()
        self.events.setReadOnly(True)
        self.events.setMaximumBlockCount(500)
        self.events.setMaximumHeight(105)
        self.events.setAccessibleName('Event timeline and build log')
        outer.addWidget(self.events)
        self.events.hide()
        self.log_toggle.toggled.connect(self.events.setVisible)
        self.log_toggle.toggled.connect(lambda opened: self.log_toggle.setText(
            'Hide raw event / build log' if opened else 'Show raw event / build log'))
        self.statusBar().showMessage(f'Recordings: {worker.database}  |  Observational playground, not a causal benchmark')
        self.add_workload(workload('periodic'))
        self.add_workload(workload('cpu'))
        self.table.setCurrentCell(0, 0)
        self.resource_choice.currentIndexChanged.connect(self.select_resource)
        self.resource_backgrounds.valueChanged.connect(self.select_resource)
        self.laya_device.currentIndexChanged.connect(lambda: self.update_controls())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.pump)
        self.timer.start(100)
        QShortcut(QKeySequence.StandardKey.New, self, activated=self.add_dialog)
        QShortcut(QKeySequence.StandardKey.Quit, self, activated=self.close)
        self.update_controls()
        self.refresh_decision_activity()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'table'):
            compact = self.height() < 830
            self.heading.setVisible(not compact)
            self.intro.setVisible(not compact)
            self.table.setMinimumHeight(50 if compact else 95)

    def button(self, layout, text, callback):
        button = QPushButton(text)
        button.setAccessibleName(text)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def select_resource(self):
        if self.active or self.busy or self.readiness_run:
            self.resource_choice.blockSignals(True)
            self.resource_choice.setCurrentIndex(self.resource_choice.findData(self.resource))
            self.resource_choice.blockSignals(False)
            return
        self.unload_model()
        self.resource = self.resource_choice.currentData()
        self.policy = PolicyController(resource=self.resource, audit=lambda **event: self.audit(**event))
        self.history_mode = False
        self.workloads.clear()
        self.clear_charts()
        self.allocation_history.clear()
        if self.resource == 'cpu':
            self.add_workload(workload('periodic'))
            self.add_workload(workload('cpu'))
        else:
            count = self.resource_backgrounds.value() + 1
            for index in range(count):
                config = ({'mib': 512 // count // 16 * 16, 'working_set_mib': 512 // count // 16 * 16 + 48,
                           'seed': 20261008 + index}
                          if self.resource == 'memory' else {'share': 10000 // count, 'foreground': index == 0})
                row = workload('cache' if self.resource == 'memory' else 'network',
                               'Foreground' if index == 0 else f'Background {index}', config)
                row['role'] = 'Foreground' if index == 0 else 'Background'
                self.add_workload(row)
        self.allocation_chart.unit = '%' if self.resource == 'network' else 'MiB'
        self.metric_tabs.setCurrentWidget(self.cpu_chart if self.resource == 'cpu' else self.allocation_chart)
        self.table.setCurrentCell(0, 0)
        self.show_details()
        self.update_controls()

    def resource_task(self, row):
        allocation = row.get('allocated', row['config'].get('mib', row['config'].get('share')))
        return ResourceTask(row['id'], row.get('importance', 'Normal'), row.get('identity'),
                            row.get('original_allocation', allocation), allocation,
                            row.get('intervention_order'), row.get('sample', {}).get('cpu_pct'),
                            row.get('original_nice', 0), row['nice'])

    def manual_allocation(self, action):
        if self.resource == 'cpu' or not self.resource_supported or self.auto_active or self.pending:
            return
        foreground = next((row for row in self.workloads.values() if row.get('role') == 'Foreground' and row['status'] == 'running'), None)
        if foreground:
            decision = allocation_decision(action, self.current_model_tasks(), self.resource_task(foreground), self.resource)
            self.request_allocation(decision, source='manual')

    def request_allocation(self, decision, *, source, cycle_id=None):
        if decision.action not in ('favor', 'restore') or self.pending or not self.active or self.history_mode:
            return None
        foreground = next((r for r in self.workloads.values() if r.get('role') == 'Foreground'), None)
        if not foreground:
            return None
        request_id = str(uuid.uuid4())
        command = dict(v=VERSION, request_id=request_id, op='memory_budget' if self.resource == 'memory' else 'network_share',
                       foreground_id=foreground['id'], changes=list(decision.changes))
        try:
            self.worker.submit({'op': 'control', 'command': command})
        except queue.Full:
            self.status.setText('Allocation queue is full; control disabled.')
            self.auto_active = False
            self.policy.stop()
            self.action('stop_session')
            return None
        self.pending[request_id] = {**command, 'workload_id': decision.target, 'source': source,
                                   'cycle_id': cycle_id or f'control:{request_id}', 'allocation_action': decision.action,
                                   'before_metrics': self.allocation_metrics()}
        self.audit('priority_requested', request_id=request_id, source=source,
                   cycle_id=self.pending[request_id]['cycle_id'], changes=list(decision.changes),
                   workload_id=decision.target, action=decision.action)
        self.update_controls()
        return request_id

    def allocation_metrics(self):
        fields = ('mono_ns', 'miss_rate', 'p95_latency_ms', 'rss_mib', 'app_rate', 'allocated',
                  'cache_hits', 'cache_misses', 'queue_delay_ms', 'failed_transfers')
        return {key: {field: row.get('sample', {}).get(field) for field in fields}
                for key, row in self.workloads.items() if row['status'] == 'running'}

    def restore_resource_next(self):
        if not self.resource_restoring or self.pending:
            return
        foreground = next((r for r in self.workloads.values() if r.get('role') == 'Foreground' and r['status'] == 'running'), None)
        if foreground:
            decision = allocation_decision('restore', self.current_model_tasks(), self.resource_task(foreground), self.resource)
            request_id = self.request_allocation(decision, source='restoration')
            if request_id:
                self.pending[request_id]['restoration'] = True
                return
        self.resource_restoring = False
        self.finish_deferred_stop()

    def handle_allocation_ack(self, message):
        request = self.pending.pop(message.get('request_id'), None)
        if not request:
            return  # Duplicate/late acknowledgement never grants new authority.
        changes = request['changes']
        at = message.get('mono_ns')
        matched = (message.get('ok') is True and message.get('op') == request['op'] and
                   message.get('session_id', self.session_id) == self.session_id and
                   message.get('changes') == changes and type(at) is int and at >= 0 and
                   message.get('confirmed') == {c['workload_id']: c['requested'] for c in changes} and
                   all(c['workload_id'] in self.workloads and self.workloads[c['workload_id']].get('identity') == c['identity'] for c in changes))
        if matched and request.get('automatic'):
            matched = self.policy.acknowledge_allocation(request['request_id'], changes, at / 1e9)
        self.audit('priority_outcome', cycle_id=request['cycle_id'], request_id=request['request_id'],
                   source=request['source'], confirmed=bool(matched), changes=changes,
                   acknowledged_at=at / 1e9 if matched else None,
                   reason='' if matched else message.get('error', 'Allocation acknowledgement mismatch'))
        if not matched:
            self.resource_supported = self.auto_active = self.resource_restoring = False
            self.policy.stop()
            self.action('stop_session')
            return
        if self.experiment and self.experiment.get('phase') == 'measurement':
            self.experiment['interventions'] += 1
            self.experiment['intervention_targets'].append(request['workload_id'])
            self.experiment['intervention_importances'].append(self.workloads[request['workload_id']]['importance'])
            self.experiment.setdefault('intervention_events', []).append(dict(
                action=request['allocation_action'], provider=request['source'], request_id=request['request_id'],
                acknowledged_at_ns=at, changes=changes, resource=self.resource))
        for change in changes:
            self.workloads[change['workload_id']]['allocated'] = change['requested']
        self.resource_effects.append(dict(request_id=request['request_id'], cycle_id=request['cycle_id'],
                                         acknowledged_at_ns=at, before=request['before_metrics']))
        self.resource_effects = self.resource_effects[-20:]
        target = request['workload_id']
        if request['allocation_action'] == 'favor':
            self.allocation_history.append(target)
        else:
            for index in range(len(self.allocation_history) - 1, -1, -1):
                if self.allocation_history[index] == target:
                    self.allocation_history.pop(index)
                    break
        for key, row in self.workloads.items():
            row['intervention_order'] = max((i + 1 for i, k in enumerate(self.allocation_history) if k == key), default=None)
        self.restore_resource_next()
        self.finish_deferred_stop()
        self.advance_experiment()
        self.refresh_table()
        self.show_details(reset_fields=False)
        self.refresh_decision_activity()

    @property
    def rule_state(self):
        return self.policy.state

    @rule_state.setter
    def rule_state(self, value):
        self.policy.state = value

    def make_providers_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self.jev_key = QLineEdit()
        self.jev_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.jev_key.setMaxLength(4096)
        self.jev_key.setAccessibleName('Jev API key')
        self.jev_key.setPlaceholderText('Not saved by the app')
        form.addRow('Jev API key', self.jev_key)
        self.laya_device = QComboBox()
        self.laya_device.addItem('Apple GPU (MPS)', 'mps')
        self.laya_device.addItem('CPU (separate validation)', 'cpu')
        form.addRow('Laya device', self.laya_device)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        self.jev_test = self.button(buttons, 'Test Jev connection', lambda: self.prepare_provider('jev', True))
        self.jev_validate = self.button(buttons, 'Validate Jev readiness', lambda: self.prepare_provider('jev'))
        self.laya_load = self.button(buttons, 'Load Laya', lambda: self.prepare_provider('laya', True))
        self.laya_validate = self.button(buttons, 'Validate Laya readiness', lambda: self.prepare_provider('laya'))
        layout.addLayout(buttons)
        self.provider_status = QLabel('Jev: key required. Laya: load the bundled checkpoint before workloads. '
                                      'Live model control requires 100 validated shadow contexts and session preflight.')
        self.provider_status.setWordWrap(True)
        self.provider_status.setAccessibleName('Provider readiness')
        layout.addWidget(self.provider_status)
        self.helper_status = QLabel('Helper: unloaded')
        self.helper_status.setAccessibleName('Helper loaded status')
        self.readiness_status = QLabel()
        self.readiness_status.setWordWrap(True)
        self.readiness_status.setAccessibleName('Full resource readiness')
        layout.addWidget(self.helper_status)
        layout.addWidget(self.readiness_status)
        note = QLabel('Jev receives anonymous synthetic metrics only. Each decision has a one-second deadline. '
                      'Two failed replies switch to Rule until re-enabled. Shadow proposals do not change resources. '
                      'Readiness measures safe operation and latency; it does not establish performance improvement.')
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        self.tabs.addTab(page, 'Providers')
        evidence = self.worker.database.parent / 'model-readiness'
        self.readiness.update(latest_reports(evidence))
        self.refresh_provider_state()

    def refresh_provider_state(self):
        if not hasattr(self, 'helper_status'):
            return
        loaded = bool(self.loaded_provider and self.model_helper and self.model_helper.ready)
        self.helper_status.setText(f'Helper: {self.loaded_provider.title()} loaded' if loaded
                                   else 'Helper: unloaded' if not self.model_helper else 'Helper: loading or unavailable')
        states = []
        for provider in ('jev', 'laya'):
            fingerprint = self.provider_fingerprint(provider)
            report = self.readiness.get(fingerprint, {})
            state = ('live readiness passed' if self.model_is_ready(provider) else
                     'Shadow only—latency gate failed' if report and not readiness_matches(report, fingerprint) else
                     'Shadow only—native checks pending' if report else 'Shadow only—full readiness required')
            states.append(f'{provider.title()} / {self.resource.title()}: {state}')
        self.readiness_status.setText('\n'.join(states))

    def provider_fingerprint(self, provider):
        return configuration_fingerprint(provider, self.laya_device.currentData() if provider == 'laya' else 'hosted', self.resource)

    def model_is_ready(self, provider):
        fingerprint = self.provider_fingerprint(provider)
        return live_readiness_matches(self.readiness.get(fingerprint, {}), fingerprint, provider)

    def live_model_allowed(self, provider):
        return self.model_is_ready(provider) or (
            self.allow_unqualified_jev_demo and provider == 'jev'
            and self.resource in {'memory', 'network'}
            and not self.experiment and not self.history_mode)

    def unload_model(self):
        if self.model_helper:
            self.model_helper.close()
        self.model_helper = self.loaded_provider = self.loaded_fingerprint = None
        self.policy.helper = None
        self.provider_status.setText('Connection: no active helper; helper unloaded.')
        self.refresh_provider_state()

    def prepare_provider(self, provider, connection_only=False):
        if self.auto_active or any(row['status'] == 'running' for row in self.workloads.values()) or self.readiness_run:
            self.provider_status.setText('Stop workloads before changing or validating providers.')
            return
        self.unload_model()
        command, checkpoint = runtime_paths()
        config = {'provider': provider}
        if provider == 'jev':
            if not self.jev_key.text().strip():
                self.provider_status.setText('Jev requires an API key entered above.')
                return
            config['key'] = self.jev_key.text().strip()
        else:
            config.update(checkpoint=str(checkpoint), device=self.laya_device.currentData(),
                          support=str(self.worker.database.parent / 'models'),
                          warmups=[frozen_contexts(self.resource)[i] for i in (35, 71, 107)])
        try:
            self.model_helper = HelperProcess(command, config)
            self.loaded_provider = provider
            self.loaded_fingerprint = self.provider_fingerprint(provider)
            self.readiness_run = ReadinessRun(self.model_helper, provider,
                self.laya_device.currentData() if provider == 'laya' else 'hosted',
                resource=self.resource, connection_only=connection_only)
            self.provider_status.setText(f'{provider.title()}: loading and warming…')
        except (OSError, ValueError):
            self.provider_status.setText(f'{provider.title()}: helper unavailable; rebuild the app runtime.')
        self.update_controls()

    def current_model_tasks(self):
        if self.resource != 'cpu':
            return [self.resource_task(row) for row in self.workloads.values()
                    if row.get('role') == 'Background' and row['status'] == 'running']
        tasks = []
        for row in self.workloads.values():
            if row.get('role') == 'Background' and row['status'] == 'running':
                value = row.get('sample', {}).get('cpu_pct')
                if value is None and row.get('series'):
                    value = row['series'][-1][1].get('cpu_pct')
                tasks.append(ManagedTask(row['id'], row['importance'], value,
                    row.get('original_nice', row['nice']), row['nice'], row['identity'], row.get('intervention_order')))
        return tasks

    def poll_model(self):
        if self.readiness_run:
            try:
                self.readiness_run.poll()
            except (OSError, ValueError, RuntimeError):
                self.readiness_run.rows.append({'valid': False, 'error': 'helper_failed', 'audited': True})
                self.readiness_run.finish()
            run = self.readiness_run
            self.provider_status.setText(f'{self.loaded_provider.title()}: {len(run.rows)}/{len(run.contexts)} contexts checked')
            if run.done:
                report = run.report
                try:
                    path = run.save(self.worker.database.parent / 'model-readiness')
                    if len(run.contexts) >= 100:
                        self.readiness[run.fingerprint] = report
                    connection = ('connection passed' if report['valid_in_deadline'] else 'connection unavailable')
                    self.provider_status.setText(f'{self.loaded_provider.title()}: '
                        + ((connection + ' • ') if len(run.contexts) == 1 else '')
                        + ('live readiness passed' if self.model_is_ready(self.loaded_provider)
                           else 'shadow only; live readiness not passed')
                        + f' • {report["valid_in_deadline"]}/{report["contexts"]} within 1 s • {path.name}')
                except OSError:
                    if len(run.contexts) >= 100:
                        self.readiness.pop(run.fingerprint, None)
                    self.provider_status.setText('Could not save readiness evidence; live control stays locked.')
                self.readiness_run = None
                self.update_controls()
                if self.experiment and self.experiment.get('phase') == 'model_loading':
                    if self.model_helper.ready and report['valid_in_deadline']:
                        self.start_trial_workloads()
                    else:
                        self.experiment['cancelled'] = True
                        self.experiment['interruption_reason'] = 'provider preload failed before warm-up'
                        self.finish_experiment()
            return
        if self.auto_active and (self.policy.outstanding or self.policy.expired):
            foreground = next((row for row in self.workloads.values() if row.get('role') == 'Foreground'), None)
            valid = bool(foreground and foreground['status'] == 'running' and not foreground['stale'] and
                         all(not row['stale'] for row in self.workloads.values() if row.get('role') == 'Background'))
            decision = self.policy.poll(self.current_model_tasks(),
                (foreground['id'], foreground.get('identity')) if foreground else (None, None), telemetry_valid=valid)
            if decision:
                self.apply_policy_decision(decision)
        elif self.model_helper:
            try:
                self.model_helper.poll()
            except (OSError, ValueError, RuntimeError):
                self.provider_status.setText('Model helper unavailable; Rule remains available.')
        self.refresh_provider_state()

    def apply_policy_decision(self, decision):
        if decision.action == 'restore_all':
            self.set_auto(False)
        elif self.resource != 'cpu' and decision.action in ('favor', 'restore'):
            source = self.policy.requested if self.policy.model_request_id else 'rule'
            request_id = self.request_allocation(decision, source=source, cycle_id=self.policy.cycle_id)
            if request_id:
                self.pending[request_id]['automatic'] = decision.action
                self.policy.command_sent(request_id, decision)
        elif decision.action in ('lower', 'restore') and not any(item.get('op') == 'priority' for item in self.pending.values()):
            source = self.policy.requested if self.policy.model_request_id else 'rule'
            request_id = self.request_priority(decision.target, decision.nice,
                                               source=source, cycle_id=self.policy.cycle_id)
            if request_id:
                self.pending[request_id].update(automatic=decision.action, decision_reason=decision.reason,
                    decision_time=self.policy.window.end, decision_provider=source)
                self.policy.command_sent(request_id, decision)
        self.refresh_decision_activity()

    def refresh_decision_activity(self):
        panel = self.decision_panel
        panel.origin_ns = self.origin_ns
        panel.names.update({key: row['name'] for key, row in self.workloads.items()})
        foreground = next((row for row in self.workloads.values() if row.get('role') == 'Foreground'), {})
        stale = any(row.get('stale') or row['status'] != 'running' for row in self.workloads.values()
                    if row.get('role') in ('Foreground', 'Background'))
        status = policy_status(self.policy, now=time.monotonic(), selected=self.policy_choice.currentData(),
            running=self.active, history=self.history_mode, anchor=self.clock_anchor,
            telemetry_receipt=foreground.get('receipt_ns'), telemetry_stale=stale)
        if not self.history_mode and not self.policy.active:
            supported = self.priority_supported if self.resource == 'cpu' else self.resource_supported
            status['explanation'] = ('Auto disabled: ' if self.preflight_complete and not supported else '') + self.preflight_reason
        panel.update_status(status)
        panel.render()

    def request_decision_page(self, cursor):
        if not self.session_id:
            self.decision_panel.note.setText('Start a sandbox or open a recording to view saved decisions.')
            return
        token = uuid.uuid4().hex
        self.decision_page_token = (token, not cursor)
        try:
            self.worker.submit({'op': 'decision_history', 'session_id': self.session_id, 'token': token, **cursor})
        except queue.Full:
            self.decision_panel.note.setText('History queue busy · try again after the next update.')

    def select_decision(self, entry):
        chart = self.cpu_chart if self.resource == 'cpu' else self.allocation_chart
        at = entry.acknowledged_at if entry else None
        if at is None or self.origin_ns is None:
            chart.selected_marker = None
            chart.update()
            self.decision_panel.note.setText('No confirmed change to highlight for this decision.')
            return
        seconds = at - self.origin_ns / 1e9
        chart.selected_marker = (seconds, 'Selected confirmed change')
        chart.update()
        start, end = chart.visible_range()
        self.decision_panel.note.setText(f'Confirmed change highlighted on {self.resource.title()} · observed effects are not causal proof.'
            if start <= seconds <= end else 'Confirmed change is outside the visible chart range.')

    def make_history_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        label = QLabel('Saved recordings stay on this Mac. Open charts and events without Docker. Exports contain all raw records, not just displayed points.')
        label.setWordWrap(True)
        layout.addWidget(label)
        self.history = QListWidget()
        self.history.setAccessibleName('Saved recordings')
        self.history.itemDoubleClicked.connect(self.open_history)
        layout.addWidget(self.history)
        buttons = QHBoxLayout()
        self.history_refresh = self.button(buttons, 'Refresh recordings', lambda: self.action('history', busy=False))
        self.history_open = self.button(buttons, 'Open recording', self.open_history)
        self.json_export = self.button(buttons, 'Export JSON…', lambda: self.export('json'))
        self.csv_export = self.button(buttons, 'Export CSV…', lambda: self.export('csv'))
        self.new_session = self.button(buttons, 'New playground', self.new_playground)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.tabs.addTab(page, 'Recordings')

    def make_experiments_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self.experiment_kind = QComboBox()
        self.experiment_kind.addItem('Responsiveness comparison', 'responsiveness')
        self.experiment_kind.addItem('Priority demonstration', 'priority')
        self.experiment_kind.addItem('Seven-background control coverage', 'coverage')
        self.experiment_kind.addItem('Calibration sweep', 'calibration')
        self.experiment_kind.addItem('Shadow: Jev (fixed nice 0)', 'shadow_jev')
        self.experiment_kind.addItem('Shadow: Laya (fixed nice 0)', 'shadow_laya')
        self.experiment_kind.addItem('Held-out calibration (6 ms)', 'heldout_calibration')
        self.experiment_kind.addItem('Five-policy model comparison (50 trials)', 'model_comparison')
        self.experiment_kind.addItem('Resource calibration (Memory / Network)', 'resource_calibration')
        self.experiment_kind.addItem('Resource comparison (40 trials)', 'resource_comparison')
        form.addRow('&Experiment', self.experiment_kind)
        self.experiment_backgrounds = QSpinBox()
        self.experiment_backgrounds.setRange(1, 7)
        self.experiment_backgrounds.setValue(3)
        form.addRow('&Backgrounds', self.experiment_backgrounds)
        self.experiment_budget = QDoubleSpinBox()
        self.experiment_budget.setRange(2, 19.9)
        self.experiment_budget.setValue(10)
        self.experiment_budget.setSuffix(' ms / 20 ms')
        form.addRow('Foreground &budget', self.experiment_budget)
        self.experiment_importance = QComboBox()
        self.experiment_importance.addItems(['Low', 'Normal', 'High'])
        form.addRow('Background &importance', self.experiment_importance)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        self.experiment_start = self.button(buttons, 'Start experiment', self.start_experiment)
        self.experiment_cancel = self.button(buttons, 'Cancel experiment', self.cancel_experiment)
        self.experiment_export = self.button(buttons, 'Export results…', self.export_experiment)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.experiment_progress = QLabel('Ready. Trials use 5 s warm-up and 180 s measurement windows.')
        self.experiment_progress.setWordWrap(True)
        layout.addWidget(self.experiment_progress)
        self.experiment_results = QPlainTextEdit()
        self.experiment_results.setReadOnly(True)
        self.experiment_results.setPlaceholderText('Completed trial results appear here.')
        layout.addWidget(self.experiment_results, 1)
        note = QLabel('Trials restart workload processes, retain full-resolution records, and use a fixed seeded block order. '
                      'The separate calibration manifest covers 2–16 ms budgets with three solo and paired repetitions.')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.tabs.addTab(page, 'Experiments')
        self.experiment_cancel.setEnabled(False)
        self.experiment_export.setEnabled(False)

    def action(self, op, busy=True, **fields):
        try:
            self.worker.submit({'op': op, **fields})
            if busy:
                self.busy = True
                self.status.setText({'prepare': 'Preparing sandbox image…', 'start_session': 'Starting one Linux sandbox…',
                                     'stop_session': 'Stopping workloads and saving recording…'}.get(op, 'Working…'))
            self.update_controls()
        except queue.Full:
            self.status.setText('Too many pending actions. Wait for the current operation to finish.')

    def start_experiment(self):
        if self.active or self.busy or (self.experiment and self.experiment.get('phase') != 'done'):
            self.status.setText('Stop the current sandbox before starting an experiment.')
            return
        self.experiment = None
        kind = self.experiment_kind.currentData()
        if kind.startswith('resource_'):
            self.start_resource_experiment(kind)
            return
        if self.resource != 'cpu':
            self.status.setText('Select a resource calibration/comparison for Memory or Network mode.')
            return
        if kind == 'model_comparison' and (not all(self.model_is_ready(p) for p in ('jev', 'laya')) or
                not self.qualified_config or any(self.qualified_config.get(key) != value for key, value in
                    {'budget_ms': 6, 'backgrounds': self.experiment_backgrounds.value(),
                     'importance': self.experiment_importance.currentText()}.items())):
            self.status.setText('Model comparison requires both providers ready and passing held-out 6 ms calibration.')
            return
        if kind in ('model_comparison', 'shadow_jev') and not self.jev_key.text().strip():
            self.status.setText('Enter the Jev key in Providers before starting this experiment.')
            return
        profiled = kind in ('responsiveness', 'calibration', 'heldout_calibration', 'model_comparison', 'shadow_jev', 'shadow_laya')
        backgrounds = (7 if kind == 'coverage' else
                       (self.experiment_backgrounds.value() if profiled else 1))
        self.workloads.clear()
        self.clear_charts()
        if profiled or kind == 'coverage':
            self.add_workload(workload('periodic', 'Protected foreground',
                {'budget_ms': 19.9 if kind == 'coverage' else (6 if kind in ('heldout_calibration', 'model_comparison')
                                                            else self.experiment_budget.value())}))
        else:
            competitor = workload('cpu', 'Competitor (nice 0)')
            competitor['role'] = 'Unmanaged'
            self.add_workload(competitor)
        for index in range(backgrounds):
            row = workload('cpu', f'Background {index + 1}')
            importance = (('Low', 'Low', 'Low', 'Normal', 'Normal', 'High', 'High')[index]
                          if kind == 'coverage' else self.experiment_importance.currentText())
            row.update(role='Background', importance=importance)
            self.add_workload(row)
        self.table.setCurrentCell(0, 0)
        self.show_details()
        config = {'budget_ms': 19.9 if kind == 'coverage' else (6 if kind in ('heldout_calibration', 'model_comparison')
                                                            else self.experiment_budget.value()),
                  'backgrounds': backgrounds, 'importance': self.experiment_importance.currentText(),
                  'provider_fingerprints': {p: self.provider_fingerprint(p) for p in ('jev', 'laya')},
                  'device': self.laya_device.currentData(), 'deadline_ms': 1000}
        self.experiment = {'kind': kind, 'trials': trial_plan(kind), 'index': -1,
                           'phase': 'session', 'results': [], 'samples': [],
                           'raw_ticks': [], 'config': config,
                           'cancelled': False, 'interventions': 0, 'intervention_targets': [],
                           'intervention_importances': [],
                           'seed': 20260930}
        self.experiment_results.clear()
        self.experiment_cancel.setEnabled(True)
        self.experiment_start.setEnabled(False)
        self.experiment_export.setEnabled(False)
        self.experiment_progress.setText('Starting sandbox and mandatory reversibility preflight…')
        self.start_session()
        self.tabs.setCurrentIndex(1)

    def start_resource_experiment(self, kind):
        if self.resource == 'cpu':
            self.status.setText('Select Memory or Network before this experiment.')
            return
        if kind == 'resource_comparison' and (not self.model_is_ready('jev') or not self.jev_key.text().strip()
                                             or self.resource not in self.qualified_resources):
            self.status.setText('Resource comparison requires frozen calibration and Jev readiness for this resource; enter the Jev key in Providers.')
            return
        self.resource_backgrounds.setValue(self.experiment_backgrounds.value())
        self.select_resource()
        for row in self.workloads.values():
            if row['role'] == 'Background':
                row['importance'] = self.experiment_importance.currentText()
        configs = [dict(preset=row['preset'], role=row['role'], config=dict(row['config'])) for row in self.workloads.values()]
        config = dict(resource=self.resource, backgrounds=self.resource_backgrounds.value(),
                      importance=self.experiment_importance.currentText(), link_mbps=self.link_mbps.value(),
                      workload_configs=configs, provider_fingerprints={p: self.provider_fingerprint(p) for p in ('jev', 'laya')})
        config['profiles'] = ({'light': {'extra_mib': 0}, 'contention': {'extra_mib': 16}} if self.resource == 'memory'
                              else {'light': {'response_kib': 16}, 'contention': {'response_kib': 64}})
        self.experiment = dict(kind=kind, trials=trial_plan(kind), index=-1, phase='session', results=[],
            samples=[], raw_ticks=[], config=config, cancelled=False, interventions=0,
            intervention_targets=[], intervention_importances=[], seed=20260930)
        self.experiment_results.clear()
        self.experiment_progress.setText('Starting resource experiment; CPU priorities remain fixed.')
        self.start_session()
        self.tabs.setCurrentIndex(1)

    def experiment_next_trial(self):
        experiment = self.experiment
        if not experiment or experiment['cancelled']:
            self.finish_experiment()
            return
        experiment['index'] += 1
        if experiment['index'] >= len(experiment['trials']):
            self.finish_experiment()
            return
        experiment.update(phase='starting', samples=[], raw_ticks=[], raw_records=[], alignment_start_ns=None, interventions=0,
                          intervention_targets=[], intervention_importances=[], intervention_events=[],
                          decision_window_ids=set(), fallback_window_ids=set(), inference_events=[])
        trial = experiment['trials'][experiment['index']]
        if experiment['kind'].startswith('resource_'):
            profile = experiment['config']['profiles'][trial['profile']]
            for row in self.workloads.values():
                if self.resource == 'memory':
                    row['config']['working_set_mib'] = row['config']['mib'] + profile['extra_mib']
                elif row['role'] == 'Foreground':
                    row['config']['response_kib'] = profile['response_kib']
        if experiment['kind'] in ('calibration', 'heldout_calibration'):
            foreground = next(row for row in self.workloads.values() if row.get('role') == 'Foreground')
            foreground['config'] = {'budget_ms': trial['budget_ms']}
        self.experiment_progress.setText(
            f'Trial {experiment["index"] + 1}/{len(experiment["trials"])} • block {trial["block"] + 1} • '
            f'{trial["arm"]} • starting fresh workload processes')
        if trial['arm'] in ('jev', 'laya', 'shadow:jev', 'shadow:laya'):
            experiment['phase'] = 'model_loading'
            self.prepare_provider(trial['arm'].split(':')[-1], connection_only=True)
            return
        self.unload_model()
        self.start_trial_workloads()

    def start_trial_workloads(self):
        experiment = self.experiment
        experiment['phase'] = 'starting'
        trial = experiment['trials'][experiment['index']]
        active_ids = []
        for key, row in self.workloads.items():
            profiled = experiment['kind'] in ('calibration', 'responsiveness', 'heldout_calibration',
                                              'model_comparison', 'shadow_jev', 'shadow_laya')
            selected = (not profiled or row.get('role') == 'Foreground' or
                        (trial.get('profile') == 'light' and row['name'] == 'Background 1') or
                        trial.get('profile') == 'contention')
            if selected and row['status'] != 'running':
                active_ids.append(key)
                self.control('start', key)
            elif selected:
                active_ids.append(key)
        experiment['active_trial_ids'] = active_ids
        self.advance_experiment()

    def advance_experiment(self):
        experiment = self.experiment
        if not experiment or self.pending:
            return
        phase = experiment['phase']
        if phase == 'starting' and all(self.workloads[key]['status'] == 'running'
                                       for key in experiment.get('active_trial_ids', self.workloads)):
            experiment['phase'] = 'configuring'
            trial = experiment['trials'][experiment['index']]
            arm = trial['arm']
            active = set(experiment.get('active_trial_ids', ()))
            backgrounds = [(key, row) for key, row in self.workloads.items()
                           if key in active and row.get('role') == 'Background']
            if experiment['kind'] == 'priority':
                if arm:
                    self.request_priority(backgrounds[0][0], arm)
            elif arm == 'manual10':
                for key, _ in backgrounds:
                    self.request_priority(key, 10)
            elif arm == 'manual' and self.resource != 'cpu':
                self.manual_allocation('favor')
            elif arm in ('rule', 'jev', 'laya', 'shadow:jev', 'shadow:laya'):
                self.policy_choice.setCurrentIndex(self.policy_choice.findData(arm))
                self.set_auto(True)
                if not self.auto_active:
                    experiment['cancelled'] = True
                    experiment['interruption_reason'] = 'policy could not be enabled after preflight'
                    self.finish_experiment()
                    return
            if not self.pending:
                self.begin_experiment_trial()
        elif phase == 'configuring':
            self.begin_experiment_trial()
        elif phase == 'restoring':
            self.stop_experiment_trial()
        elif phase == 'stopping' and all(row['status'] != 'running' for row in self.workloads.values()):
            if experiment['kind'] == 'coverage' and experiment['results']:
                result = experiment['results'][-1]
                result['restoration_verified'] = all(
                    row['nice'] == row.get('original_nice', row['nice']) for row in self.workloads.values())
                importance_rank = {'Low': 0, 'Normal': 1, 'High': 2}
                order = [importance_rank[value] for value in result['intervention_importances']]
                result['control_coverage_passed'] = (result['valid'] and
                    len(set(result['distinct_intervention_targets'])) == 7 and order == sorted(order) and
                    result['restoration_verified'])
            self.experiment_next_trial()

    def begin_experiment_trial(self):
        if not self.experiment:
            return
        trial = self.experiment['trials'][self.experiment['index']]
        now = time.monotonic()
        self.experiment.update(phase='warmup', warmup_end=now + trial['warmup_seconds'])
        self.audit('trial_boundary', boundary='warmup_start', trial_index=self.experiment['index'], trial=trial)

    def tick_experiment(self):
        experiment = self.experiment
        if not experiment:
            return
        now = time.monotonic()
        if experiment['phase'] == 'warmup':
            remaining = max(0, experiment['warmup_end'] - now)
            self.experiment_progress.setText(f'Trial {experiment["index"] + 1}/{len(experiment["trials"])} • warm-up {remaining:.1f} s')
            if now >= experiment['warmup_end']:
                duration = experiment['trials'][experiment['index']]['measurement_seconds']
                foreground = next((row for row in self.workloads.values()
                                   if row.get('role') == 'Foreground'), None)
                raw = (foreground or {}).get('raw')
                if foreground and not raw:
                    self.experiment_progress.setText('Waiting for foreground tick alignment…')
                    return
                if foreground and experiment.get('alignment_start_ns') is None:
                    elapsed = max(0, raw['mono_ns'] - raw['start_ns'])
                    experiment['alignment_start_ns'] = (raw['start_ns'] +
                        ((elapsed + 4_999_999_999) // 5_000_000_000) * 5_000_000_000)
                if foreground and raw['mono_ns'] < experiment['alignment_start_ns']:
                    self.experiment_progress.setText('Aligning to the next complete five-second boundary…')
                    return
                if foreground:
                    start_ns = experiment['alignment_start_ns']
                else:
                    active = [self.workloads[key].get('sample') for key in experiment['active_trial_ids']]
                    if not active or any(not sample or sample.get('mono_ns') is None for sample in active):
                        self.experiment_progress.setText('Waiting for workload telemetry…')
                        return
                    start_ns = max(sample['mono_ns'] for sample in active)
                end_ns = start_ns + duration * 1_000_000_000
                experiment.update(phase='measurement', measurement_end=now + duration,
                                  raw_ticks=[tick for tick in experiment['raw_ticks']
                                             if start_ns <= tick['scheduled_ns'] < end_ns],
                                  measurement_start_ns=start_ns,
                                  measurement_end_ns=end_ns,
                                  controller_cpu_start_ns=time.process_time_ns())
                self.audit('trial_boundary', boundary='measurement_start', trial_index=experiment['index'],
                           measurement_start_ns=start_ns, measurement_end_ns=end_ns)
        elif experiment['phase'] == 'measurement':
            remaining = max(0, experiment['measurement_end'] - now)
            self.experiment_progress.setText(f'Trial {experiment["index"] + 1}/{len(experiment["trials"])} • measurement {remaining:.0f} s remaining')
            if now >= experiment['measurement_end']:
                experiment.update(phase='drain', drain_end=now + 8,
                                  controller_cpu_end_ns=time.process_time_ns())
                if self.auto_active:
                    self.set_auto(False)
        elif experiment['phase'] == 'drain':
            self.experiment_progress.setText('Waiting for the final complete telemetry window…')
            if now >= experiment['drain_end']:
                self.complete_experiment_trial()

    def complete_experiment_trial(self):
        experiment = self.experiment
        trial = experiment['trials'][experiment['index']]
        active = set(experiment.get('active_trial_ids', ()))
        background_ids = {key for key, row in self.workloads.items() if key in active and
                          (row.get('role') == 'Background' or experiment['kind'] == 'priority')}
        metrics = summarize_trial(None if experiment['kind'] == 'priority' or self.resource != 'cpu' else experiment['raw_ticks'],
                                  experiment['samples'], background_ids,
                                  experiment['measurement_start_ns'], experiment['measurement_end_ns'],
                                  raw_records=experiment.get('raw_records', []))
        if self.resource != 'cpu':
            foreground = next(row for row in self.workloads.values() if row['role'] == 'Foreground')
            metrics = summarize_resource(experiment.get('raw_records', []), experiment['samples'], foreground['id'],
                background_ids, experiment['measurement_start_ns'], experiment['measurement_end_ns'], self.resource)
        controller_cpu_seconds = (experiment['controller_cpu_end_ns'] -
                                  experiment['controller_cpu_start_ns']) / 1e9
        result = {**trial, **metrics,
            'controller_cpu_seconds': controller_cpu_seconds,
            'controller_cpu_pct': 100 * controller_cpu_seconds / trial['measurement_seconds'],
            'interventions': experiment['interventions'],
            'distinct_intervention_targets': list(experiment['intervention_targets']),
            'intervention_importances': list(experiment['intervention_importances'])}
        result.update(decision_windows=len(experiment.get('decision_window_ids', ())),
                      fallback_windows=len(experiment.get('fallback_window_ids', ())),
                      intervention_events=experiment.get('intervention_events', []),
                      inference_overhead=inference_summary(experiment.get('inference_events', []),
                          getattr(self.model_helper, 'metadata', {}).get('host_cold_start_ms')))
        experiment['results'].append(result)
        self.audit('trial_boundary', boundary='measurement_end', trial_index=experiment['index'], result=result,
                   measurement_start_ns=experiment['measurement_start_ns'],
                   measurement_end_ns=experiment['measurement_end_ns'])
        experiment['phase'] = 'restoring'
        if self.resource != 'cpu':
            self.set_auto(False)
            if not self.pending:
                self.stop_experiment_trial()
            return
        if self.auto_active:
            self.set_auto(False)
        for key, row in self.restoration_targets():
            original = row.get('original_nice', 0)
            if row['status'] == 'running' and row['nice'] != original and not any(
                    item.get('workload_id') == key for item in self.pending.values()):
                request_id = self.request_priority(key, original, source='restoration')
                if request_id:
                    self.pending[request_id]['restoration'] = True
        if not self.pending:
            self.stop_experiment_trial()

    def stop_experiment_trial(self):
        if not self.experiment:
            return
        self.experiment['phase'] = 'stopping'
        for key, row in self.workloads.items():
            if row['status'] == 'running':
                self.control('stop', key)
        self.advance_experiment()

    def cancel_experiment(self):
        if not self.experiment:
            return
        self.experiment['cancelled'] = True
        self.experiment['phase'] = 'restoring'
        if self.resource != 'cpu':
            self.set_auto(False)
            if not self.pending:
                self.stop_experiment_trial()
            return
        if self.auto_active:
            self.set_auto(False)
        for key, row in self.restoration_targets():
            original = row.get('original_nice', 0)
            if row['status'] == 'running' and row['nice'] != original and not any(
                    item.get('workload_id') == key for item in self.pending.values()):
                request_id = self.request_priority(key, original, source='restoration')
                if request_id:
                    self.pending[request_id]['restoration'] = True
        if not self.pending:
            self.stop_experiment_trial()
        self.experiment_progress.setText('Cancelling: restoring priorities and stopping owned workloads…')

    def finish_experiment(self):
        if not self.experiment:
            return
        experiment = self.experiment
        report = {'schema_version': 2, 'report_version': 2, 'kind': experiment['kind'], 'seed': experiment['seed'],
                  'configuration': experiment.get('config'),
                  'cancelled': experiment['cancelled'], 'trials': experiment['results']}
        if experiment.get('interruption_reason'):
            report['interruption_reason'] = experiment['interruption_reason']
        if experiment['kind'] == 'resource_calibration':
            groups = {p: [r for r in experiment['results'] if r.get('profile') == p]
                      for p in ('light', 'contention')}
            qualified = (not experiment['cancelled'] and all(len(rows) == 3 and all(r.get('valid') for r in rows)
                         for rows in groups.values()) and all(r['miss_rate'] <= .02 for r in groups['light'])
                         and all(r['miss_rate'] > .02 for r in groups['contention']))
            self.qualified_resources.pop(self.resource, None)
            if qualified and (key := resource_calibration_key(experiment['config'])):
                self.qualified_resources[self.resource] = key
                report['frozen_configuration'] = key
            report['calibration_passed'] = bool(qualified)
        elif experiment['kind'] == 'resource_comparison':
            report['comparison'] = resource_comparison(experiment['results'], self.resource,
                frozen=resource_calibration_key(experiment['config']) == self.qualified_resources.get(self.resource))
        if experiment['kind'] in ('responsiveness', 'model_comparison') and experiment['results']:
            if self.qualified_config and self.qualified_config == calibration_key(experiment['config']):
                try:
                    report['comparison'] = comparison_report(experiment['results'])
                except (KeyError, TypeError):
                    report['comparison'] = {'useful': False, 'reason': 'incomplete trial metrics'}
            else:
                report['comparison'] = {'useful': False,
                    'reason': 'configuration was not frozen from a passing desktop calibration'}
        elif experiment['kind'] in ('calibration', 'heldout_calibration'):
            self.qualified_config = None
            qualified = []
            for budget in (2, 4, 6, 8, 10, 12, 14, 16):
                groups = {profile: [row for row in experiment['results']
                                    if row.get('budget_ms') == budget and row.get('profile') == profile
                                    and row.get('valid')]
                          for profile in ('solo', 'light', 'contention')}
                if all(len(rows) == 3 for rows in groups.values()):
                    rates = {profile: sum(row['miss_rate'] for row in rows) / 3
                             for profile, rows in groups.items()}
                    contention_p95 = max(row['p95_lateness_ms'] for row in groups['contention'])
                    if qualifies_calibration(rates['solo'], rates['light'], rates['contention'], contention_p95):
                        qualified.append({'budget_ms': budget, 'miss_rates': rates,
                                          'contention_p95_lateness_ms': contention_p95})
            report['qualified_candidates'] = qualified
            if qualified and not experiment['cancelled']:
                self.qualified_config = calibration_key({**experiment['config'], 'budget_ms': qualified[0]['budget_ms']})
                report['frozen_configuration'] = self.qualified_config
        experiment['report'] = report
        experiment['phase'] = 'done'
        self.experiment_results.setPlainText(json.dumps(report, indent=2, allow_nan=False))
        self.experiment_progress.setText('Experiment cancelled.' if experiment['cancelled'] else 'Experiment complete.')
        self.experiment_cancel.setEnabled(False)
        self.experiment_export.setEnabled(True)
        if self.active:
            self.action('stop_session')

    def export_experiment(self):
        if not self.experiment or 'report' not in self.experiment:
            return
        path, _ = QFileDialog.getSaveFileName(self, 'Export experiment results', 'adaptive-os-experiment.json',
                                              'JSON (*.json)')
        if path:
            Path(path).write_text(json.dumps(self.experiment['report'], indent=2, allow_nan=False) + '\n')

    def add_workload(self, row):
        if len(self.workloads) >= MAX_WORKLOADS:
            raise ValueError('At most 8 workloads per playground; remove a stopped workload first')
        configured_memory = sum(w['config'].get('mib', 0) for w in self.workloads.values())
        if configured_memory + row['config'].get('mib', 0) > 512:
            raise ValueError('Combined configured memory buffers cannot exceed 512 MiB')
        self.workloads[row['id']] = row
        self.refresh_table()

    def add_dialog(self):
        if self.history_mode or self.busy:
            return
        dialog = WorkloadDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.add_workload(dialog.result_workload())
                self.table.setCurrentCell(len(self.workloads) - 1, 0)
            except ValueError as exc:
                self.status.setText(str(exc))
        self.update_controls()

    def selected_id(self):
        row = self.table.currentRow()
        keys = list(self.workloads)
        return keys[row] if 0 <= row < len(keys) else None

    def refresh_table(self):
        selected = self.selected_id()
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.workloads))
        for index, (key, row) in enumerate(self.workloads.items()):
            sample = row['sample'] if not row['stale'] else {}
            pending = next((p['op'] for p in self.pending.values() if p.get('workload_id') == key), None)
            state = f'{pending} pending' if pending else ('stale' if row['stale'] and row['status'] == 'running' else row['status'])
            rate = '—' if sample.get('app_rate') is None else f'{sample["app_rate"]:,.1f} {sample.get("app_unit", "")}'
            values = [row['name'], PRESETS[row['preset']], state, format_number(sample.get('cpu_pct')),
                      format_number(sample.get('rss_mib')), rate, PRIORITIES.get(row['nice'], 'Unknown')]
            for col, value in enumerate(values):
                item = self.table.item(index, col)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(index, col, item)
                item.setText(value)
                if col == 0:
                    item.setForeground(QColor(COLORS[index % len(COLORS)]))
            for chart in (self.cpu_chart, self.memory_chart, self.allocation_chart, self.network_chart, self.throughput_chart, self.latency_chart):
                chart.labels[key] = row['name']
                chart.color_indexes[key] = index
        self.table.resizeColumnsToContents()
        if selected in self.workloads and self.table.currentRow() != list(self.workloads).index(selected):
            self.table.setCurrentCell(list(self.workloads).index(selected), 0)
        self.table.blockSignals(False)

    def show_details(self, reset_fields=True):
        key = self.selected_id()
        if not key:
            return
        row = self.workloads[key]
        if reset_fields:
            self.detail_title.setText(PRESETS[row['preset']])
            self.edit_name.setText(row['name'])
            self.edit_budget.setValue(row['config'].get('budget_ms', 4))
            self.edit_memory.setRange(16 if row['preset'] == 'cache' else 1, 512 if row['preset'] == 'cache' else 128)
            self.edit_memory.setSingleStep(16 if row['preset'] == 'cache' else 1)
            self.edit_memory.setValue(row['config'].get('mib', 64))
        self.priority.blockSignals(True)
        self.priority.setCurrentIndex(max(0, self.priority.findData(row['nice'])))
        self.priority.blockSignals(False)
        self.role.blockSignals(True)
        self.role.setCurrentText(row.get('role', 'Unmanaged'))
        self.role.blockSignals(False)
        self.importance.blockSignals(True)
        self.importance.setCurrentText(row.get('importance', 'Normal'))
        self.importance.blockSignals(False)
        s = row['sample'] if not row['stale'] else {}
        self.metrics.setText(f'Status: {"stale" if row["stale"] else row["status"]}'
            f'\nCPU: {format_number(s.get("cpu_pct"))}%   RSS: {format_number(s.get("rss_mib"))} MiB'
            f'\nDeadline misses (5 s): {format_number(None if s.get("miss_rate") is None else 100*s["miss_rate"])}%'
            f'\np95 lateness: {format_number(s.get("p95_lateness_ms"), 2)} ms'
            f'\np95 wakeup: {format_number(s.get("p95_wakeup_ms"), 2)} ms'
            f'\nKernel read / write: {format_number(None if s.get("read_bytes") is None else s["read_bytes"]/1048576)} / '
            f'{format_number(None if s.get("write_bytes") is None else s["write_bytes"]/1048576)} MiB'
            + ('\nWarm-up (first 5 s)' if s.get('warmup') else ''))
        if row['preset'] in ('cache', 'network'):
            allocation = row.get('allocated')
            rate = s.get('miss_rate')
            metric = 'Cache misses' if row['preset'] == 'cache' else 'Deadline misses'
            self.metrics.setText(f'Status: {"stale" if row["stale"] else row["status"]}'
                f'\nCPU: {format_number(s.get("cpu_pct"))}% · RSS: {format_number(s.get("rss_mib"))} MiB'
                f'\n{metric} (5 s): {format_number(None if rate is None else 100 * rate)}%'
                f'\np95 request latency: {format_number(s.get("p95_latency_ms"), 2)} ms'
                f'\nCompleted work: {format_number(s.get("app_rate"))} {s.get("app_unit", "")}'
                + ('\nWarm-up (first 5 s)' if s.get('warmup') else ''))
            if row['preset'] == 'cache':
                self.metrics.setText(self.metrics.text() + f'\nConfigured / confirmed capacity: {row["config"]["mib"]} / {format_number(allocation, 0)} MiB'
                    f'\nCache hits / misses: {s.get("cache_hits", "—")} / {s.get("cache_misses", "—")}')
            else:
                self.metrics.setText(self.metrics.text() + f'\nAssigned share: {format_number(allocation / 100 if allocation else None)}%'
                    f'\nQueue delay: {format_number(s.get("queue_delay_ms"))} ms'
                    f'\nCompleted application records / failed: {s.get("completed_records", "—")} / {s.get("failed_transfers", "—")}')
            self.latency_chart.title = 'p95 request completion latency'
        else:
            self.latency_chart.title = 'p95 completion lateness'
        self.throughput_chart.unit = s.get('app_unit', 'MiB/s' if row['preset'] in ('memory', 'io') else 'hashes/s')
        for chart, metric in ((self.throughput_chart, 'app_rate'), (self.latency_chart, 'p95_lateness_ms')):
            chart.clear()
            for seconds, sample in row.get('series', []):
                chart.append(key, seconds, sample.get(metric))
        self.update_controls()

    def update_controls(self):
        self.refresh_provider_state()
        running_experiment = bool(self.experiment and self.experiment.get('phase') != 'done')
        editable = not self.history_mode and not self.busy and not self.closing and not running_experiment
        self.resource_choice.setEnabled(editable and not self.active and not self.readiness_run)
        self.resource_backgrounds.setEnabled(editable and not self.active and self.resource != 'cpu')
        self.link_mbps.setEnabled(editable and not self.active and self.resource == 'network')
        for button in (self.favor_button, self.restore_button):
            button.setEnabled(editable and self.active and self.resource != 'cpu' and self.resource_supported
                              and not self.auto_active and not self.pending and not self.resource_restoring)
        self.prepare_button.setEnabled(editable and not self.active)
        self.start_button.setEnabled(editable and not self.active)
        self.stop_button.setEnabled(self.active and not self.busy and not self.closing)
        self.add_button.setEnabled(editable and self.resource == 'cpu' and len(self.workloads) < MAX_WORKLOADS)
        self.start_all_button.setEnabled(editable and self.active and self.preflight_complete)
        self.stop_all_button.setEnabled(editable and self.active)
        for button in (self.history_refresh, self.history_open, self.json_export, self.csv_export, self.new_session):
            button.setEnabled(not self.active and not self.busy and not self.closing)
        self.workload_previous.setEnabled(self.history_mode and not self.busy and self.workload_offset > 0)
        self.workload_next.setEnabled(self.history_mode and not self.busy and self.workload_offset + MAX_WORKLOADS < self.workload_total)
        key = self.selected_id()
        row = self.workloads.get(key)
        waiting = any(p.get('workload_id') == key or any(c['workload_id'] == key for c in p.get('changes', []))
                      for p in self.pending.values())
        running = bool(row and row['status'] == 'running')
        mutable = bool(editable and row and not waiting)
        self.run_button.setEnabled(mutable and self.active and not running)
        self.halt_button.setEnabled(mutable and self.active and running)
        self.priority.setEnabled(mutable and running and self.active and self.priority_supported and not self.auto_active and self.resource == 'cpu')
        self.role.setEnabled(mutable and not self.auto_active and (self.resource == 'cpu' or not running))
        self.importance.setEnabled(mutable and not self.auto_active and bool(row and row.get('role') == 'Background'))
        managed = [item for item in self.workloads.values() if item.get('role') in ('Foreground', 'Background')]
        managed_ids = {item['id'] for item in managed}
        managed_pending = any(item.get('workload_id') in managed_ids for item in self.pending.values())
        self.auto_button.setEnabled(editable and (self.auto_active or (self.active and self.preflight_complete
                                    and (self.priority_supported if self.resource == 'cpu' else self.resource_supported)
                                    and not managed_pending and bool(managed))))
        self.auto_button.setText('Disable policy' if self.auto_active else 'Enable policy')
        self.auto_button.setAccessibleName(self.auto_button.text())
        self.policy_choice.setEnabled(editable and not self.auto_active and not self.pending)
        provider_editable = editable and not self.auto_active and not self.readiness_run and not any(
            item['status'] == 'running' for item in self.workloads.values())
        for field in (self.jev_key, self.laya_device, self.jev_test, self.jev_validate, self.laya_load, self.laya_validate):
            field.setEnabled(provider_editable)
        for provider in ('jev', 'laya'):
            item = self.policy_choice.model().item(self.policy_choice.findData(provider))
            item.setEnabled(self.live_model_allowed(provider))
        self.save_button.setEnabled(mutable and not running)
        self.remove_button.setEnabled(mutable and not running)
        self.edit_name.setEnabled(mutable and not running)
        self.edit_budget.setEnabled(mutable and not running and row['preset'] == 'periodic')
        self.edit_memory.setEnabled(mutable and not running and row['preset'] in ('memory', 'cache'))
        self.experiment_start.setEnabled(not self.active and not self.busy and not running_experiment)
        self.experiment_cancel.setEnabled(running_experiment)
        for field in (self.experiment_kind, self.experiment_backgrounds,
                      self.experiment_budget, self.experiment_importance):
            field.setEnabled(not running_experiment)

    def save_settings(self):
        key = self.selected_id()
        if not key:
            return
        row = self.workloads[key]
        name = self.edit_name.text().strip()
        if not name:
            self.status.setText('Enter a workload name.')
            return
        config = {'budget_ms': self.edit_budget.value()} if row['preset'] == 'periodic' else (
            {**row['config'], 'mib': self.edit_memory.value()} if row['preset'] in ('memory', 'cache') else row['config'])
        total = sum(w['config'].get('mib', 0) for k, w in self.workloads.items() if k != key) + config.get('mib', 0)
        if total > 512:
            self.status.setText('Combined configured memory buffers cannot exceed 512 MiB.')
            return
        try:
            config = validate_config(row['preset'], config)
        except ValueError as exc:
            self.status.setText('Invalid settings: ' + str(exc))
            return
        row.update(name=name, config=config)
        self.refresh_table()
        self.status.setText('Settings saved for the next workload start.')

    def update_policy_fields(self):
        key = self.selected_id()
        if key and not self.auto_active:
            self.workloads[key].update(role=self.role.currentText(), importance=self.importance.currentText())
            if self.workloads[key]['preset'] == 'network':
                self.workloads[key]['config']['foreground'] = self.role.currentText() == 'Foreground'
            self.update_controls()

    def audit(self, kind, **fields):
        fields.setdefault('host_monotonic_ns', time.monotonic_ns())
        fields.setdefault('event_version', 2)
        fields.setdefault('session', self.session_id)
        fields.setdefault('resource', self.resource)
        if self.clock_anchor:
            mono, host = self.clock_anchor
            fields.setdefault('sandbox_estimate_ns', mono + fields['host_monotonic_ns'] - host)
        if kind == 'decision_context':
            fields['device'] = self.laya_device.currentData() if self.policy.requested == 'laya' else 'hosted'
        event = {'kind': kind, **fields}
        self.decision_panel.ingest(event)
        if kind in ('decision_acknowledgement', 'priority_outcome') and fields.get('acknowledged_at') is not None:
            if kind == 'decision_acknowledgement' or fields.get('confirmed'):
                chart = self.cpu_chart if fields['resource'] == 'cpu' else self.allocation_chart
                chart.mark_acknowledgement(fields['request_id'],
                    fields['acknowledged_at'] - (self.origin_ns or 0) / 1e9, 'Confirmed resource change')
        if self.experiment and self.experiment.get('phase') == 'measurement':
            if kind in ('decision_context', 'model_proposal', 'policy_fallback') or (
                    kind == 'model_rejection' and fields.get('reason') == 'late_response'):
                entry = {'kind': kind, **{key: fields.get(key) for key in
                    ('request_id', 'latency_ms', 'helper_cpu_seconds', 'helper_max_rss', 'reason')}}
                entry['input_tokens'] = fields.get('proposal', {}).get('input_tokens')
                if 'context' in fields:
                    entry['input_bytes'] = len(json.dumps(fields['context'], separators=(',', ':')).encode())
                self.experiment.setdefault('inference_events', []).append(entry)
            window_id = fields.get('decision_window')
            if kind == 'rule_reference' and window_id:
                self.experiment.setdefault('decision_window_ids', set()).add(window_id)
                if fields.get('requested') != 'rule' and fields.get('effective') == 'rule':
                    self.experiment.setdefault('fallback_window_ids', set()).add(window_id)
            elif kind == 'policy_fallback' and window_id:
                self.experiment.setdefault('fallback_window_ids', set()).add(window_id)
        if self.active:
            try:
                self.worker.submit({'op': 'audit', 'message': event})
            except queue.Full:
                self.status.setText('Controller audit queue is full; Auto disabled.')
                self.set_auto(False)

    def set_auto(self, enabled):
        if self.resource != 'cpu' and not enabled:
            self.auto_active = False
            self.policy.stop()
            self.resource_restoring = True
            self.restore_resource_next()
            self.update_controls()
            return
        if enabled:
            if self.auto_active:
                return
            selected = self.policy_choice.currentData()
            if selected == 'manual':
                self.set_auto(False)
                return
            provider, shadow = selected.split(':')[-1], selected.startswith('shadow:')
            if provider != 'rule' and not shadow and not self.live_model_allowed(provider):
                self.status.setText('Live model control requires passed readiness for this exact provider and device.')
                self.auto_active = False
                return
            if provider != 'rule' and (self.loaded_provider != provider or not self.model_helper or not self.model_helper.ready or
                                       self.loaded_fingerprint != self.provider_fingerprint(provider)):
                self.status.setText('Load and test the selected provider in Providers before starting workloads.')
                self.auto_active = False
                return
            foreground = [row for row in self.workloads.values()
                          if row.get('role') == 'Foreground' and row['status'] == 'running']
            backgrounds = [row for row in self.workloads.values()
                           if row.get('role') == 'Background' and row['status'] == 'running']
            managed_ids = {row['id'] for row in foreground + backgrounds}
            if not self.preflight_complete or not (self.priority_supported if self.resource == 'cpu' else self.resource_supported) or len(foreground) != 1 \
                    or not 1 <= len(backgrounds) <= 7 or any(
                        request.get('workload_id') in managed_ids for request in self.pending.values()):
                self.status.setText('Auto requires a passed session preflight, one running foreground, and 1–7 running backgrounds.')
                self.auto_active = False
                self.update_controls()
                return
            for row in backgrounds:
                row.update(original_nice=row['nice'], intervention_order=None)
            if self.resource != 'cpu':
                for row in foreground + backgrounds:
                    row['original_allocation'] = row.get('allocated', row['config'].get('mib', row['config'].get('share')))
                self.allocation_history.clear()
            if provider == 'rule':
                self.unload_model()
            self.policy.helper = self.model_helper
            self.policy.activate(self.session_id, str(self.experiment['index']) if self.experiment else 'playground',
                (foreground[0]['id'], foreground[0].get('identity')),
                ([ManagedTask(row['id'], row['importance'], row.get('sample', {}).get('cpu_pct'),
                    row['original_nice'], row['nice'], row['identity']) for row in backgrounds]
                 if self.resource == 'cpu' else self.current_model_tasks()), provider, shadow=shadow,
                foreground_task=self.resource_task(foreground[0]) if self.resource != 'cpu' else None)
            self.intervention_order = 0
            self.last_missing_bucket = None
            self.last_observation_host_ns = time.monotonic_ns()
            self.auto_active = True
            if provider != 'rule':
                self.audit('provider_configuration', event_version=1, provider=provider,
                    fingerprint=self.provider_fingerprint(provider),
                    device=self.laya_device.currentData() if provider == 'laya' else 'hosted',
                    cold_start_ms=getattr(self.model_helper, 'metadata', {}).get('host_cold_start_ms'),
                    readiness_passed=self.model_is_ready(provider), deadline_ms=1000,
                    unqualified_demo=(not shadow and self.allow_unqualified_jev_demo
                                      and provider == 'jev' and not self.model_is_ready(provider)))
            self.controller_status.setText('Auto: Rule • observing valid windows')
            self.audit('cpu_policy', policy_version='rule-v1', foreground_id=foreground[0]['id'],
                       backgrounds=[{'workload_id': row['id'], 'identity': row['identity'],
                                     'importance': row['importance'], 'original_nice': row['original_nice']}
                                    for row in backgrounds])
        else:
            self.auto_active = False
            self.policy.stop()
            for request in self.pending.values():
                if request.get('automatic'):
                    request['restore_after_ack'] = True
            restoring = []
            for key, row in self.restoration_targets():
                if (row.get('role') == 'Background' and row['status'] == 'running'
                        and row.get('original_nice', row['nice']) != row['nice']):
                    request_id = self.request_priority(key, row['original_nice'], source='restoration')
                    if request_id:
                        self.pending[request_id]['restoration'] = True
                        restoring.append({'workload_id': key, 'identity': row['identity'],
                                          'nice': row['original_nice']})
            if restoring:
                self.audit('cpu_restoration', targets=restoring)
            self.controller_status.setText('Manual mode')
        self.update_controls()

    def restoration_targets(self):
        return sorted(self.workloads.items(), key=lambda item: (
            {'High': 0, 'Normal': 1, 'Low': 2}.get(item[1].get('importance'), 1),
            -(item[1].get('intervention_order') or 0), item[0]))

    def evaluate_auto(self, foreground):
        if not self.auto_active or foreground.get('role') != 'Foreground':
            return
        sample = foreground['sample']
        start, end = sample.get('window_start_ns'), sample.get('window_end_ns')
        if start is None or end is None:
            return
        if self.rule_state.last_window_end is not None and end / 1e9 <= self.rule_state.last_window_end:
            return
        backgrounds = [row for row in self.workloads.values()
                       if row.get('role') == 'Background' and row['status'] == 'running']
        if self.resource != 'cpu':
            self.policy.foreground_task = self.resource_task(foreground)
            valid = (not foreground['stale'] and sample.get('miss_rate') is not None and
                     all(not row['stale'] and row.get('sample', {}).get('progress_fresh') for row in backgrounds))
            decision = self.policy.observe(CpuWindow(start / 1e9, end / 1e9, sample.get('miss_rate'), None, valid),
                self.current_model_tasks(), (foreground['id'], foreground.get('identity')),
                foreground_p95_ms=sample.get('p95_latency_ms'),
                work={row['id']: row.get('sample', {}).get('app_rate') for row in backgrounds})
            self.last_observation_host_ns = time.monotonic_ns()
            if decision:
                self.apply_policy_decision(decision)
            return
        cpu = {}
        for row in backgrounds:
            values = [value.get('cpu_pct') for _, value in row.get('series', ())
                      if start < value.get('mono_ns', -1) <= end and value.get('cpu_pct') is not None]
            cpu[row['id']] = sum(values) / len(values) if len(values) >= 4 else None
        valid = (not foreground['stale'] and all(not row['stale'] for row in backgrounds)
                 and all(value is not None for value in cpu.values()))
        tasks = [ManagedTask(row['id'], row['importance'], cpu[row['id']],
                            row['original_nice'], row['nice'], row['identity'], row.get('intervention_order'))
                 for row in backgrounds]
        window = CpuWindow(start / 1e9, end / 1e9,
                           sample.get('miss_rate'), sum(item.cpu_pct for item in tasks) if valid else None, valid)
        decision = self.policy.observe(window, tasks, (foreground['id'], foreground.get('identity')),
            foreground_p95_ms=sample.get('p95_lateness_ms'),
            work={row['id']: row.get('sample', {}).get('app_rate') for row in backgrounds})
        self.last_observation_host_ns = time.monotonic_ns()
        self.last_missing_bucket = None
        reference = self.policy.reference
        self.audit('cpu_decision', policy_version='rule-v1', window_start_ns=start,
                   window_end_ns=end, valid=valid, miss_rate=sample.get('miss_rate'),
                   background_cpu=window.background_cpu, action=reference.action,
                   target=reference.target, nice=reference.nice, reason=reference.reason,
                   reference_only=self.policy.requested != 'rule')
        if decision:
            self.apply_policy_decision(decision)

    def remove_selected(self):
        key = self.selected_id()
        if key and self.workloads[key]['status'] != 'running':
            del self.workloads[key]
            for chart in (self.cpu_chart, self.memory_chart, self.allocation_chart, self.network_chart, self.throughput_chart, self.latency_chart):
                chart.series.pop(key, None)
                chart.labels.pop(key, None)
                chart.color_indexes.pop(key, None)
                chart.update()
            self.refresh_table()
            if self.workloads:
                self.table.setCurrentCell(0, 0)
            self.update_controls()

    def start_session(self):
        if self.resource != 'cpu' and (sum(row.get('role') == 'Foreground' for row in self.workloads.values()) != 1
                                      or any(row.get('role') == 'Unmanaged' for row in self.workloads.values())):
            self.status.setText('Mark exactly one foreground and one to seven backgrounds before starting.')
            return
        self.reset_controller_state()
        self.clear_charts()
        self.events.clear()
        for row in self.workloads.values():
            row.update(status='stopped', sample={}, stale=False, nice=0, receipt_ns=0)
            row.pop('series', None)
            row.pop('raw', None)
        self.origin_ns = None
        self.resource_supported = False
        self.action('start_session', workloads=self.definitions(), resource=self.resource, link_mbps=self.link_mbps.value())

    def reset_controller_state(self):
        self.auto_active = False
        self.policy.stop()
        self.rule_state = RuleState()
        self.intervention_order = 0
        self.last_missing_bucket = None
        self.deferred_stop = None
        self.resource_restoring = False
        self.allocation_history.clear()
        self.resource_effects.clear()
        self.controller_status.setText('Manual mode • priority preflight pending')

    def definitions(self):
        return [{k: row[k] for k in ('id', 'name', 'preset', 'config')} for row in self.workloads.values()]

    def control(self, op, key, **fields):
        if not self.active or self.history_mode or any(p.get('workload_id') == key for p in self.pending.values()):
            return None
        command = {'v': VERSION, 'request_id': str(uuid.uuid4()), 'op': op, 'workload_id': key, **fields}
        request = {'op': 'control', 'command': command}
        if op == 'start':
            row = self.workloads[key]
            command.update(preset=row['preset'], config=row['config'])
            request['workload'] = {k: row[k] for k in ('id', 'name', 'preset', 'config')}
        try:
            queued = op == 'priority' and any(p.get('op') == 'priority' and not p.get('queued')
                                              for p in self.pending.values())
            if not queued:
                self.worker.submit(request)
            self.pending[command['request_id']] = {**command, **({'queued': True} if queued else {})}
            self.refresh_table()
            self.update_controls()
            return command['request_id']
        except queue.Full:
            self.status.setText('Action queue is full; wait for acknowledgements.')
            return None

    def dispatch_next_priority(self):
        if not self.active or any(p.get('op') == 'priority' and not p.get('queued') for p in self.pending.values()):
            return
        request = next((p for p in self.pending.values() if p.get('queued')), None)
        if request:
            command = {key: request[key] for key in ('v', 'request_id', 'op', 'workload_id', 'nice', 'identity')}
            try:
                self.worker.submit({'op': 'control', 'command': command})
                request.pop('queued')
                self.audit('priority_dispatched', cycle_id=request.get('cycle_id'),
                           request_id=request['request_id'], source=request.get('source', 'manual'))
            except queue.Full:
                self.status.setText('Priority queue is waiting for the worker.')

    def request_priority(self, key, nice, *, source='manual', cycle_id=None):
        row = self.workloads[key]
        request_id = self.control('priority', key, identity=row.get('identity'), nice=nice)
        if request_id:
            request = self.pending[request_id]
            request.update(source=source, cycle_id=cycle_id or f'control:{request_id}', before_nice=row['nice'])
            self.audit('priority_requested', cycle_id=request['cycle_id'], source=source,
                request_id=request_id, workload_id=key, before_nice=row['nice'], nice=nice,
                queued=bool(request.get('queued')))
        return request_id

    def intervention_pending(self):
        return self.resource_restoring or any(item.get('restoration') or item.get('automatic') or
            item.get('op') in ('memory_budget', 'network_share') for item in self.pending.values())

    def stop_session_requested(self):
        if self.auto_active or self.resource != 'cpu':
            self.set_auto(False)
        if self.intervention_pending():
            self.deferred_stop = 'session'
        else:
            self.action('stop_session')

    def finish_deferred_stop(self):
        if not self.deferred_stop or self.intervention_pending():
            return
        target, self.deferred_stop = self.deferred_stop, None
        if target == 'session':
            self.action('stop_session')
        elif target == 'all':
            self.stop_all()
        elif target in self.workloads and self.workloads[target]['status'] == 'running':
            self.control('stop', target)

    def change_priority(self):
        key = self.selected_id()
        if key and self.priority.isEnabled():
            self.request_priority(key, self.priority.currentData())

    def start_selected(self):
        if key := self.selected_id():
            self.control('start', key)

    def stop_selected(self):
        if key := self.selected_id():
            if (self.auto_active or self.resource != 'cpu') and self.workloads[key].get('role') in ('Foreground', 'Background'):
                self.set_auto(False)
            if self.intervention_pending():
                self.deferred_stop = key
            else:
                self.control('stop', key)

    def start_all(self):
        for key, row in self.workloads.items():
            if row['status'] != 'running':
                self.control('start', key)

    def stop_all(self):
        if self.auto_active or (self.resource != 'cpu' and not self.resource_restoring):
            self.set_auto(False)
        if self.intervention_pending():
            self.deferred_stop = 'all'
            return
        for key, row in self.workloads.items():
            if row['status'] == 'running':
                self.control('stop', key)

    def log(self, text):
        self.events.appendPlainText(time.strftime('%H:%M:%S') + '  ' + text)

    def sample(self, message):
        from collections import deque
        key = message['workload_id']
        if key not in self.workloads:
            return
        row = self.workloads[key]
        receipt = message.get('host_receipt_ns', time.monotonic_ns())
        stale = message.get('stale', False) or (not self.history_mode and time.monotonic_ns() - receipt > 3_000_000_000)
        row.update(sample=message, receipt_ns=receipt, stale=stale, nice=message.get('nice', row['nice']))
        if message.get('allocated') is not None:
            row['allocated'] = message['allocated']
        if self.origin_ns is None:
            self.origin_ns = message['mono_ns']
        seconds = (message['mono_ns'] - self.origin_ns) / 1e9
        values = {} if stale else message
        row.setdefault('series', deque(maxlen=300)).append((seconds, values))
        self.cpu_chart.append(key, seconds, values.get('cpu_pct'))
        self.memory_chart.append(key, seconds, values.get('rss_mib'))
        allocated = values.get('allocated')
        self.allocation_chart.append(key, seconds, allocated / 100 if allocated is not None and self.resource == 'network' else allocated)
        self.network_chart.append(key, seconds, values.get('app_rate') if self.resource == 'network' else None)
        if self.resource != 'cpu' and row.get('role') == 'Foreground' and not stale and not self.history_mode:
            for effect in list(self.resource_effects):
                if (message.get('window_start_ns') or 0) >= effect['acknowledged_at_ns']:
                    self.audit('resource_effect', **effect, after=self.allocation_metrics(), observational=True)
                    self.resource_effects.remove(effect)
        if self.experiment and self.experiment.get('phase') in ('warmup', 'measurement', 'drain'):
            # Counter intervals can straddle either boundary regardless of UI delivery phase.
            # summarize_trial clips them to the exact Linux measurement interval.
            sample = {**message, 'role': row.get('role')}
            if self.experiment['phase'] == 'drain':
                sample['drain'] = True
            self.experiment['samples'].append(sample)
        if not self.experiment or self.experiment.get('phase') != 'drain':
            foreground = next((item for item in self.workloads.values()
                               if item.get('role') == 'Foreground'), None)
            if foreground:
                self.evaluate_auto(foreground)

    def handle_update(self, message):
        kind = message['kind']
        if kind == 'sample':
            self.sample(message)
        elif kind == 'raw':
            row = self.workloads.get(message.get('workload_id'))
            if row:
                row['raw'] = message['data']
                experiment = self.experiment
                if experiment and experiment.get('phase') in ('starting', 'warmup', 'measurement', 'drain'):
                    experiment.setdefault('raw_records', []).append(message)
                if experiment and row.get('role') == 'Foreground':
                    ticks = message['data'].get('ticks', ())
                    if experiment.get('phase') == 'warmup':
                        # Boundary ticks may arrive before the UI advances to measurement.
                        experiment['raw_ticks'].extend(ticks)
                    elif experiment.get('phase') in ('measurement', 'drain'):
                        start, end = experiment['measurement_start_ns'], experiment['measurement_end_ns']
                        experiment['raw_ticks'].extend(tick for tick in ticks
                                                       if start <= tick['scheduled_ns'] < end)
        elif kind == 'ready':
            self.active, self.busy = True, False
            self.history_mode = False
            self.session_id = message['session_id']
            self.origin_ns = message['mono_ns']
            self.clock_anchor = (message['mono_ns'], message.get('receipt_ns',
                                 message.get('host_receipt_ns', time.monotonic_ns())))
            self.decision_panel.clear()
            self.decision_panel.origin_ns = self.origin_ns
            self.decision_page_token = None
            if self.experiment and self.experiment.get('phase') == 'session':
                self.experiment['config']['sandbox'] = {key: message.get(key) for key in
                    ('image_id', 'workload_cpu', 'collector_cpu', 'allowed_cpus', 'shared_collector')}
                if (self.experiment['kind'] == 'model_comparison' and
                        calibration_key(self.experiment['config']) != self.qualified_config):
                    self.experiment['cancelled'] = True
                    self.experiment['interruption_reason'] = 'sandbox image or CPU placement differs from held-out calibration'
                    self.finish_experiment()
                    return
                if (self.experiment['kind'] == 'resource_comparison' and resource_calibration_key(self.experiment['config']) !=
                        self.qualified_resources.get(self.resource)):
                    self.experiment['cancelled'] = True
                    self.experiment['interruption_reason'] = 'resource configuration differs from frozen calibration'
                    self.finish_experiment()
                    return
            self.priority_supported = message['priority_supported']
            self.preflight_complete = False
            self.preflight_reason = 'Priority preflight pending'
            self.priority.setToolTip(message.get('priority_reason', ''))
            self.status.setText(f'Sandbox running • workload CPU {message["workload_cpu"]} • collector CPU {message["collector_cpu"]}. '
                                'Click Start all to run the workloads. ' + message.get('priority_reason', ''))
            self.log('Sandbox ready: ' + self.session_id)
            if message.get('shared_collector'):
                self.log('Warning: workloads and collector share the only available CPU.')
        elif kind == 'ack':
            if message.get('op') == 'configure':
                self.resource_supported = bool(message.get('ok') and message.get('resource') == self.resource)
                self.preflight_complete = self.resource_supported
                self.preflight_reason = f'{self.resource.title()} control ready; CPU priorities fixed.' if self.resource_supported else 'Resource setup failed'
                self.status.setText(self.preflight_reason)
                if self.experiment and self.experiment.get('phase') == 'session':
                    if self.resource_supported:
                        self.experiment_next_trial()
                    else:
                        self.cancel_experiment()
                self.update_controls()
                return
            if message.get('op') in ('memory_budget', 'network_share'):
                self.handle_allocation_ack(message)
                return
            if message.get('op') == 'priority_preflight':
                if message.get('session_id', self.session_id) != self.session_id:
                    self.log('Ignored stale priority preflight result from another session')
                    return
                self.preflight_complete = True
                self.priority_supported = message.get('priority_supported', False) and message['ok']
                reason = message.get('priority_reason', message.get('error', 'Priority preflight failed'))
                self.preflight_reason = reason
                self.priority.setToolTip(reason)
                self.status.setText(reason + ('. Start workloads when ready.' if self.priority_supported else '. Auto is disabled.'))
                self.controller_status.setText('Manual mode • ' +
                    (reason if self.priority_supported else 'Auto disabled: ' + reason))
                self.log(reason)
                if self.experiment and self.experiment.get('phase') == 'session':
                    if self.priority_supported:
                        self.experiment_next_trial()
                    else:
                        self.experiment['cancelled'] = True
                        self.finish_experiment()
                self.refresh_table()
                self.show_details(reset_fields=False)
                self.update_controls()
                self.refresh_decision_activity()
                return
            request = self.pending.pop(message.get('request_id'), None)
            key = message.get('workload_id') or (request or {}).get('workload_id')
            matches = (request and not request.get('queued') and message.get('session_id', self.session_id) == self.session_id
                       and message.get('op') == request['op'] and key == request['workload_id']
                       and (request['op'] != 'priority' or
                            (message.get('nice') == request['nice'] and
                             message.get('identity') == request['identity'])))
            if message['ok'] and matches and request.get('automatic'):
                at_ns = message.get('mono_ns')
                matches = (type(at_ns) is int and at_ns >= 0 and self.policy.acknowledge(
                    message['request_id'], key, message.get('identity'), message.get('nice'), at_ns / 1e9))
            if request and request.get('op') == 'priority':
                confirmed = bool(message['ok'] and matches and key in self.workloads)
                reason = '' if confirmed else ('Acknowledgement did not match request/readback' if message['ok']
                                               else message.get('error', 'Control failed'))
                at = message.get('mono_ns')
                self.audit('priority_outcome', cycle_id=request.get('cycle_id'),
                    source=request.get('source', 'manual'), request_id=message.get('request_id'),
                    workload_id=request.get('workload_id'), confirmed=confirmed, reason=reason,
                    acknowledged_at=at / 1e9 if confirmed and type(at) is int else None)
            if message['ok'] and matches and key in self.workloads:
                row = self.workloads[key]
                if 'status' in message:
                    row['status'] = message['status']
                    row['stale'] = False
                    if message['status'] == 'stopped':
                        row['sample'] = {}
                        row.pop('raw', None)
                if message.get('op') == 'start':
                    row.update(pid=message.get('pid'), identity=message.get('identity'),
                               original_nice=message.get('nice', row['nice']),
                               receipt_ns=message.get('host_receipt_ns', time.monotonic_ns()))
                    if 'allocated' in message:
                        row.update(allocated=message['allocated'], original_allocation=message['allocated'])
                if 'nice' in message:
                    row['nice'] = message['nice']
                if request.get('automatic'):
                    if request['automatic'] == 'lower':
                        self.intervention_order += 1
                        row['intervention_order'] = self.intervention_order
                    else:
                        row['intervention_order'] = None
                    acknowledged_at = message.get('mono_ns')
                    if self.experiment:
                        self.experiment['interventions'] += 1
                        self.experiment.setdefault('intervention_events', []).append({
                            'action': request['automatic'], 'provider': request.get('decision_provider', 'rule'),
                            'acknowledged_at_ns': acknowledged_at, 'request_id': message['request_id'],
                            'workload_id': key, 'identity': row['identity'], 'nice': row['nice']})
                        if request['automatic'] == 'lower':
                            self.experiment['intervention_targets'].append(key)
                            self.experiment['intervention_importances'].append(row['importance'])
                if request.get('restore_after_ack') and row['nice'] != row.get('original_nice', row['nice']):
                    restore_id = self.request_priority(key, row['original_nice'], source='restoration')
                    if restore_id:
                        self.pending[restore_id]['restoration'] = True
                self.log(f'{row["name"]}: {message.get("op", "control")} acknowledged'
                         + (f' (nice {message["nice"]})' if 'nice' in message else ''))
                if request.get('restoration'):
                    self.finish_deferred_stop()
                self.dispatch_next_priority()
                if self.resource != 'cpu':
                    self.restore_resource_next()
                self.advance_experiment()
            elif request and message['ok']:
                error = 'Control acknowledgement did not match the requested operation or readback'
                requested_key = request.get('workload_id')
                if request.get('automatic') and requested_key in self.workloads:
                    self.set_auto(False)
                    restore_id = self.request_priority(
                        requested_key, self.workloads[requested_key].get('original_nice', 0), source='restoration')
                    if restore_id:
                        self.pending[restore_id]['restoration'] = True
                        self.deferred_stop = 'session'
                    else:
                        self.action('stop_session')
                elif request.get('restoration'):
                    self.action('stop_session')
                elif request.get('op') == 'priority':
                    self.priority_supported = self.preflight_complete = False
                    self.action('stop_session')
                self.status.setText(error)
                self.log('Control failed: ' + error)
            elif not message['ok']:
                if request and request.get('op') == 'priority':
                    self.priority_supported = self.preflight_complete = False
                    if self.auto_active:
                        self.set_auto(False)
                    requested_key = request.get('workload_id')
                    if request.get('restoration') or requested_key not in self.workloads:
                        self.action('stop_session')
                    else:
                        restore_id = self.request_priority(
                            requested_key, self.workloads[requested_key].get('original_nice', 0), source='restoration')
                        if restore_id:
                            self.pending[restore_id]['restoration'] = True
                            self.deferred_stop = 'session'
                        else:
                            self.action('stop_session')
                self.status.setText(message.get('error', 'Control failed'))
                self.log('Control failed: ' + message.get('error', 'Unknown error'))
        elif kind == 'exit':
            key = message['workload_id']
            if key in self.workloads:
                if self.auto_active and self.workloads[key].get('role') in ('Foreground', 'Background'):
                    self.set_auto(False)
                self.workloads[key].update(status='failed', sample={}, stale=False)
                self.log(f'{self.workloads[key]["name"]} exited: {message.get("error", message.get("exit_code"))}')
                if self.experiment and self.experiment.get('phase') != 'done':
                    self.cancel_experiment()
        elif kind == 'stopped':
            self.active, self.busy = False, False
            self.reset_controller_state()
            self.preflight_complete = False
            self.pending.clear()
            for row in self.workloads.values():
                row.update(status='stopped', sample={}, stale=False)
            self.status.setText('Sandbox stopped. Recording saved.' if message.get('clean') else 'Sandbox stopped with errors; see the timeline.')
            self.log(self.status.text())
            if self.experiment and self.experiment.get('phase') != 'done':
                self.experiment['cancelled'] = True
                self.experiment.setdefault('interruption_reason', self.status.text())
                self.finish_experiment()
        elif kind == 'error':
            self.busy = False
            self.reset_controller_state()
            self.pending.clear()
            self.status.setText(message['message'])
            self.log('Error: ' + message['message'])
            if self.experiment and self.experiment.get('phase') != 'done':
                self.experiment['cancelled'] = True
                self.experiment['interruption_reason'] = message['message']
                self.finish_experiment()
        elif kind == 'docker':
            self.busy = False
            self.status.setText('Docker Desktop is ready. Start sandbox to prepare the image and begin.')
        elif kind == 'prepared':
            self.busy = False
            self.status.setText('Sandbox image is ready. Click Start sandbox.')
        elif kind in ('build', 'event'):
            if kind == 'build':
                self.log_toggle.setChecked(True)
            self.log(message.get('message', json.dumps(message)))
        elif kind == 'history':
            self.history.clear()
            from PySide6.QtWidgets import QListWidgetItem
            for session in message['sessions']:
                item = QListWidgetItem(f'{session["started_at"][:19].replace("T", " ")}  •  {session["status"]}  •  {session["id"][:8]}')
                item.setData(Qt.ItemDataRole.UserRole, session['id'])
                self.history.addItem(item)
        elif kind == 'loaded':
            self.load_recording(message['recording'])
        elif kind == 'decision_history':
            if (self.decision_page_token and message.get('session_id') == self.session_id
                    and message.get('token') == self.decision_page_token[0]):
                if message.get('error'):
                    self.decision_panel.note.setText(message['error'])
                elif not self.decision_page_token[1] or self.decision_panel.following:
                    self.decision_panel.set_page(message['page'], live=self.decision_page_token[1] and not self.history_mode)
                self.decision_page_token = None
        elif kind == 'exported':
            self.busy = False
            self.status.setText('Exported: ' + message['path'])
        elif kind == 'leftovers':
            self.busy = False
            self.leftovers = message['sessions']
            self.recover_button.setVisible(bool(self.leftovers))
            if self.leftovers:
                self.log('Previous sandbox resources found. Use Recover to inspect and remove an exact session.')
        self.refresh_table()
        self.show_details(reset_fields=False)
        self.update_controls()
        self.refresh_decision_activity()

    def refresh_staleness(self):
        if self.history_mode:
            return
        now = time.monotonic_ns()
        for key, row in self.workloads.items():
            if row['status'] == 'running' and row['receipt_ns'] and now - row['receipt_ns'] > 3_000_000_000 and not row['stale']:
                row['stale'] = True
                points = self.cpu_chart.series.get(key)
                seconds = points[-1][0] + 1 if points else 0
                for chart in (self.cpu_chart, self.memory_chart, self.allocation_chart, self.network_chart, self.throughput_chart, self.latency_chart):
                    if key in chart.series:
                        chart.append(key, seconds, None)
                if 'series' in row:
                    row['series'].append((seconds, {}))
                self.log(f'{row["name"]}: telemetry stale (>3 seconds)')
        if self.auto_active:
            foreground = next((row for row in self.workloads.values()
                               if row.get('role') == 'Foreground' and row['status'] == 'running'), None)
            if (foreground and foreground['stale'] and
                    now - self.last_observation_host_ns >= 5_000_000_000):
                self.last_observation_host_ns = now
                end = (self.rule_state.last_window_end + 5 if self.rule_state.last_window_end is not None
                       else (self.origin_ns or 0) / 1e9 + 5)
                backgrounds = [row for row in self.workloads.values()
                               if row.get('role') == 'Background' and row['status'] == 'running']
                tasks = ([ManagedTask(row['id'], row['importance'], None, row['original_nice'], row['nice'],
                                     row['identity'], row.get('intervention_order')) for row in backgrounds]
                         if self.resource == 'cpu' else self.current_model_tasks())
                decision = self.policy.observe(CpuWindow(end - 5, end, None, None, False), tasks,
                    (foreground['id'], foreground.get('identity')))
                self.audit('cpu_decision', policy_version='rule-v1', valid=False,
                           action=self.policy.reference.action, reason=self.policy.reference.reason,
                           synthetic_missing=True, reference_only=self.policy.requested != 'rule')
                if decision:
                    self.apply_policy_decision(decision)
        self.refresh_table()
        self.show_details(reset_fields=False)

    def pump(self):
        if self.shutdown_requested:
            self.begin_shutdown()
        for _ in range(100):
            try:
                self.handle_update(self.worker.updates.get_nowait())
            except queue.Empty:
                break
        if self.worker.error and self.worker.error != self.last_worker_error:
            self.last_worker_error = self.worker.error
            self.status.setText(self.worker.error)
        if self.closing:
            if not self.worker.is_alive():
                self.active, self.busy = False, False
                self.close()
            elif not self.quit_submitted:
                try:
                    self.worker.submit({'op': 'quit'})
                    self.quit_submitted = True
                except queue.Full:
                    pass  # Retry next tick after the worker drains pending requests.
            return
        self.refresh_staleness()
        self.poll_model()
        self.dispatch_next_priority()
        self.tick_experiment()
        self.refresh_decision_activity()

    def history_id(self):
        item = self.history.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def open_history(self, *_):
        if not self.active and not self.busy and (session_id := self.history_id()):
            self.action('load', session_id=session_id)

    def history_page(self, delta):
        if self.history_mode and not self.active and not self.busy:
            self.action('load', session_id=self.session_id,
                        workload_offset=max(0, self.workload_offset + delta * MAX_WORKLOADS))

    def export(self, format):
        session_id = self.history_id()
        if not session_id or self.active or self.busy:
            self.status.setText('Select a saved recording first.')
            return
        path, _ = QFileDialog.getSaveFileName(self, 'Export recording', f'adaptive-os-{session_id[:8]}.{format}',
                                            f'{format.upper()} (*.{format})')
        if path:
            self.action('export', session_id=session_id, path=path, format=format)

    def clear_charts(self):
        for chart in (self.cpu_chart, self.memory_chart, self.allocation_chart, self.network_chart, self.throughput_chart, self.latency_chart):
            chart.clear()
            chart.labels.clear()
            chart.color_indexes.clear()
            chart.window_seconds, chart.gap_seconds = 300, 2.5

    def load_recording(self, recording):
        self.busy = False
        self.history_mode = True
        self.resource = recording['session'].get('metadata', {}).get('resource', 'cpu')
        self.resource_choice.blockSignals(True)
        self.resource_choice.setCurrentIndex(self.resource_choice.findData(self.resource))
        self.resource_choice.blockSignals(False)
        self.allocation_chart.unit = '%' if self.resource == 'network' else 'MiB'
        self.session_id = recording['session']['id']
        self.workload_offset = recording.get('workload_offset', 0)
        self.workload_total = recording.get('workload_total', len(recording['workloads']))
        self.workload_pager.setVisible(self.workload_total > MAX_WORKLOADS)
        self.workload_page_label.setText(f'Workloads {self.workload_offset + 1}–'
            f'{min(self.workload_offset + MAX_WORKLOADS, self.workload_total)} of {self.workload_total}')
        self.workloads.clear()
        self.clear_charts()
        self.events.clear()
        self.decision_panel.clear()
        self.decision_page_token = None
        self.clock_anchor = None
        self.origin_ns = min((m['mono_ns'] for m in recording['samples']), default=0)
        self.decision_panel.origin_ns = self.origin_ns
        duration = max((m['mono_ns'] for m in recording['samples']), default=0) - self.origin_ns
        for chart in (self.cpu_chart, self.memory_chart, self.allocation_chart, self.network_chart, self.throughput_chart, self.latency_chart):
            chart.window_seconds = max(300, duration / 1e9)
            # Downsampled history points are observations, not interpolated gaps.
            chart.gap_seconds = 2.5
        for item in recording['workloads']:
            row = workload(item['preset'], item['name'], item['config'])
            row.update(id=item['id'], status='recorded')
            self.workloads[row['id']] = row
        for message in recording['samples']:
            self.sample(message)
        for event in recording['events']:
            self.events.appendPlainText(json.dumps(event, ensure_ascii=False))
        self.decision_panel.set_page(recording.get('decisions', {}))
        for entry in self.decision_panel.displayed.values():
            if entry.acknowledged_at is not None:
                chart = self.cpu_chart if self.resource == 'cpu' else self.allocation_chart
                chart.mark_acknowledgement(entry.cycle_id,
                    entry.acknowledged_at - self.origin_ns / 1e9, 'Recorded confirmed change')
        self.status.setText(f'Recording {self.session_id[:8]} • {recording["session"]["status"]} • read-only, up to 300 observed points per workload. Export for full resolution.')
        self.refresh_table()
        if self.workloads:
            self.table.setCurrentCell(0, 0)
            self.show_details()
        self.tabs.setCurrentIndex(0)

    def new_playground(self):
        if self.active:
            return
        self.history_mode = False
        self.session_id = None
        self.clock_anchor = None
        self.decision_page_token = None
        self.decision_panel.clear()
        self.workload_pager.hide()
        self.workloads.clear()
        self.pending.clear()
        self.clear_charts()
        self.select_resource()
        self.table.setCurrentCell(0, 0)
        self.show_details()
        self.status.setText('New playground ready. Start sandbox when Docker Desktop is running.')
        self.tabs.setCurrentIndex(0)
        self.update_controls()

    def recover(self):
        if self.active or self.busy or not self.leftovers:
            return
        session = self.leftovers[0]
        result = QMessageBox.question(self, 'Remove previous sandbox resources?',
            f'Session {session["session_id"]}\nContainers: {len(session["containers"])}\nVolumes: {len(session["volumes"])}\n\n'
            'This stops any remaining workloads and deletes scratch files for this exact session. Recordings are kept.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        if result == QMessageBox.StandardButton.Yes:
            self.action('recover', session_id=session['session_id'])

    def request_shutdown(self, *_):
        # Signal handlers only set a flag; Qt performs cleanup on its next timer tick.
        self.shutdown_requested = True

    def begin_shutdown(self):
        if self.closing:
            return
        self.closing = True
        self.auto_active = False
        self.experiment = None
        self.policy.stop()
        self.unload_model()
        self.jev_key.clear()
        if self.worker.runtime:
            self.worker.runtime.cancel.set()
        self.busy = True
        self.update_controls()
        self.status.setText('Saving and cleaning up before closing…')

    def closeEvent(self, event):
        if not self.worker.is_alive():
            self.policy.stop()
            self.unload_model()
            self.jev_key.clear()
            event.accept()
            return
        if self.closing:
            event.ignore()
            return
        if self.active or self.busy:
            result = QMessageBox.question(self, 'Stop sandbox and quit?',
                'Stop all workloads, save the recording, and remove this session’s sandbox and scratch volume?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
            if result != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.begin_shutdown()
        event.ignore()


def read_jev_env_file(path):
    try:
        lines = path.read_text(encoding='utf-8-sig').splitlines()
    except (OSError, UnicodeError):
        raise ValueError('Cannot read Jev env file; check the path and UTF-8 encoding.') from None
    key = None
    for line in lines:
        name, separator, value = line.strip().removeprefix('export ').partition('=')
        if not separator or name.strip() != 'JEV_API_KEY':
            continue
        if key is not None:
            raise ValueError('Jev env file must contain only one JEV_API_KEY assignment.')
        try:
            # Parse quotes and comments as data, without shell execution or variable expansion.
            values = shlex.split(value, comments=True)
        except ValueError:
            raise ValueError('Invalid JEV_API_KEY quoting in Jev env file.') from None
        if (len(values) != 1 or not 1 <= len(values[0]) <= 4096
                or any(not '!' <= char <= '~' for char in values[0])):
            raise ValueError('JEV_API_KEY must be 1–4096 printable ASCII characters without whitespace.')
        key = values[0]
    if key is None:
        raise ValueError('Jev env file must contain JEV_API_KEY=your-key.')
    return key


def main(argv=None):
    parser = argparse.ArgumentParser(description='Native single-sandbox workload playground')
    parser.add_argument('--backend', choices=('docker', 'guest-linux'), default='docker')
    parser.add_argument('--data-dir', type=Path, help='override the desktop recording folder')
    parser.add_argument('--jev-env-file', type=Path, metavar='PATH',
                        help='load JEV_API_KEY from a .env file into the masked Providers field')
    parser.add_argument('--allow-unqualified-jev-demo', action='store_true',
                        help='allow supervised Jev Memory/Network playground demos without passed readiness')
    args = parser.parse_args(argv)
    if args.backend == 'guest-linux' and args.allow_unqualified_jev_demo:
        parser.error('Guest Linux cannot use Docker experimental readiness overrides')
    try:
        jev_key = read_jev_env_file(args.jev_env_file) if args.jev_env_file else ''
    except ValueError as exc:
        parser.error(str(exc))
    if args.backend == 'guest-linux':
        from .guest_linux.desktop import main as guest_main
        return guest_main(args.data_dir, jev_key)
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName('Adaptive OS Controller')
    app.setOrganizationName('Adaptive OS')
    app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    directory = args.data_dir or Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
    try:
        directory.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(directory / 'desktop.lock'))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            QMessageBox.warning(None, 'Already running', 'Another app instance is using this recording folder. Close it first.')
            return 1
    except OSError as exc:
        QMessageBox.critical(None, 'Recording folder unavailable', str(exc))
        return 1
    worker = SessionWorker(directory / 'sessions-v2.sqlite3')
    window = MainWindow(worker, allow_unqualified_jev_demo=args.allow_unqualified_jev_demo)
    if jev_key:
        window.jev_key.setText(jev_key)
        window.jev_key.setToolTip('Loaded from the selected env file; changes here apply to this session only.')
        window.provider_status.setText('Jev: key loaded from env file. Test connection or validate readiness in Providers.')
    del jev_key
    previous_handlers = {sig: signal.signal(sig, window.request_shutdown) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        window.show()
        worker.start()
        worker.submit({'op': 'check'})
        result = app.exec()
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        if worker.is_alive():
            worker.submit({'op': 'quit'})
            worker.join(timeout=20)
        lock.unlock()
    if worker.error:
        print(worker.error, file=sys.stderr)
    return result or int(bool(worker.error))


if __name__ == '__main__':
    raise SystemExit(main())
