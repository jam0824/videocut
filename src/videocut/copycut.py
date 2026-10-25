"""無劣化カット: 圧縮されたパケットをそのままコピーして区間をつなげる

再エンコードしないので画質は落ちず、速度はほぼディスクの読み書き速度で決まる。
映像はキーフレームからしか再生を始められないため、区間はキーフレームの位置に広げて切る。
"""

from collections.abc import Sequence
from fractions import Fraction

import av

from videocut.exporting import (
    CancelFn,
    Cancelled,
    Progress,
    ProgressFn,
    atomic_output,
    check_cancel,
    copy_stream_metadata,
)
from videocut.media import KeyframeIndex, MediaInfo, open_media, probe
from videocut.plan import OutputSpec
from videocut.ranges import SNAP_TOLERANCE, Range, snap_outward, total_length

__all__ = ["Cancelled", "copy_cut"]

# 区間の終わりを過ぎてもこの秒数までは読む（音声パケットが映像より後ろに置かれていることがあるため）
_READ_PAST_END = 3.0
_CANCEL_CHECK_PACKETS = 200


def copy_cut(
    source: str,
    outputs: Sequence[OutputSpec],
    on_progress: ProgressFn | None = None,
    should_cancel: CancelFn | None = None,
) -> None:
    info = probe(source)
    keyframes = KeyframeIndex(source, info)
    try:
        plans = [(spec.path, snap_outward(spec.ranges, keyframes, info.duration)) for spec in outputs]
    finally:
        keyframes.close()

    progress = Progress(on_progress, sum(total_length(ranges) for _, ranges in plans))
    done = 0.0
    for path, ranges in plans:
        with atomic_output(path) as tmp:
            with open_media(source) as inp, av.open(tmp, "w") as out:
                _remux(inp, out, info, ranges, lambda t, base=done: progress.update(base + t), should_cancel)
        done += total_length(ranges)
    progress.finish()


def _remux(inp, out, info: MediaInfo, ranges: Sequence[Range], report, should_cancel: CancelFn | None) -> None:
    video = inp.streams[info.video.index]
    audios = [inp.streams[a.index] for a in info.audio]
    sources = [video, *audios]
    targets = {s.index: _add_stream(out, s) for s in sources}

    last_dts: dict[int, int] = {}
    offset = Fraction(0)  # 出力側での区間の開始時刻
    count = 0
    for i, r in enumerate(ranges):
        shift = offset - Fraction(r.start) - Fraction(info.start_time)
        inp.seek(
            int((max(r.start - 1.0, 0.0) + info.start_time) / video.time_base),
            stream=video,
            backward=True,
            any_frame=False,
        )
        video_started = video_done = False
        audio_done: set[int] = set()
        for packet in inp.demux(sources):
            ts = packet.pts if packet.pts is not None else packet.dts
            if ts is None:
                continue
            stream = packet.stream
            t = float(ts * stream.time_base) - info.start_time

            if stream.index == video.index:
                if packet.is_keyframe and t >= r.end - SNAP_TOLERANCE:
                    video_done = True
                if video_done:
                    if len(audio_done) == len(audios) or t > r.end + _READ_PAST_END:
                        break
                    continue
                if not video_started:
                    if not (packet.is_keyframe and t >= r.start - SNAP_TOLERANCE):
                        continue
                    video_started = True
                if t < r.start - SNAP_TOLERANCE:
                    continue  # オープンGOPの先行フレーム（前のGOPを参照するので再生できない）
                report(float(offset) + t - r.start)
            else:
                if stream.index in audio_done:
                    continue
                # 音声パケットは約21ms単位。中心が区間内にあるものを残す
                duration = float(packet.duration * stream.time_base) if packet.duration else 0.0
                half = duration / 2
                if i == 0:
                    # AAC は直前のパケットが無いと頭が欠けるので、先頭の区間だけは直前の1パケットも残す。
                    # 出力では負の時刻になり、MP4 ではエディットリストで再生時に飛ばされる
                    if t + duration < r.start - 1e-6:
                        continue
                elif t + half < r.start:
                    continue
                if t + half >= r.end:
                    audio_done.add(stream.index)
                    continue

            _write(out, packet, targets[stream.index], shift, last_dts)
            count += 1
            if count % _CANCEL_CHECK_PACKETS == 0:
                check_cancel(should_cancel)
        offset += Fraction(r.end) - Fraction(r.start)


def _add_stream(out, template):
    stream = out.add_stream_from_template(template)
    copy_stream_metadata(template, stream)
    return stream


def _write(out, packet, target, shift: Fraction, last_dts: dict[int, int]) -> None:
    ticks = round(shift / packet.stream.time_base)
    if packet.pts is not None:
        packet.pts += ticks
    if packet.dts is not None:
        packet.dts += ticks
        previous = last_dts.get(target.index)
        if previous is not None and packet.dts <= previous:
            if target.type != "video":
                return  # つなぎ目で前の区間と重なる音声パケットは捨てる
            packet.dts = previous + 1
        last_dts[target.index] = packet.dts
    packet.stream = target
    out.mux(packet)
