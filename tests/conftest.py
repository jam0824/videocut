import os

import pytest

from media_factory import make_indexed_video

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def indexed_mp4(tmp_path_factory):
    """10秒・60fps・キーフレーム0.5秒間隔・音声1トラックの MP4"""
    return make_indexed_video(tmp_path_factory.mktemp("media") / "indexed.mp4")


@pytest.fixture(scope="session")
def offset_mkv(tmp_path_factory):
    """映像が1.5秒から始まる・音声2トラックの MKV"""
    return make_indexed_video(tmp_path_factory.mktemp("media") / "offset.mkv", audio_tracks=2, start_offset=1.5)


@pytest.fixture(scope="session")
def long_mp4(tmp_path_factory):
    """中止のテスト用: 書き出しに数秒かかる長さの MP4"""
    return make_indexed_video(tmp_path_factory.mktemp("media") / "long.mp4", seconds=120)


@pytest.fixture(scope="session")
def delayed_video_mp4(tmp_path_factory):
    """音声より映像が0.25秒遅れて始まる MP4（キーフレームは映像の開始から0.5秒ごと）"""
    return make_indexed_video(tmp_path_factory.mktemp("media") / "delayed.mp4", video_delay=0.25)
