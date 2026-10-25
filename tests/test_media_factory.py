"""検査用ヘルパー自体が正しく読み取れることの確認"""

import pytest

from media_factory import beep_times, decode_frames, frame_indices


def test_生成した動画のフレーム番号を順に読み取れる(indexed_mp4):
    assert frame_indices(indexed_mp4) == list(range(600))


def test_フレーム時刻は60fps間隔になる(indexed_mp4):
    frames = decode_frames(indexed_mp4)
    assert frames[0][0] == pytest.approx(0, abs=1e-6)
    assert frames[300][0] == pytest.approx(5.0, abs=1e-6)


def test_ビープは毎秒ちょうどに鳴る(indexed_mp4):
    assert beep_times(indexed_mp4) == pytest.approx([float(s) for s in range(10)], abs=0.002)


def test_映像が遅れて始まる動画でもビープは映像の毎秒に合う(delayed_video_mp4):
    frames = decode_frames(delayed_video_mp4)
    assert frames[0][0] == pytest.approx(0.25, abs=1e-3)
    assert beep_times(delayed_video_mp4) == pytest.approx([0.25 + s for s in range(10)], abs=0.002)


def test_開始がずれたMKVでもビープは映像の毎秒に合う(offset_mkv):
    frames = decode_frames(offset_mkv)
    video_start = frames[0][0]
    for track in (0, 1):
        expected = [video_start + s for s in range(10)]
        assert beep_times(offset_mkv, track) == pytest.approx(expected, abs=0.002)
