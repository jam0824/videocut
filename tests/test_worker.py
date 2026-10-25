"""書き出しワーカー（別プロセス）の統合テスト: 実際にプロセスを起動してやり取りする"""

import json
import subprocess
import sys
import threading
import time

from media_factory import frame_indices
from videocut.plan import Job, Method, OutputSpec
from videocut.ranges import Range


def run_worker(job: Job):
    proc = subprocess.run(
        [sys.executable, "-m", "videocut.worker"],
        input=json.dumps(job.to_dict()).encode("utf-8"),
        capture_output=True,
        timeout=120,
    )
    events = [json.loads(line) for line in proc.stdout.decode("ascii").splitlines() if line.strip()]
    return proc.returncode, events


def test_無劣化のジョブを実行して完了を通知する(indexed_mp4, tmp_path):
    out = tmp_path / "出力.mp4"
    code, events = run_worker(Job(str(indexed_mp4), Method.COPY, [OutputSpec(str(out), (Range(1.0, 2.0),))]))
    assert code == 0
    assert events[-1] == {"event": "done", "outputs": [str(out)]}
    assert any(e["event"] == "progress" for e in events)
    assert frame_indices(out) == list(range(60, 120))


def test_再エンコードのジョブを実行する(indexed_mp4, tmp_path):
    out = tmp_path / "out.mp4"
    code, events = run_worker(Job(str(indexed_mp4), Method.REENCODE, [OutputSpec(str(out), (Range(1.0, 1.5),))]))
    assert code == 0
    assert events[-1]["event"] == "done"
    assert frame_indices(out) == list(range(60, 90))


def test_cancelの行を受け取ると中止して一時ファイルを残さない(long_mp4, tmp_path):
    out = tmp_path / "out.mp4"
    job = Job(str(long_mp4), Method.REENCODE, [OutputSpec(str(out), (Range(0, 120.0),))])
    proc = subprocess.Popen(
        [sys.executable, "-m", "videocut.worker"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    watchdog = threading.Timer(60, proc.kill)  # 応答が無いときにテストが止まったままにならないように
    watchdog.start()
    proc.stdin.write(json.dumps(job.to_dict()).encode("utf-8") + b"\n")
    proc.stdin.flush()
    assert json.loads(proc.stdout.readline())["event"] == "progress"
    temp = tmp_path / "out.videocut-tmp.mp4"
    deadline = time.monotonic() + 30
    while not temp.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert temp.exists()

    proc.stdin.write(b"cancel\n")
    proc.stdin.flush()
    rest, _ = proc.communicate(timeout=60)
    watchdog.cancel()
    events = [json.loads(line) for line in rest.decode("ascii").splitlines() if line.strip()]
    assert proc.returncode == 2
    assert events[-1] == {"event": "canceled"}
    assert list(tmp_path.iterdir()) == []


def test_失敗したらエラーを通知して終了コード1で終わる(tmp_path):
    job = Job(str(tmp_path / "無い.mp4"), Method.COPY, [OutputSpec(str(tmp_path / "out.mp4"), (Range(0, 1),))])
    code, events = run_worker(job)
    assert code == 1
    assert events[-1]["event"] == "error"
    assert "無い.mp4" in events[-1]["message"]
