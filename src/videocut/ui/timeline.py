"""タイムライン: 区間と再生位置を表示し、クリックでシーク、区間の端のドラッグで範囲を調整する

ホイールで拡大縮小（カーソル位置を中心）、Shift+ホイールか下のスクロールバーで左右に移動する。
"""

from collections.abc import Callable

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QScrollBar, QSizePolicy, QWidget

from videocut.ranges import Mode, Range

MARGIN = 8
RULER_HEIGHT = 18
TRACK_TOP = RULER_HEIGHT
TRACK_HEIGHT = 34
RESULT_TOP = TRACK_TOP + TRACK_HEIGHT + 3
RESULT_HEIGHT = 7
SCROLLBAR_HEIGHT = 12
EDGE_GRAB_PX = 6
MIN_VIEW_SECONDS = 1.0
_TICK_STEPS = (0.1, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200)
_MIN_TICK_PX = 80

_BACKGROUND = QColor(30, 31, 36)
_TRACK = QColor(52, 54, 62)
_TEXT = QColor(180, 182, 190)
_RANGE_COLORS = {Mode.EXTRACT: QColor(64, 150, 88), Mode.DELETE: QColor(196, 72, 72)}
_RESULT = QColor(70, 170, 200)
_PENDING = QColor(240, 200, 60)
_PLAYHEAD = QColor(255, 255, 255)


class Timeline(QWidget):
    seekRequested = Signal(float)
    rangeClicked = Signal(int)
    rangeEdited = Signal(int, float, float)  # 区間の番号, 開始, 終了（ドラッグを離したとき）

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumHeight(RESULT_TOP + RESULT_HEIGHT + SCROLLBAR_HEIGHT + 4)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self._duration = 0.0
        self._position = 0.0
        self._ranges: list[Range] = []
        self._mode = Mode.EXTRACT
        self._result: list[Range] = []
        self._pending: float | None = None
        self._selected: int | None = None
        self._view_start = 0.0
        self._view_length = 1.0
        self._snap: Callable[[float], float] = lambda t: t
        self._min_length = 0.0
        self._drag: tuple[int, str] | None = None  # ドラッグ中の区間の端 (番号, "start" / "end")
        self._seeking = False
        self._hover_x: float | None = None
        self.scrollbar = QScrollBar(Qt.Orientation.Horizontal, self)
        self.scrollbar.valueChanged.connect(self._on_scroll)
        self._update_scrollbar()

    # ---- 表示する内容 ----

    def set_duration(self, seconds: float) -> None:
        self._duration = max(0.0, seconds)
        self._view_start, self._view_length = 0.0, max(self._duration, 1e-3)
        self._update_scrollbar()
        self.update()

    def set_position(self, seconds: float, follow: bool = False) -> None:
        self._position = seconds
        if follow and not self._seeking:
            end = self._view_start + self._view_length
            if seconds < self._view_start or seconds > end:
                self._set_view(seconds - self._view_length * 0.1, self._view_length)
        self.update()

    def set_ranges(self, ranges: list[Range], mode: Mode) -> None:
        if self._drag is None:
            self._ranges = list(ranges)
        self._mode = mode
        self.update()

    def set_result(self, ranges: list[Range]) -> None:
        """書き出し後に実際に残る部分（下の細い帯で表示する）"""
        self._result = list(ranges)
        self.update()

    def set_pending_start(self, seconds: float | None) -> None:
        self._pending = seconds
        self.update()

    def set_selected(self, index: int | None) -> None:
        self._selected = index
        self.update()

    def set_frame_snap(self, snap: Callable[[float], float], min_length: float) -> None:
        """ドラッグした端をフレームの境目に合わせる関数と、区間の最短の長さ（1フレーム）"""
        self._snap = snap
        self._min_length = min_length

    # ---- 表示範囲 ----

    @property
    def view_start(self) -> float:
        return self._view_start

    @property
    def view_length(self) -> float:
        return self._view_length

    def zoom(self, factor: float, anchor: float) -> None:
        if self._duration <= 0:
            return
        length = min(max(self._view_length * factor, min(MIN_VIEW_SECONDS, self._duration)), self._duration)
        ratio = (anchor - self._view_start) / self._view_length
        self._set_view(anchor - ratio * length, length)

    def _set_view(self, start: float, length: float) -> None:
        self._view_length = length
        self._view_start = min(max(start, 0.0), max(0.0, self._duration - length))
        self._update_scrollbar()
        self.update()

    def _update_scrollbar(self) -> None:
        self.scrollbar.blockSignals(True)
        self.scrollbar.setRange(0, max(0, round((self._duration - self._view_length) * 1000)))
        self.scrollbar.setPageStep(max(1, round(self._view_length * 1000)))
        self.scrollbar.setSingleStep(max(1, round(self._view_length * 100)))
        self.scrollbar.setValue(round(self._view_start * 1000))
        self.scrollbar.blockSignals(False)
        self.scrollbar.setVisible(self._view_length < self._duration - 1e-6)

    def _on_scroll(self, value: int) -> None:
        self._view_start = value / 1000
        self.update()

    # ---- 座標 ----

    def _track_width(self) -> float:
        return max(1.0, self.width() - 2 * MARGIN)

    def x_at(self, t: float) -> float:
        return MARGIN + (t - self._view_start) / self._view_length * self._track_width()

    def time_at(self, x: float) -> float:
        t = self._view_start + (x - MARGIN) / self._track_width() * self._view_length
        return min(max(t, 0.0), self._duration)

    def _edge_at(self, x: float) -> tuple[int, str] | None:
        best = None
        best_distance = EDGE_GRAB_PX + 1
        order = sorted(range(len(self._ranges)), key=lambda i: i != self._selected)  # 選択中の区間を優先
        for i in order:
            r = self._ranges[i]
            for side, t in (("start", r.start), ("end", r.end)):
                distance = abs(self.x_at(t) - x)
                if distance < best_distance:
                    best, best_distance = (i, side), distance
        return best

    def _range_at(self, t: float) -> int | None:
        for i, r in enumerate(self._ranges):
            if r.start <= t < r.end:
                return i
        return None

    # ---- マウス操作 ----

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._duration <= 0:
            return
        x, y = event.position().x(), event.position().y()
        edge = self._edge_at(x) if y < RESULT_TOP + RESULT_HEIGHT else None
        if edge is not None:
            self._drag = edge
            return
        t = self.time_at(x)
        index = self._range_at(t)
        if index is not None and TRACK_TOP <= y < TRACK_TOP + TRACK_HEIGHT:
            self.rangeClicked.emit(index)
        self._seeking = True
        self.seekRequested.emit(t)

    def mouseMoveEvent(self, event) -> None:
        x = event.position().x()
        self._hover_x = x
        if self._drag is not None:
            self._drag_edge_to(self.time_at(x))
        elif self._seeking:
            self.seekRequested.emit(self.time_at(x))
        else:
            near_edge = self._edge_at(x) is not None and event.position().y() < RESULT_TOP + RESULT_HEIGHT
            self.setCursor(Qt.CursorShape.SizeHorCursor if near_edge else Qt.CursorShape.ArrowCursor)
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._drag is not None:
            index, _ = self._drag
            self._drag = None
            r = self._ranges[index]
            self.rangeEdited.emit(index, r.start, r.end)
        self._seeking = False

    def leaveEvent(self, event) -> None:
        self._hover_x = None
        self.update()

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta()
        steps = (delta.y() or delta.x()) / 120
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier or abs(delta.x()) > abs(delta.y()):
            self._set_view(self._view_start - steps * self._view_length * 0.1, self._view_length)
        else:
            self.zoom(0.8**steps, self.time_at(event.position().x()))
        event.accept()

    def _drag_edge_to(self, t: float) -> None:
        index, side = self._drag
        r = self._ranges[index]
        t = self._snap(t)
        if side == "start":
            low = self._ranges[index - 1].end if index > 0 else 0.0
            start = min(max(t, low), r.end - max(self._min_length, 1e-6))
            self._ranges[index] = Range(start, r.end)
            self.seekRequested.emit(start)
        else:
            high = self._ranges[index + 1].start if index + 1 < len(self._ranges) else self._duration
            end = max(min(t, high), r.start + max(self._min_length, 1e-6))
            self._ranges[index] = Range(r.start, end)
            self.seekRequested.emit(max(r.start, end - self._min_length))  # 区間に含まれる最後のフレームを見せる

    def resizeEvent(self, event) -> None:
        self.scrollbar.setGeometry(MARGIN, self.height() - SCROLLBAR_HEIGHT, self.width() - 2 * MARGIN, SCROLLBAR_HEIGHT)
        super().resizeEvent(event)

    # ---- 描画 ----

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), _BACKGROUND)
        width = self._track_width()
        p.fillRect(QRectF(MARGIN, TRACK_TOP, width, TRACK_HEIGHT), _TRACK)
        if self._duration <= 0:
            return
        p.setClipRect(QRectF(MARGIN, 0, width, self.height()))
        self._paint_ruler(p)

        color = _RANGE_COLORS[self._mode]
        for i, r in enumerate(self._ranges):
            rect = QRectF(self.x_at(r.start), TRACK_TOP + 2, max(1.0, self.x_at(r.end) - self.x_at(r.start)), TRACK_HEIGHT - 4)
            p.fillRect(rect, color.lighter(130) if i == self._selected else color)
            if i == self._selected:
                p.setPen(QPen(QColor(255, 255, 255), 2))
                p.drawRect(rect)

        for r in self._result:
            p.fillRect(QRectF(self.x_at(r.start), RESULT_TOP, max(1.0, self.x_at(r.end) - self.x_at(r.start)), RESULT_HEIGHT), _RESULT)

        if self._pending is not None:
            x = self.x_at(self._pending)
            p.setPen(QPen(_PENDING, 2, Qt.PenStyle.DashLine))
            p.drawLine(round(x), TRACK_TOP, round(x), RESULT_TOP + RESULT_HEIGHT)

        if self._hover_x is not None:
            p.setPen(QPen(QColor(255, 255, 255, 70), 1))
            p.drawLine(round(self._hover_x), TRACK_TOP, round(self._hover_x), RESULT_TOP + RESULT_HEIGHT)

        x = self.x_at(self._position)
        p.setPen(QPen(_PLAYHEAD, 2))
        p.drawLine(round(x), TRACK_TOP - 4, round(x), RESULT_TOP + RESULT_HEIGHT)
        p.setBrush(_PLAYHEAD)
        p.drawPolygon(QPolygonF([QPointF(x - 5, TRACK_TOP - 9), QPointF(x + 5, TRACK_TOP - 9), QPointF(x, TRACK_TOP - 3)]))

        if self._hover_x is not None:
            label = _format_label(self.time_at(self._hover_x), digits=3)
            p.setPen(_TEXT)
            p.drawText(QRectF(self._hover_x + 6, TRACK_TOP + 2, 120, 14), Qt.AlignmentFlag.AlignLeft, label)

    def _paint_ruler(self, p: QPainter) -> None:
        width = self._track_width()
        step = next((s for s in _TICK_STEPS if s / self._view_length * width >= _MIN_TICK_PX), _TICK_STEPS[-1])
        digits = 0 if step >= 1 else 1
        p.setPen(_TEXT)
        k = int(self._view_start // step)
        end = self._view_start + self._view_length
        while k * step <= end:
            x = self.x_at(k * step)
            p.drawLine(round(x), RULER_HEIGHT - 5, round(x), RULER_HEIGHT)
            p.drawText(QRectF(x + 3, 0, 100, RULER_HEIGHT - 3), Qt.AlignmentFlag.AlignLeft, _format_label(k * step, digits))
            k += 1


def _format_label(t: float, digits: int) -> str:
    """目盛りのラベル。1時間未満なら M:SS、digits 桁の小数も付ける"""
    total = round(max(0.0, t), digits)
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    sec = f"{seconds:0{3 + digits}.{digits}f}" if digits else f"{int(seconds):02d}"
    if hours >= 1:
        return f"{int(hours)}:{int(minutes):02d}:{sec}"
    return f"{int(minutes)}:{sec}"
