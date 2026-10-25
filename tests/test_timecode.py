import pytest

from videocut.timecode import format_time, parse_time


class TestFormatTime:
    def test_時分秒とミリ秒で表示する(self):
        assert format_time(3723.456) == "01:02:03.456"

    def test_0秒は全桁0で表示する(self):
        assert format_time(0) == "00:00:00.000"

    def test_ミリ秒未満は四捨五入する(self):
        assert format_time(1.2346) == "00:00:01.235"

    def test_丸めで繰り上がった秒を分に反映する(self):
        assert format_time(59.9996) == "00:01:00.000"

    def test_10時間を超えても表示できる(self):
        assert format_time(36000) == "10:00:00.000"

    def test_負の値は0として表示する(self):
        assert format_time(-0.5) == "00:00:00.000"


class TestParseTime:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("01:02:03.456", 3723.456),
            ("1:02:03", 3723.0),
            ("1:2:3", 3723.0),
            ("02:03.5", 123.5),
            ("83.25", 83.25),
            ("  1:00  ", 60.0),
            ("0:00:00", 0.0),
        ],
    )
    def test_時分秒の書式を秒に変換する(self, text, expected):
        assert parse_time(text) == pytest.approx(expected)

    def test_全角の数字と記号も受け付ける(self):
        assert parse_time("１：２３．５") == pytest.approx(83.5)

    @pytest.mark.parametrize("text", ["", "abc", "1:60", "1:2:3:4", "-1", "1::2", "1:-1", "1:2:60"])
    def test_不正な書式はValueErrorにする(self, text):
        with pytest.raises(ValueError):
            parse_time(text)
