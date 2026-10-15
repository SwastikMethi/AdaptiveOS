"""Bounded native Qt time-series plots, with explicit missing-data gaps."""

from collections import deque
import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

COLORS = ['#2463a8', '#177e89', '#a76618', '#914d99', '#cf4e46', '#56723c', '#5b68ad', '#8c604b']


class TimeSeriesChart(QWidget):
    def __init__(self, title, unit, parent=None):
        super().__init__(parent)
        self.title, self.unit = title, unit
        self.series = {}
        self.labels = {}
        self.color_indexes = {}
        self.window_seconds = 300
        self.gap_seconds = 2.5
        self.markers = {}
        self.selected_marker = None
        self.setMinimumHeight(160)
        self.setAccessibleName(f'{title} chart, {unit}; numeric values are in the workload table and details')

    def append(self, key, seconds, value):
        if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value)):
            value = None
        points = self.series.setdefault(key, deque(maxlen=300))
        points.append((seconds, value))
        self.update()

    def clear(self):
        self.series.clear()
        self.markers.clear()
        self.selected_marker = None
        self.update()

    def visible_range(self):
        end = max((point[0] for points in self.series.values() for point in points), default=0)
        return max(0, end - self.window_seconds), end

    def mark_acknowledgement(self, key, seconds, label):
        self.markers[key] = (seconds, label)
        if len(self.markers) > 100:
            self.markers.pop(next(iter(self.markers)))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor('#ffffff'))
        painter.setPen(QColor('#243247'))
        painter.drawText(12, 22, f'{self.title} ({self.unit})')
        plot = QRectF(48, 36, max(1, self.width() - 65), max(1, self.height() - 95))
        all_points = [point for points in self.series.values() for point in points]
        end = max((p[0] for p in all_points), default=0)
        start = max(0, end - self.window_seconds)
        width = max(10, end - start)
        maximum = max((p[1] for p in all_points if p[1] is not None and p[0] >= start), default=0)
        maximum = max(100 if self.unit == '%' else 1, maximum * 1.1)
        painter.setPen(QColor('#dce3ec'))
        for fraction in (0, .5, 1):
            y = plot.bottom() - fraction * plot.height()
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(QColor('#64748b'))
            label = f'{maximum * fraction:.1f}' if maximum < 10 else f'{maximum * fraction:.0f}'
            painter.drawText(QRectF(0, y - 8, 42, 18), Qt.AlignmentFlag.AlignRight, label)
            painter.setPen(QColor('#dce3ec'))
        painter.setPen(QColor('#64748b'))
        painter.drawText(int(plot.left()), int(plot.bottom() + 18), f'{start:.0f} s')
        painter.drawText(int(plot.right() - 52), int(plot.bottom() + 18), f'{end:.0f} s')
        markers = [(seconds, label, False) for seconds, label in self.markers.values()]
        if self.selected_marker:
            markers.append((*self.selected_marker, True))
        for seconds, label, selected in markers:
            if start <= seconds <= end:
                x = plot.left() + (seconds - start) / width * plot.width()
                painter.setPen(QPen(QColor('#9b5300' if selected else '#94a3b8'), 2 if selected else 1,
                                    Qt.PenStyle.DashLine))
                painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
                if selected:
                    painter.drawText(int(min(x + 4, plot.right() - 180)), int(plot.top() + 14), label)
        for index, (key, points) in enumerate(self.series.items()):
            color = QColor(COLORS[self.color_indexes.get(key, index) % len(COLORS)])
            path = QPainterPath()
            connected = False
            previous_time = None
            for seconds, value in points:
                if seconds < start:
                    continue
                if value is None:
                    connected = False
                    continue
                point = QPointF(plot.left() + (seconds - start) / width * plot.width(),
                                plot.bottom() - value / maximum * plot.height())
                painter.setPen(QPen(color, 2))
                painter.drawPoint(point)
                if connected and previous_time is not None and seconds - previous_time <= self.gap_seconds:
                    path.lineTo(point)
                else:
                    path.moveTo(point)
                connected, previous_time = True, seconds
            painter.setPen(QPen(color, 2))
            painter.drawPath(path)
            # Two rows of textual legends for up to eight competing workloads.
            column, row = index % 4, index // 4
            x = 12 + column * (self.width() - 24) / 4
            y = self.height() - 22 + row * 15
            painter.drawText(int(x), y, self.labels.get(key, key)[:21])
