"""書き出しワーカー（videocut.worker）を別プロセスで動かし、進捗と結果をシグナルで伝える"""

import json
import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from videocut.plan import Job, temp_path_for

# 中止を伝えても、この時間内に終わらなければ強制終了する
_KILL_AFTER_MS = 5000
# 強制終了の直後はまだファイルが開かれていることがあるので、消せるまでこの秒数だけ待つ
_TEMP_REMOVE_WAIT_SECONDS = 3.0


def _worker_python() -> str:
    """コンソール窓を出さないよう、あれば pythonw.exe で起動する"""
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    return str(windowless if windowless.exists() else exe)


class ExportRunner(QObject):
    progressChanged = Signal(float)
    succeeded = Signal(list)  # 出力ファイルのパス
    failed = Signal(str, str)  # メッセージ, 詳細
    canceled = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._process: QProcess | None = None
        self._job: Job | None = None
        self._buffer = b""
        self._result: dict = {}
        self._canceling = False

    def is_running(self) -> bool:
        return self._process is not None

    def start(self, job: Job) -> None:
        if self._process is not None:
            raise RuntimeError("書き出し中です")
        self._job, self._buffer, self._result, self._canceling = job, b"", {}, False
        process = QProcess(self)
        process.setProgram(_worker_python())
        process.setArguments(["-m", "videocut.worker"])
        process.readyReadStandardOutput.connect(self._read_output)
        process.finished.connect(self._on_finished)
        process.errorOccurred.connect(self._on_error)
        self._process = process
        process.start()
        # 標準入力は閉じずに残し、中止の指示に使う
        process.write(json.dumps(job.to_dict()).encode("utf-8") + b"\n")

    def cancel(self) -> None:
        """ワーカーに中止を伝える。ワーカーは一時ファイルを消してから終わる"""
        process = self._process
        if process is None or self._canceling:
            return
        self._canceling = True
        process.write(b"cancel\n")
        QTimer.singleShot(_KILL_AFTER_MS, lambda: self._kill(process))

    def cancel_and_wait(self, msecs: int = _KILL_AFTER_MS) -> None:
        """アプリ終了時用: 中止して、一時ファイルの後始末が終わるまで待つ"""
        process = self._process
        if process is None:
            return
        self.cancel()
        if not process.waitForFinished(msecs):
            process.kill()
            process.waitForFinished(msecs)

    def _kill(self, process: QProcess) -> None:
        if self._process is process:
            process.kill()

    def _read_output(self) -> None:
        if self._process is None:
            return
        self._buffer += self._process.readAllStandardOutput().data()
        *lines, self._buffer = self._buffer.split(b"\n")
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") == "progress":
                self.progressChanged.emit(float(event["value"]))
            else:
                self._result = event

    def _on_error(self, error: QProcess.ProcessError) -> None:
        # 起動に失敗したときは finished が来ないのでここで終える
        if error == QProcess.ProcessError.FailedToStart and self._process is not None:
            detail = self._process.errorString()
            self._close_process()
            self.failed.emit("書き出し用のプロセスを起動できませんでした", detail)

    def _on_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        self._read_output()
        stderr = self._process.readAllStandardError().data().decode("utf-8", "replace")
        job = self._job
        self._close_process()
        if self._canceling:
            _remove_temp_files(job)
            self.canceled.emit()
        elif exit_status == QProcess.ExitStatus.NormalExit and exit_code == 0 and self._result.get("event") == "done":
            self.succeeded.emit(self._result["outputs"])
        else:
            _remove_temp_files(job)
            message = self._result.get("message") or f"書き出し処理が異常終了しました（終了コード {exit_code}）"
            self.failed.emit(message, self._result.get("detail") or stderr[-4000:])

    def _close_process(self) -> None:
        self._process.deleteLater()
        self._process = None


def _remove_temp_files(job: Job | None) -> None:
    pending = [temp_path_for(o.path) for o in job.outputs] if job else []
    deadline = time.monotonic() + _TEMP_REMOVE_WAIT_SECONDS
    while pending:
        locked = []
        for path in pending:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError:
                locked.append(path)
        if not locked or time.monotonic() > deadline:
            return
        pending = locked
        time.sleep(0.05)
