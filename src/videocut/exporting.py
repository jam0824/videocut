"""書き出し処理（無劣化・再エンコード）で共通に使う部品"""

import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress

from videocut.plan import temp_path_for

ProgressFn = Callable[[float], None]
CancelFn = Callable[[], bool]

# 書き出し先へ引き継ぐストリームのメタデータ（MKV の DURATION など元動画の長さに依存するものは除く）
_STREAM_METADATA = ("language", "title", "handler_name")


class Cancelled(Exception):
    """利用者が書き出しを中止した"""


class Progress:
    """進捗（0〜1）を一定間隔で通知する。値は減らない"""

    def __init__(self, callback: ProgressFn | None, total: float, interval: float = 0.2):
        self._callback = callback
        self._total = total if total > 0 else 1.0
        self._interval = interval
        self._last_time = 0.0
        self._done = 0.0

    def update(self, done: float) -> None:
        self._done = max(self._done, min(done, self._total))
        now = time.monotonic()
        if self._callback and now - self._last_time >= self._interval:
            self._last_time = now
            self._callback(self._done / self._total)

    def finish(self) -> None:
        if self._callback:
            self._callback(1.0)


def copy_stream_metadata(src, dst) -> None:
    for key in _STREAM_METADATA:
        if key in src.metadata:
            dst.metadata[key] = src.metadata[key]
    dst.disposition = src.disposition


def check_cancel(should_cancel: CancelFn | None) -> None:
    if should_cancel and should_cancel():
        raise Cancelled


@contextmanager
def atomic_output(path: str) -> Iterator[str]:
    """一時ファイルに書き、成功したときだけ本来の名前に置き換える"""
    tmp = temp_path_for(path)
    try:
        yield tmp
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            os.remove(tmp)
        raise
