"""メインウィンドウの配線テスト: 実際の再生（QMediaPlayer）と書き出し（ワーカープロセス）まで通す"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QMessageBox

from media_factory import frame_indices
from videocut.ranges import Range
from videocut.ui.main_window import MainWindow

FRAME = 1 / 60


@pytest.fixture
def window(qtbot, monkeypatch):
    messages = []

    def record(kind):
        def show(*args, **kwargs):
            messages.append((kind, args[2] if len(args) > 2 else ""))
            return QMessageBox.StandardButton.Yes

        return staticmethod(show)

    for kind in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, kind, record(kind))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: messages.append(("box", self.text())) or 0)
    w = MainWindow()
    qtbot.addWidget(w)
    w.messages = messages
    w.show()
    yield w
    w.close()


def open_video(qtbot, window, path):
    window.open_file(str(path))
    qtbot.waitUntil(lambda: window.frame_time is not None, timeout=10_000)


def show_frame(qtbot, window, n):
    window.seek_frame(n * FRAME)
    qtbot.waitUntil(lambda: window.frame_time == pytest.approx(n * FRAME, abs=1e-3), timeout=10_000)


def save_as(monkeypatch, path):
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(path), "")))


class TestOpen:
    def test_開くと長さと映像の情報を表示する(self, qtbot, window, indexed_mp4):
        open_video(qtbot, window, indexed_mp4)
        assert "00:00:10.000" in window.time_label.text()
        assert "320x180" in window.info_label.text()
        assert "60fps" in window.info_label.text()

    def test_区間があるときに別の動画を開くなら確認して区間を消す(self, qtbot, window, indexed_mp4, offset_mkv):
        open_video(qtbot, window, indexed_mp4)
        window.set_ranges([Range(1.0, 2.0)])
        window.open_file(str(offset_mkv))
        assert window.messages[-1][0] == "question"
        assert window.info.path == str(offset_mkv)
        assert window.ranges == []

    def test_確認でいいえを選ぶと今の動画と区間を残す(self, qtbot, window, indexed_mp4, offset_mkv, monkeypatch):
        open_video(qtbot, window, indexed_mp4)
        window.set_ranges([Range(1.0, 2.0)])
        monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No))
        window.open_file(str(offset_mkv))
        assert window.info.path == str(indexed_mp4)
        assert window.ranges == [Range(1.0, 2.0)]

    def test_開けないファイルはエラーを表示する(self, window, tmp_path):
        bad = tmp_path / "bad.mp4"
        bad.write_bytes(b"x" * 1000)
        window.open_file(str(bad))
        assert window.info is None
        assert window.messages[-1][0] == "critical"


class TestNavigation:
    def test_右キーで1フレーム進み左キーで戻る(self, qtbot, window, indexed_mp4):
        open_video(qtbot, window, indexed_mp4)
        show_frame(qtbot, window, 60)
        qtbot.keyClick(window, Qt.Key.Key_Right)
        qtbot.waitUntil(lambda: window.frame_time == pytest.approx(61 * FRAME, abs=1e-3), timeout=10_000)
        qtbot.keyClick(window, Qt.Key.Key_Left)
        qtbot.keyClick(window, Qt.Key.Key_Left)
        qtbot.waitUntil(lambda: window.frame_time == pytest.approx(59 * FRAME, abs=1e-3), timeout=10_000)

    def test_上下キーで前後のキーフレームへ移動する(self, qtbot, window, indexed_mp4):
        open_video(qtbot, window, indexed_mp4)
        show_frame(qtbot, window, 70)
        qtbot.keyClick(window, Qt.Key.Key_Down)
        qtbot.waitUntil(lambda: window.frame_time == pytest.approx(1.5, abs=1e-3), timeout=10_000)
        qtbot.keyClick(window, Qt.Key.Key_Up)
        qtbot.waitUntil(lambda: window.frame_time == pytest.approx(1.0, abs=1e-3), timeout=10_000)

    def test_時刻を入力するとその位置へ移動する(self, qtbot, window, indexed_mp4):
        open_video(qtbot, window, indexed_mp4)
        window.time_edit.setText("0:03.5")
        window.time_edit.returnPressed.emit()
        qtbot.waitUntil(lambda: window.frame_time == pytest.approx(3.5, abs=1e-3), timeout=10_000)


class TestRanges:
    def test_開始点と終了点のキーで区間を追加する(self, qtbot, window, indexed_mp4):
        open_video(qtbot, window, indexed_mp4)
        show_frame(qtbot, window, 60)
        qtbot.keyClick(window, Qt.Key.Key_I)
        show_frame(qtbot, window, 119)
        qtbot.keyClick(window, Qt.Key.Key_O)
        # 終了点は表示中のフレームを含む（119 フレーム目の終わり = 2.0 秒）
        assert len(window.ranges) == 1
        assert (window.ranges[0].start, window.ranges[0].end) == pytest.approx((1.0, 2.0), abs=1e-3)
        assert window.table.rowCount() == 1

    def test_モードで書き出し後の長さが変わる(self, qtbot, window, indexed_mp4):
        open_video(qtbot, window, indexed_mp4)
        window.reencode_radio.setChecked(True)
        window.set_ranges([Range(1.0, 2.0)])
        assert "00:00:01.000" in window.summary_label.text()
        window.delete_radio.setChecked(True)
        assert "00:00:09.000" in window.summary_label.text()

    def test_無劣化ではキーフレームまで広げた範囲を表示する(self, qtbot, window, indexed_mp4):
        open_video(qtbot, window, indexed_mp4)
        window.copy_radio.setChecked(True)
        window.set_ranges([Range(1.1, 1.9)])
        assert "00:00:01.000" in window.summary_label.text()
        assert window.table.item(0, 3).text() == "00:00:01.000 – 00:00:02.000"

    def test_選択した区間をDeleteキーで消す(self, qtbot, window, indexed_mp4):
        open_video(qtbot, window, indexed_mp4)
        window.set_ranges([Range(1.0, 2.0), Range(4.0, 5.0)])
        window.table.selectRow(0)
        qtbot.keyClick(window, Qt.Key.Key_Delete)
        assert window.ranges == [Range(4.0, 5.0)]

    def test_タイムラインで端を動かすと区間に反映する(self, qtbot, window, indexed_mp4):
        open_video(qtbot, window, indexed_mp4)
        window.set_ranges([Range(1.0, 2.0)])
        window.timeline.rangeEdited.emit(0, 1.0, 3.0)
        assert window.ranges == [Range(1.0, 3.0)]
        assert window.table.item(0, 1).text() == "00:00:03.000"


class TestExport:
    def test_抜き出しを書き出すと区間のファイルができる(self, qtbot, window, indexed_mp4, tmp_path, monkeypatch):
        out = tmp_path / "結果.mp4"
        save_as(monkeypatch, out)
        open_video(qtbot, window, indexed_mp4)
        window.copy_radio.setChecked(True)
        window.set_ranges([Range(1.0, 2.0)])
        with qtbot.waitSignal(window.runner.succeeded, timeout=60_000):
            window.export()
        assert frame_indices(out) == list(range(60, 120))
        assert not window.cancel_button.isEnabled()

    def test_削除を再エンコードで書き出すと区間が消える(self, qtbot, window, indexed_mp4, tmp_path, monkeypatch):
        out = tmp_path / "out.mp4"
        save_as(monkeypatch, out)
        open_video(qtbot, window, indexed_mp4)
        window.delete_radio.setChecked(True)
        window.reencode_radio.setChecked(True)
        window.set_ranges([Range(2.0, 8.0)])
        with qtbot.waitSignal(window.runner.succeeded, timeout=60_000):
            window.export()
        assert frame_indices(out) == list(range(0, 120)) + list(range(480, 600))

    def test_区間ごとに別ファイルで書き出す(self, qtbot, window, indexed_mp4, tmp_path, monkeypatch):
        save_as(monkeypatch, tmp_path / "clip.mp4")
        open_video(qtbot, window, indexed_mp4)
        window.copy_radio.setChecked(True)
        window.separate_check.setChecked(True)
        window.set_ranges([Range(1.0, 2.0), Range(4.0, 5.0)])
        with qtbot.waitSignal(window.runner.succeeded, timeout=60_000):
            window.export()
        assert frame_indices(tmp_path / "clip_01.mp4") == list(range(60, 120))
        assert frame_indices(tmp_path / "clip_02.mp4") == list(range(240, 300))

    def test_元の動画と同じファイルには書き出さない(self, qtbot, window, indexed_mp4, monkeypatch):
        save_as(monkeypatch, indexed_mp4)
        open_video(qtbot, window, indexed_mp4)
        window.set_ranges([Range(1.0, 2.0)])
        window.export()
        assert not window.runner.is_running()
        assert window.messages[-1][0] == "critical"

    def test_別の名前でも元の動画と同じ実体なら書き出さない(self, qtbot, window, tmp_path, monkeypatch):
        from media_factory import make_indexed_video

        source = make_indexed_video(tmp_path / "source.mp4", seconds=3)
        link = tmp_path / "link.mp4"
        link.hardlink_to(source)
        save_as(monkeypatch, link)
        open_video(qtbot, window, source)
        window.set_ranges([Range(1.0, 2.0)])
        window.export()
        assert not window.runner.is_running()
        assert window.messages[-1][0] == "critical"
