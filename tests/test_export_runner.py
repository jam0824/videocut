"""ExportRunner（ワーカープロセスの起動と進捗の受け取り）の統合テスト"""

from pathlib import Path

from media_factory import frame_indices
from videocut.plan import Job, Method, OutputSpec
from videocut.ranges import Range
from videocut.ui.export_runner import ExportRunner


def test_書き出しが終わると出力ファイルを通知する(qtbot, indexed_mp4, tmp_path):
    runner = ExportRunner()
    out = tmp_path / "out.mp4"
    progress = []
    runner.progressChanged.connect(progress.append)
    with qtbot.waitSignal(runner.succeeded, timeout=60_000) as blocker:
        runner.start(Job(str(indexed_mp4), Method.COPY, [OutputSpec(str(out), (Range(1.0, 2.0),))]))
        assert runner.is_running()
    assert blocker.args == [[str(out)]]
    assert progress and progress[-1] == 1.0
    assert frame_indices(out) == list(range(60, 120))
    assert not runner.is_running()


def test_失敗するとメッセージを通知する(qtbot, tmp_path):
    runner = ExportRunner()
    job = Job(str(tmp_path / "missing.mp4"), Method.COPY, [OutputSpec(str(tmp_path / "out.mp4"), (Range(0, 1),))])
    with qtbot.waitSignal(runner.failed, timeout=60_000) as blocker:
        runner.start(job)
    message, detail = blocker.args
    assert "missing.mp4" in message
    assert detail


def test_書き出しの途中で中止すると一時ファイルを残さない(qtbot, long_mp4, tmp_path):
    runner = ExportRunner()
    out = tmp_path / "out.mp4"
    runner.start(Job(str(long_mp4), Method.REENCODE, [OutputSpec(str(out), (Range(0, 120.0),))]))
    qtbot.waitUntil((tmp_path / "out.videocut-tmp.mp4").exists, timeout=60_000)
    with qtbot.waitSignal(runner.canceled, timeout=60_000):
        runner.cancel()
    assert not runner.is_running()
    assert list(Path(tmp_path).iterdir()) == []


def test_起動直後に中止しても一時ファイルを残さない(qtbot, indexed_mp4, tmp_path):
    runner = ExportRunner()
    with qtbot.waitSignal(runner.canceled, timeout=60_000):
        runner.start(Job(str(indexed_mp4), Method.REENCODE, [OutputSpec(str(tmp_path / "out.mp4"), (Range(0, 10.0),))]))
        runner.cancel()
    assert list(Path(tmp_path).iterdir()) == []


def test_アプリ終了時の中止は後始末まで待つ(qtbot, long_mp4, tmp_path):
    runner = ExportRunner()
    out = tmp_path / "out.mp4"
    runner.start(Job(str(long_mp4), Method.REENCODE, [OutputSpec(str(out), (Range(0, 120.0),))]))
    qtbot.waitUntil((tmp_path / "out.videocut-tmp.mp4").exists, timeout=60_000)
    runner.cancel_and_wait()
    assert not runner.is_running()
    assert list(Path(tmp_path).iterdir()) == []
