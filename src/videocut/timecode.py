"""時刻（秒）と "HH:MM:SS.mmm" 形式の文字列を相互に変換する"""

import re
import unicodedata

_INT = re.compile(r"\d+")
_SECONDS = re.compile(r"\d+(?:\.\d+)?")


def format_time(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def parse_time(text: str) -> float:
    """"1:02:03.5" / "02:03.5" / "83.5" を秒に変換する。全角入力も受け付ける"""
    normalized = unicodedata.normalize("NFKC", text).strip()
    parts = normalized.split(":")
    *heads, last = parts
    if (
        not normalized
        or len(parts) > 3
        or not _SECONDS.fullmatch(last)
        or not all(_INT.fullmatch(p) for p in heads)
    ):
        raise ValueError(f"時刻の書式が正しくありません: {text!r}")

    seconds = float(last)
    units = [int(p) for p in heads]
    if heads and seconds >= 60:
        raise ValueError(f"秒は60未満で指定してください: {text!r}")
    if len(units) == 2 and units[1] >= 60:
        raise ValueError(f"分は60未満で指定してください: {text!r}")

    for power, value in enumerate(reversed(units), start=1):
        seconds += value * 60**power
    return seconds
