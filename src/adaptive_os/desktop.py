"""Native Qt Widgets playground; no web server, browser, or webview."""

import argparse
import json
from pathlib import Path
import queue
import sys
import time
import uuid

from PySide6.QtCore import QLockFile, QStandardPaths, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog,
    QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QSpinBox, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .charts import COLORS, TimeSeriesChart
from .desktop_worker import SessionWorker
from .sandbox.protocol import MAX_WORKLOADS, PRESETS, PRIORITIES, VERSION, validate_config


def workload(preset, name=None, config=None):
    return {'id': str(uuid.uuid4()), 'name': name or PRESETS[preset], 'preset': preset,
            'config': validate_config(preset, config or {}), 'status': 'stopped',
            'nice': 0, 'sample': {}, 'stale': False, 'receipt_ns': 0}


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
        return workload(preset, self.name.text().strip() or PRESETS[preset], config)


class MainWindow(QMainWindow):
    def __init__(self, worker):
        super().__init__()
        self.worker = worker
        self.workloads = {}
        self.pending = {}
        self.active = False
        self.busy = False
        self.priority_supported = False
        self.session_id = None
        self.history_mode = False
        self.origin_ns = None
        self.closing = False
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
        font = heading.font()
        font.setPointSize(23)
        font.setWeight(QFont.Weight.DemiBold)
        heading.setFont(font)
        outer.addWidget(heading)
        intro = QLabel('One Linux sandbox. Multiple competing workloads. Observe the scheduler and change CPU priorities live.')
        intro.setWordWrap(True)
        outer.addWidget(intro)
        toolbar = QHBoxLayout()
        outer.addLayout(toolbar)
        self.prepare_button = self.button(toolbar, 'Prepare image', lambda: self.action('prepare'))
        self.start_button = self.button(toolbar, 'Start sandbox', self.start_session)
        self.stop_button = self.button(toolbar, 'Stop sandbox', lambda: self.action('stop_session'))
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
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        playground = QWidget()
        play_layout = QVBoxLayout(playground)
        play_layout.setContentsMargins(0, 10, 0, 0)
        split = QSplitter(Qt.Orientation.Horizontal)
        play_layout.addWidget(split, 1)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(['Workload', 'Preset', 'State', 'CPU %', 'RSS MiB', 'Throughput', 'Priority'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(160)
        self.table.setMaximumHeight(270)
        self.table.setAccessibleName('Competing workloads')
        self.table.itemSelectionChanged.connect(self.show_details)
        left_layout.addWidget(self.table)
        self.cpu_chart = TimeSeriesChart('Shared CPU', '%')
        self.memory_chart = TimeSeriesChart('Resident memory', 'MiB')
        left_layout.addWidget(self.cpu_chart, 1)
        self.metric_tabs = QTabWidget()
        self.metric_tabs.addTab(self.memory_chart, 'Memory')
        left_layout.addWidget(self.metric_tabs, 1)
        left_layout.addWidget(QLabel('CPU 100% = one logical CPU. Gaps mean missing or stale data. First 5 seconds are warm-up.'))
        split.addWidget(left)
        details = QWidget()
        details.setMinimumWidth(270)
        details.setMaximumWidth(365)
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
        right.addLayout(form)
        config_buttons = QHBoxLayout()
        self.save_button = self.button(config_buttons, 'Save settings', self.save_settings)
        self.remove_button = self.button(config_buttons, 'Remove', self.remove_selected)
        right.addLayout(config_buttons)
        detail_buttons = QHBoxLayout()
        self.run_button = self.button(detail_buttons, 'Start workload', self.start_selected)
        self.halt_button = self.button(detail_buttons, 'Stop workload', self.stop_selected)
        right.addLayout(detail_buttons)
        self.metrics = QLabel('Select a workload to see its latest measurements.')
        self.metrics.setWordWrap(True)
        self.metrics.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        right.addWidget(self.metrics)
        self.throughput_chart = TimeSeriesChart('Selected throughput', 'hashes/s')
        self.latency_chart = TimeSeriesChart('p95 completion lateness', 'ms')
        self.metric_tabs.addTab(self.throughput_chart, 'Selected throughput')
        self.metric_tabs.addTab(self.latency_chart, 'Selected latency')
        note = QLabel('Priority only changes CPU scheduling, not memory limits or I/O priority. File throughput includes cached I/O; kernel counters are separate.')
        note.setWordWrap(True)
        right.addWidget(note)
        right.addStretch()
        split.addWidget(details)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        self.tabs.addTab(playground, 'Live playground')
        self.make_history_tab()
        outer.addWidget(QLabel('Event timeline / image build log'))
        self.events = QPlainTextEdit()
        self.events.setReadOnly(True)
        self.events.setMaximumBlockCount(500)
        self.events.setMaximumHeight(105)
        self.events.setAccessibleName('Event timeline and build log')
        outer.addWidget(self.events)
        self.statusBar().showMessage(f'Recordings: {worker.database}  |  Observational playground, not a causal benchmark')
        self.add_workload(workload('periodic'))
        self.add_workload(workload('cpu'))
        self.table.selectRow(0)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.pump)
        self.timer.start(100)
        QShortcut(QKeySequence.StandardKey.New, self, activated=self.add_dialog)
        QShortcut(QKeySequence.StandardKey.Quit, self, activated=self.close)
        self.update_controls()

    def button(self, layout, text, callback):
        button = QPushButton(text)
        button.setAccessibleName(text)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

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
                self.table.selectRow(len(self.workloads) - 1)
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
            for chart in (self.cpu_chart, self.memory_chart, self.throughput_chart, self.latency_chart):
                chart.labels[key] = row['name']
                chart.color_indexes[key] = index
        self.table.resizeColumnsToContents()
        if selected in self.workloads and self.table.currentRow() != list(self.workloads).index(selected):
            self.table.selectRow(list(self.workloads).index(selected))
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
            self.edit_memory.setValue(row['config'].get('mib', 64))
        self.priority.blockSignals(True)
        self.priority.setCurrentIndex(max(0, self.priority.findData(row['nice'])))
        self.priority.blockSignals(False)
        s = row['sample'] if not row['stale'] else {}
        self.metrics.setText(f'Status: {"stale" if row["stale"] else row["status"]}'
            f'\nCPU: {format_number(s.get("cpu_pct"))}%   RSS: {format_number(s.get("rss_mib"))} MiB'
            f'\nDeadline misses (5 s): {format_number(None if s.get("miss_rate") is None else 100*s["miss_rate"])}%'
            f'\np95 lateness: {format_number(s.get("p95_lateness_ms"), 2)} ms'
            f'\np95 wakeup: {format_number(s.get("p95_wakeup_ms"), 2)} ms'
            f'\nKernel read / write: {format_number(None if s.get("read_bytes") is None else s["read_bytes"]/1048576)} / '
            f'{format_number(None if s.get("write_bytes") is None else s["write_bytes"]/1048576)} MiB'
            + ('\nWarm-up (first 5 s)' if s.get('warmup') else ''))
        self.throughput_chart.unit = 'MiB/s' if row['preset'] in ('memory', 'io') else 'hashes/s'
        for chart, metric in ((self.throughput_chart, 'app_rate'), (self.latency_chart, 'p95_lateness_ms')):
            chart.clear()
            for seconds, sample in row.get('series', []):
                chart.append(key, seconds, sample.get(metric))
        self.update_controls()

    def update_controls(self):
        editable = not self.history_mode and not self.busy and not self.closing
        self.prepare_button.setEnabled(editable and not self.active)
        self.start_button.setEnabled(editable and not self.active)
        self.stop_button.setEnabled(self.active and not self.busy and not self.closing)
        self.add_button.setEnabled(editable and len(self.workloads) < MAX_WORKLOADS)
        self.start_all_button.setEnabled(editable and self.active)
        self.stop_all_button.setEnabled(editable and self.active)
        for button in (self.history_refresh, self.history_open, self.json_export, self.csv_export, self.new_session):
            button.setEnabled(not self.active and not self.busy and not self.closing)
        key = self.selected_id()
        row = self.workloads.get(key)
        waiting = any(p.get('workload_id') == key for p in self.pending.values())
        running = bool(row and row['status'] == 'running')
        mutable = bool(editable and row and not waiting)
        self.run_button.setEnabled(mutable and self.active and not running)
        self.halt_button.setEnabled(mutable and self.active and running)
        self.priority.setEnabled(mutable and running and self.active and self.priority_supported)
        self.save_button.setEnabled(mutable and not running)
        self.remove_button.setEnabled(mutable and not running)
        self.edit_name.setEnabled(mutable and not running)
        self.edit_budget.setEnabled(mutable and not running and row['preset'] == 'periodic')
        self.edit_memory.setEnabled(mutable and not running and row['preset'] == 'memory')

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
            {'mib': self.edit_memory.value()} if row['preset'] == 'memory' else {})
        total = sum(w['config'].get('mib', 0) for k, w in self.workloads.items() if k != key) + config.get('mib', 0)
        if total > 512:
            self.status.setText('Combined configured memory buffers cannot exceed 512 MiB.')
            return
        row.update(name=name, config=validate_config(row['preset'], config))
        self.refresh_table()
        self.status.setText('Settings saved for the next workload start.')

    def remove_selected(self):
        key = self.selected_id()
        if key and self.workloads[key]['status'] != 'running':
            del self.workloads[key]
            for chart in (self.cpu_chart, self.memory_chart, self.throughput_chart, self.latency_chart):
                chart.series.pop(key, None)
                chart.labels.pop(key, None)
                chart.color_indexes.pop(key, None)
                chart.update()
            self.refresh_table()
            if self.workloads:
                self.table.selectRow(0)
            self.update_controls()

    def start_session(self):
        self.clear_charts()
        self.events.clear()
        for row in self.workloads.values():
            row.update(status='stopped', sample={}, stale=False, nice=0, receipt_ns=0)
            row.pop('series', None)
        self.origin_ns = None
        self.action('start_session', workloads=self.definitions())

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
            self.worker.submit(request)
            self.pending[command['request_id']] = command
            self.refresh_table()
            self.update_controls()
            return command['request_id']
        except queue.Full:
            self.status.setText('Action queue is full; wait for acknowledgements.')
            return None

    def request_priority(self, key, nice):
        return self.control('priority', key, nice=nice)

    def change_priority(self):
        key = self.selected_id()
        if key and self.priority.isEnabled():
            self.request_priority(key, self.priority.currentData())

    def start_selected(self):
        if key := self.selected_id():
            self.control('start', key)

    def stop_selected(self):
        if key := self.selected_id():
            self.control('stop', key)

    def start_all(self):
        for key, row in self.workloads.items():
            if row['status'] != 'running':
                self.control('start', key)

    def stop_all(self):
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
        if self.origin_ns is None:
            self.origin_ns = message['mono_ns']
        seconds = (message['mono_ns'] - self.origin_ns) / 1e9
        values = {} if stale else message
        row.setdefault('series', deque(maxlen=300)).append((seconds, values))
        self.cpu_chart.append(key, seconds, values.get('cpu_pct'))
        self.memory_chart.append(key, seconds, values.get('rss_mib'))

    def handle_update(self, message):
        kind = message['kind']
        if kind == 'sample':
            self.sample(message)
        elif kind == 'ready':
            self.active, self.busy = True, False
            self.history_mode = False
            self.session_id = message['session_id']
            self.origin_ns = message['mono_ns']
            self.priority_supported = message['priority_supported']
            self.priority.setToolTip(message.get('priority_reason', ''))
            self.status.setText(f'Sandbox running • workload CPU {message["workload_cpu"]} • collector CPU {message["collector_cpu"]}. '
                                'Click Start all to run the workloads. ' + message.get('priority_reason', ''))
            self.log('Sandbox ready: ' + self.session_id)
            if message.get('shared_collector'):
                self.log('Warning: workloads and collector share the only available CPU.')
        elif kind == 'ack':
            request = self.pending.pop(message.get('request_id'), None)
            key = message.get('workload_id') or (request or {}).get('workload_id')
            matches = (request and message.get('op') == request['op'] and key == request['workload_id']
                       and (request['op'] != 'priority' or message.get('nice') == request['nice']))
            if message['ok'] and matches and key in self.workloads:
                row = self.workloads[key]
                if 'status' in message:
                    row['status'] = message['status']
                    row['stale'] = False
                    if message['status'] == 'stopped':
                        row['sample'] = {}
                if 'nice' in message:
                    row['nice'] = message['nice']
                self.log(f'{row["name"]}: {message.get("op", "control")} acknowledged'
                         + (f' (nice {message["nice"]})' if 'nice' in message else ''))
            elif not message['ok']:
                self.status.setText(message.get('error', 'Control failed'))
                self.log('Control failed: ' + message.get('error', 'Unknown error'))
        elif kind == 'exit':
            key = message['workload_id']
            if key in self.workloads:
                self.workloads[key].update(status='failed', sample={}, stale=False)
                self.log(f'{self.workloads[key]["name"]} exited: {message.get("error", message.get("exit_code"))}')
        elif kind == 'stopped':
            self.active, self.busy = False, False
            self.pending.clear()
            for row in self.workloads.values():
                row.update(status='stopped', sample={}, stale=False)
            self.status.setText('Sandbox stopped. Recording saved.' if message.get('clean') else 'Sandbox stopped with errors; see the timeline.')
            self.log(self.status.text())
        elif kind == 'error':
            self.busy = False
            self.pending.clear()
            self.status.setText(message['message'])
            self.log('Error: ' + message['message'])
        elif kind == 'docker':
            self.busy = False
            self.status.setText('Docker Desktop is ready. Start sandbox to prepare the image and begin.')
        elif kind == 'prepared':
            self.busy = False
            self.status.setText('Sandbox image is ready. Click Start sandbox.')
        elif kind in ('build', 'event'):
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

    def refresh_staleness(self):
        if self.history_mode:
            return
        now = time.monotonic_ns()
        for key, row in self.workloads.items():
            if row['status'] == 'running' and row['receipt_ns'] and now - row['receipt_ns'] > 3_000_000_000 and not row['stale']:
                row['stale'] = True
                points = self.cpu_chart.series.get(key)
                seconds = points[-1][0] + 1 if points else 0
                for chart in (self.cpu_chart, self.memory_chart, self.throughput_chart, self.latency_chart):
                    if key in chart.series:
                        chart.append(key, seconds, None)
                if 'series' in row:
                    row['series'].append((seconds, {}))
                self.log(f'{row["name"]}: telemetry stale (>3 seconds)')
        self.refresh_table()
        self.show_details(reset_fields=False)

    def pump(self):
        for _ in range(100):
            try:
                self.handle_update(self.worker.updates.get_nowait())
            except queue.Empty:
                break
        if self.worker.error and self.worker.error != self.last_worker_error:
            self.last_worker_error = self.worker.error
            self.status.setText(self.worker.error)
        self.refresh_staleness()
        if self.closing and not self.worker.is_alive():
            self.active, self.busy = False, False
            self.close()

    def history_id(self):
        item = self.history.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def open_history(self, *_):
        if not self.active and not self.busy and (session_id := self.history_id()):
            self.action('load', session_id=session_id)

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
        for chart in (self.cpu_chart, self.memory_chart, self.throughput_chart, self.latency_chart):
            chart.clear()
            chart.labels.clear()
            chart.color_indexes.clear()
            chart.window_seconds, chart.gap_seconds = 300, 2.5

    def load_recording(self, recording):
        self.busy = False
        self.history_mode = True
        self.session_id = recording['session']['id']
        self.workloads.clear()
        self.clear_charts()
        self.events.clear()
        self.origin_ns = min((m['mono_ns'] for m in recording['samples']), default=0)
        duration = max((m['mono_ns'] for m in recording['samples']), default=0) - self.origin_ns
        for chart in (self.cpu_chart, self.memory_chart, self.throughput_chart, self.latency_chart):
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
        self.status.setText(f'Recording {self.session_id[:8]} • {recording["session"]["status"]} • read-only, up to 300 observed points per workload. Export for full resolution.')
        self.refresh_table()
        if self.workloads:
            self.table.selectRow(0)
            self.show_details()
        self.tabs.setCurrentIndex(0)

    def new_playground(self):
        if self.active:
            return
        self.history_mode = False
        self.workloads.clear()
        self.pending.clear()
        self.clear_charts()
        self.add_workload(workload('periodic'))
        self.add_workload(workload('cpu'))
        self.table.selectRow(0)
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

    def closeEvent(self, event):
        if not self.worker.is_alive():
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
        self.closing = True
        if self.worker.runtime:
            self.worker.runtime.cancel.set()
        self.action('quit')
        self.status.setText('Saving and cleaning up before closing…')
        event.ignore()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Native single-sandbox workload playground')
    parser.add_argument('--data-dir', type=Path, help='override the desktop recording folder')
    args = parser.parse_args(argv)
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
    window = MainWindow(worker)
    window.show()
    worker.start()
    worker.submit({'op': 'check'})
    result = app.exec()
    if worker.is_alive():
        worker.submit({'op': 'quit'})
        worker.join(timeout=20)
    lock.unlock()
    return result


if __name__ == '__main__':
    raise SystemExit(main())
