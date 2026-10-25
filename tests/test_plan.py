from pathlib import Path

import pytest

from videocut.plan import (
    Job,
    Method,
    OutputSpec,
    build_outputs,
    default_output_path,
    estimate_output_bytes,
    reencode_audio_bitrate,
    reencode_video_bitrate,
    temp_path_for,
)
from videocut.ranges import Range


class TestBuildOutputs:
    def test_つなげる場合は全区間を1つのファイルに出す(self):
        ranges = [Range(0, 1), Range(2, 3)]
        assert build_outputs(ranges, separate=False, path="D:/out/clip.mp4") == [
            OutputSpec("D:/out/clip.mp4", (Range(0, 1), Range(2, 3)))
        ]

    def test_別ファイルの場合は区間ごとに連番を付ける(self):
        ranges = [Range(0, 1), Range(2, 3), Range(4, 5)]
        outputs = build_outputs(ranges, separate=True, path="D:/out/clip.mp4")
        assert [Path(o.path).name for o in outputs] == ["clip_01.mp4", "clip_02.mp4", "clip_03.mp4"]
        assert [o.ranges for o in outputs] == [(Range(0, 1),), (Range(2, 3),), (Range(4, 5),)]

    def test_100区間以上なら連番を3桁にする(self):
        ranges = [Range(i, i + 0.5) for i in range(100)]
        outputs = build_outputs(ranges, separate=True, path="clip.mkv")
        assert Path(outputs[0].path).name == "clip_001.mkv"
        assert Path(outputs[-1].path).name == "clip_100.mkv"


class TestDefaultOutputPath:
    def test_元ファイルと同じ場所に_cutを付けた名前を提案する(self, tmp_path):
        src = tmp_path / "録画 1.mp4"
        assert default_output_path(str(src)) == str(tmp_path / "録画 1_cut.mp4")

    def test_同名のファイルがあれば番号を付ける(self, tmp_path):
        src = tmp_path / "rec.mkv"
        (tmp_path / "rec_cut.mkv").write_bytes(b"")
        (tmp_path / "rec_cut_2.mkv").write_bytes(b"")
        assert default_output_path(str(src)) == str(tmp_path / "rec_cut_3.mkv")


class TestTempPath:
    def test_拡張子を保ったまま一時ファイル名にする(self):
        assert Path(temp_path_for("D:/out/clip.mp4")).name == "clip.videocut-tmp.mp4"


class TestBitrate:
    def test_再エンコードの映像は元のビットレートの1_2倍にする(self):
        assert reencode_video_bitrate(6_000_000) == 7_200_000

    def test_映像ビットレートが不明なら8Mbpsにする(self):
        assert reencode_video_bitrate(None) == 8_000_000

    def test_音声は元のビットレートを使う(self):
        assert reencode_audio_bitrate(160_000) == 160_000

    def test_音声ビットレートが不明なら192kbpsにする(self):
        assert reencode_audio_bitrate(None) == 192_000


class TestEstimateOutputBytes:
    def test_無劣化は残す長さの割合で見積もる(self):
        size = estimate_output_bytes(
            Method.COPY, source_size=1000, duration=100, keep_length=25, video_bitrate=None, audio_bitrates=[]
        )
        assert size == 250

    def test_再エンコードはビットレートから見積もる(self):
        size = estimate_output_bytes(
            Method.REENCODE,
            source_size=1,
            duration=100,
            keep_length=10,
            video_bitrate=6_000_000,
            audio_bitrates=[160_000],
        )
        assert size == pytest.approx((7_200_000 + 160_000) * 10 / 8 * 1.1)


class TestJob:
    def test_辞書に変換して元に戻せる(self):
        job = Job(
            source="D:/録画/2026-10-25 09-59-50.mp4",
            method=Method.REENCODE,
            outputs=[OutputSpec("D:/出力/a.mp4", (Range(1.5, 3.25), Range(10, 20)))],
        )
        assert Job.from_dict(job.to_dict()) == job
