"""書き出しの計画: 出力ファイル名・サイズの見積もり・ワーカーへ渡すジョブ"""

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from videocut.ranges import Range


class Method(enum.Enum):
    COPY = "copy"  # 無劣化（キーフレーム単位）
    REENCODE = "reencode"  # 正確（再エンコード）


@dataclass(frozen=True)
class OutputSpec:
    path: str
    ranges: tuple[Range, ...]  # 元動画の時刻。この順につなげて1ファイルにする


@dataclass(frozen=True)
class Job:
    source: str
    method: Method
    outputs: list[OutputSpec]

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "method": self.method.value,
            "outputs": [{"path": o.path, "ranges": [[r.start, r.end] for r in o.ranges]} for o in self.outputs],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        return cls(
            source=data["source"],
            method=Method(data["method"]),
            outputs=[
                OutputSpec(o["path"], tuple(Range(start, end) for start, end in o["ranges"])) for o in data["outputs"]
            ],
        )


def build_outputs(ranges: Sequence[Range], separate: bool, path: str) -> list[OutputSpec]:
    if not separate:
        return [OutputSpec(path, tuple(ranges))]
    base = Path(path)
    digits = max(2, len(str(len(ranges))))
    return [
        OutputSpec(str(base.with_name(f"{base.stem}_{i:0{digits}d}{base.suffix}")), (r,))
        for i, r in enumerate(ranges, start=1)
    ]


def default_output_path(source: str, suffix: str = "_cut") -> str:
    src = Path(source)
    candidate = src.with_name(f"{src.stem}{suffix}{src.suffix}")
    number = 2
    while candidate.exists():
        candidate = src.with_name(f"{src.stem}{suffix}_{number}{src.suffix}")
        number += 1
    return str(candidate)


def temp_path_for(path: str) -> str:
    """書き出し中に使う一時ファイル。拡張子を残すので形式の判定に使える"""
    p = Path(path)
    return str(p.with_name(f"{p.stem}.videocut-tmp{p.suffix}"))


def reencode_video_bitrate(source_bitrate: int | None) -> int:
    """再エンコード時の映像ビットレート。再圧縮による劣化を抑えるため元より少し上げる"""
    if not source_bitrate or source_bitrate < 500_000:
        return 8_000_000
    return round(source_bitrate * 1.2)


def reencode_audio_bitrate(source_bitrate: int | None) -> int:
    if not source_bitrate or source_bitrate < 64_000:
        return 192_000
    return min(source_bitrate, 512_000)


def estimate_output_bytes(
    method: Method,
    source_size: int,
    duration: float,
    keep_length: float,
    video_bitrate: int | None,
    audio_bitrates: Sequence[int | None],
) -> float:
    if method is Method.COPY:
        return source_size * keep_length / duration
    bitrate = reencode_video_bitrate(video_bitrate) + sum(reencode_audio_bitrate(b) for b in audio_bitrates)
    return bitrate * keep_length / 8 * 1.1
