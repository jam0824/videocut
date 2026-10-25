"""書き出しを別プロセスで実行する（UI を固まらせないため）

標準入力の1行目でジョブ（JSON）を受け取り、経過を1行1つの JSON で標準出力に書く。
  {"event": "progress", "value": 0.42}
  {"event": "done", "outputs": [...]}
  {"event": "canceled"}
  {"event": "error", "message": "...", "detail": "..."}
その後に標準入力へ "cancel" の行が来たら、一時ファイルを消して中止する（終了コード 2）。
"""

import json
import os
import sys
import threading
import traceback
from collections.abc import Iterator

from videocut.copycut import copy_cut
from videocut.exporting import Cancelled
from videocut.plan import Job, Method
from videocut.reencode import reencode_cut


def _emit(**event) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def _read_lines(fd: int) -> Iterator[bytes]:
    # sys.stdin（バッファ付き）を別スレッドで読みかけのまま終了すると、終了処理がロック待ちで止まる。
    # ロックを持たない os.read で直接読む
    buffer = b""
    while chunk := os.read(fd, 4096):
        buffer += chunk
        *lines, buffer = buffer.split(b"\n")
        yield from lines
    if buffer:
        yield buffer


def _watch_for_cancel(lines: Iterator[bytes], canceled: threading.Event) -> None:
    for line in lines:
        if line.strip() == b"cancel":
            canceled.set()
            return


def main() -> int:
    canceled = threading.Event()
    lines = _read_lines(sys.stdin.fileno())
    try:
        job = Job.from_dict(json.loads(next(lines, b"").decode("utf-8")))
        threading.Thread(target=_watch_for_cancel, args=(lines, canceled), daemon=True).start()
        cut = copy_cut if job.method is Method.COPY else reencode_cut
        cut(
            job.source,
            job.outputs,
            on_progress=lambda value: _emit(event="progress", value=round(value, 4)),
            should_cancel=canceled.is_set,
        )
    except Cancelled:
        _emit(event="canceled")
        return 2
    except Exception as e:  # noqa: BLE001 - 理由を問わず UI へ伝える
        _emit(event="error", message=str(e) or type(e).__name__, detail=traceback.format_exc())
        return 1
    _emit(event="done", outputs=[o.path for o in job.outputs])
    return 0


if __name__ == "__main__":
    sys.exit(main())
