"""動画ファイルの情報取得とキーフレーム検索

時刻はすべて「コンテナの先頭（start_time）からの秒」で扱う。
Qt の QMediaPlayer の再生位置も同じ基準になっている。
"""

import bisect
import math
import os
from dataclasses import dataclass
from fractions import Fraction

import av
from av.error import FFmpegError


class MediaError(Exception):
    pass


@dataclass(frozen=True)
class VideoInfo:
    index: int
    codec: str
    width: int
    height: int
    fps: Fraction
    time_base: Fraction
    pix_fmt: str | None
    bit_rate: int | None
    start: float  # 最初のフレームの時刻

    @property
    def frame_duration(self) -> float:
        return float(1 / self.fps)


@dataclass(frozen=True)
class AudioInfo:
    index: int
    codec: str
    sample_rate: int
    channels: int
    bit_rate: int | None


@dataclass(frozen=True)
class MediaInfo:
    path: str
    format_name: str
    duration: float
    start_time: float  # コンテナの start_time（絶対値の秒）。各ストリームの時刻からこれを引いて使う
    size: int
    video: VideoInfo
    audio: tuple[AudioInfo, ...]


def open_media(path: str) -> av.container.InputContainer:
    try:
        return av.open(path)
    except (FFmpegError, OSError) as e:
        raise MediaError(f"動画ファイルとして開けません: {path}\n{e}") from e


def pick_video_stream(container: av.container.InputContainer) -> av.VideoStream | None:
    """カバー画像を除いた最初の映像ストリーム"""
    for stream in container.streams.video:
        if not stream.disposition & av.stream.Disposition.attached_pic:
            return stream
    return None


def container_start(container: av.container.InputContainer) -> float:
    return container.start_time / 1e6 if container.start_time is not None else 0.0


def probe(path: str) -> MediaInfo:
    with open_media(path) as c:
        video = pick_video_stream(c)
        if video is None:
            raise MediaError(f"映像トラックがありません: {path}")
        start = container_start(c)
        audio = tuple(
            AudioInfo(
                index=s.index,
                codec=s.codec_context.name,
                sample_rate=s.sample_rate,
                channels=s.channels,
                bit_rate=s.bit_rate or None,
            )
            for s in c.streams.audio
        )
        return MediaInfo(
            path=path,
            format_name=c.format.name,
            duration=_duration(c, start),
            start_time=start,
            size=os.path.getsize(path),
            video=VideoInfo(
                index=video.index,
                codec=video.codec_context.name,
                width=video.codec_context.width,
                height=video.codec_context.height,
                fps=Fraction(video.average_rate or video.guessed_rate or video.base_rate or 30),
                time_base=video.time_base,
                pix_fmt=video.codec_context.pix_fmt,
                bit_rate=video.bit_rate or _estimate_video_bit_rate(c, audio),
                start=float(video.start_time * video.time_base) - start if video.start_time is not None else 0.0,
            ),
            audio=audio,
        )


def _duration(c: av.container.InputContainer, start: float) -> float:
    if c.duration is not None:
        return c.duration / 1e6
    ends = [
        float((s.start_time or 0) * s.time_base + s.duration * s.time_base) - start
        for s in c.streams
        if s.duration is not None
    ]
    if not ends:
        raise MediaError("動画の長さが分かりません")
    return max(ends)


def _estimate_video_bit_rate(c: av.container.InputContainer, audio: tuple[AudioInfo, ...]) -> int | None:
    """MKV など映像単体のビットレートが記録されない形式向けに、全体から音声分を引いて推定する"""
    if not c.bit_rate:
        return None
    rest = c.bit_rate - sum(a.bit_rate or 160_000 for a in audio)
    return rest if rest > 0 else None


class KeyframeIndex:
    """映像のキーフレーム位置を、問い合わせのあった付近だけ調べる（調べた範囲はキャッシュする）

    ファイル全体を読むと数十GBの録画では時間がかかるため、シークして近くのパケットだけ読む。
    """

    def __init__(self, path: str, info: MediaInfo):
        self._container = open_media(path)
        self._stream = self._container.streams[info.video.index]
        self._start = info.start_time
        self._gops: list[tuple[float, float | None]] = []  # 調べ済みの [キーフレーム, 次のキーフレーム)
        self._first: float | None = None

    def close(self) -> None:
        self._container.close()

    def at_or_before(self, t: float) -> float | None:
        return self._lookup(t)[0]

    def after(self, t: float) -> float | None:
        return self._lookup(t)[1]

    def _lookup(self, t: float) -> tuple[float | None, float | None]:
        i = bisect.bisect_right(self._gops, t, key=lambda gop: gop[0]) - 1
        if i >= 0:
            k0, k1 = self._gops[i]
            if k1 is None or t < k1:
                return k0, k1
        if self._first is not None and t < self._first:
            return None, self._first

        prev, nxt = self._scan(t)
        if prev is None:
            self._first = nxt
        else:
            bisect.insort(self._gops, (prev, nxt), key=lambda gop: gop[0])
        return prev, nxt

    def _scan(self, t: float) -> tuple[float | None, float | None]:
        # MP4 のシークは DTS 基準なので、B フレームがあると t より後のキーフレームに着くことがある。
        # その場合は少しずつ手前からやり直す
        back = 1.0
        while True:
            target = max(t - back, 0.0)
            prev, nxt, first = self._read_from(target, t)
            overshot = prev is None and first is not None and first > t
            if not overshot or target == 0.0:
                return prev, nxt
            back *= 4

    def _read_from(self, target: float, t: float) -> tuple[float | None, float | None, float | None]:
        tb = self._stream.time_base
        self._container.seek(
            math.floor((target + self._start) / tb), stream=self._stream, backward=True, any_frame=False
        )
        prev = first = None
        for packet in self._container.demux(self._stream):
            if packet.pts is None or not packet.is_keyframe:
                continue
            kt = float(packet.pts * tb) - self._start
            if first is None:
                first = kt
            if kt > t:
                return prev, kt, first
            prev = kt
        return prev, None, first
