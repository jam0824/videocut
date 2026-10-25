import pytest
from PySide6.QtCore import QPoint, Qt

from videocut.ranges import Mode, Range
from videocut.ui.timeline import MARGIN, TRACK_HEIGHT, TRACK_TOP, Timeline, _format_label

TRACK_Y = TRACK_TOP + TRACK_HEIGHT // 2


@pytest.fixture
def timeline(qtbot):
    tl = Timeline()
    qtbot.addWidget(tl)
    tl.resize(1000 + 2 * MARGIN, tl.minimumHeight())
    tl.set_duration(100.0)
    return tl


def point(tl, t, y=TRACK_Y):
    return QPoint(round(tl.x_at(t)), y)


class TestGeometry:
    def test_時刻と横位置を相互に変換できる(self, timeline):
        assert timeline.x_at(50) == pytest.approx(MARGIN + 500)
        assert timeline.time_at(timeline.x_at(12.5)) == pytest.approx(12.5)

    def test_ズームしてもカーソル位置の時刻は同じ場所に残る(self, timeline):
        before = timeline.x_at(30)
        timeline.zoom(0.5, anchor=30)
        assert timeline.view_length == pytest.approx(50)
        assert timeline.x_at(30) == pytest.approx(before)

    def test_動画の長さより広くはズームアウトしない(self, timeline):
        timeline.zoom(4, anchor=50)
        assert (timeline.view_start, timeline.view_length) == pytest.approx((0, 100))

    def test_再生位置が表示範囲の外に出たら追従する(self, timeline):
        timeline.zoom(0.1, anchor=0)
        timeline.set_position(15, follow=True)
        assert timeline.view_start <= 15 <= timeline.view_start + timeline.view_length


class TestLabel:
    @pytest.mark.parametrize(
        ("t", "digits", "expected"),
        [(119.99999, 0, "2:00"), (3725.5, 1, "1:02:05.5"), (65.4321, 3, "1:05.432"), (0, 0, "0:00")],
    )
    def test_目盛りの時刻を丸めて表示する(self, t, digits, expected):
        assert _format_label(t, digits) == expected


class TestMouse:
    def test_クリックした位置へのシークを要求する(self, qtbot, timeline):
        with qtbot.waitSignal(timeline.seekRequested) as blocker:
            qtbot.mouseClick(timeline, Qt.MouseButton.LeftButton, pos=point(timeline, 25))
        assert blocker.args[0] == pytest.approx(25, abs=0.15)

    def test_区間をクリックすると選択を通知する(self, qtbot, timeline):
        timeline.set_ranges([Range(10, 20)], Mode.EXTRACT)
        with qtbot.waitSignal(timeline.rangeClicked) as blocker:
            qtbot.mouseClick(timeline, Qt.MouseButton.LeftButton, pos=point(timeline, 15))
        assert blocker.args == [0]

    def test_区間の端をドラッグすると変更後の区間を通知する(self, qtbot, timeline):
        timeline.set_ranges([Range(10, 20)], Mode.EXTRACT)
        with qtbot.waitSignal(timeline.rangeEdited) as blocker:
            qtbot.mousePress(timeline, Qt.MouseButton.LeftButton, pos=point(timeline, 20))
            qtbot.mouseMove(timeline, pos=point(timeline, 30))
            qtbot.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=point(timeline, 30))
        index, start, end = blocker.args
        assert index == 0
        assert (start, end) == pytest.approx((10, 30), abs=0.15)

    def test_ドラッグした端はフレームの境目に吸着する(self, qtbot, timeline):
        timeline.set_ranges([Range(10, 20)], Mode.EXTRACT)
        timeline.set_frame_snap(lambda t: round(t), min_length=1.0)
        with qtbot.waitSignal(timeline.rangeEdited) as blocker:
            qtbot.mousePress(timeline, Qt.MouseButton.LeftButton, pos=point(timeline, 10))
            qtbot.mouseMove(timeline, pos=point(timeline, 4.3))
            qtbot.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=point(timeline, 4.3))
        assert blocker.args[1:] == [4, 20]

    def test_端は隣の区間を越えて動かせない(self, qtbot, timeline):
        timeline.set_ranges([Range(10, 20), Range(30, 40)], Mode.DELETE)
        with qtbot.waitSignal(timeline.rangeEdited) as blocker:
            qtbot.mousePress(timeline, Qt.MouseButton.LeftButton, pos=point(timeline, 20))
            qtbot.mouseMove(timeline, pos=point(timeline, 50))
            qtbot.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=point(timeline, 50))
        assert blocker.args[2] == pytest.approx(30)
