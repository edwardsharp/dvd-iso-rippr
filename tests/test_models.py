"""pure data-model tests; no media fixtures or media tools are needed."""

from pathlib import Path

import pytest

from dvd_ripper.models import DVD, EncodeSettings, Stream, Title

VIDEO = Stream(0, "video", "mpeg2video", width=720, height=480)
AUDIO = Stream(1, "audio", "ac3", language="eng", channels=6)


@pytest.mark.parametrize(
    ("titles", "expected_number"),
    [
        ((Title(1, 120, (VIDEO,)), Title(2, 7200, (VIDEO,))), 2),
        ((Title(3, 3600, (VIDEO,)), Title(1, 300, (VIDEO,))), 3),
        ((Title(1, 3600.1, (VIDEO,)), Title(2, 3600.9, (VIDEO,))), 2),
        ((Title(8, 3600, (VIDEO,)), Title(2, 3600, (VIDEO,))), 2),
        ((Title(2, 3600, (VIDEO,)), Title(8, 3600, (VIDEO,))), 2),
        ((Title(1, None, (VIDEO,)), Title(4, 100, (VIDEO,))), 4),
        ((Title(1, None, (VIDEO,)), Title(4, 0, (VIDEO,))), 4),
        ((Title(7, None, (VIDEO,)), Title(3, None, (VIDEO,))), 3),
        ((Title(3, None, (VIDEO,)), Title(7, None, (VIDEO,))), 3),
        ((Title(9, None, (VIDEO,)),), 9),
        ((Title(1, 9000, (AUDIO,)), Title(2, 600, (VIDEO,))), 2),
        ((Title(1, 9000), Title(2, None, (VIDEO,))), 2),
        ((), None),
        ((Title(1, 9000),), None),
        ((Title(1, 9000, (AUDIO,)), Title(2, None)), None),
    ],
    ids=[
        "longest-not-title-one",
        "unsorted-titles",
        "fractional-durations",
        "tie-lower-number-last",
        "tie-lower-number-first",
        "known-beats-unknown",
        "zero-beats-unknown",
        "all-unknown-lower-number-last",
        "all-unknown-lower-number-first",
        "single-unknown-video",
        "ignores-longer-audio-only",
        "unknown-video-beats-streamless-title",
        "empty-disc",
        "no-streams",
        "no-video",
    ],
)
def test_main_title_heuristic(titles, expected_number):
    dvd = DVD(Path("disc.iso"), titles)

    if expected_number is None:
        assert dvd.main_title is None
    else:
        expected = next(title for title in titles if title.number == expected_number)
        assert dvd.main_title is expected
    assert dvd.titles is titles


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        (None, "unknown"),
        (0, "00:00:00"),
        (0.99, "00:00:00"),
        (9, "00:00:09"),
        (59.999, "00:00:59"),
        (60, "00:01:00"),
        (60.999, "00:01:00"),
        (3599.999, "00:59:59"),
        (3600, "01:00:00"),
        (3661.9, "01:01:01"),
        (24 * 3600, "24:00:00"),
        (100 * 3600 + 2 * 60 + 3, "100:02:03"),
    ],
)
def test_duration_text(duration, expected):
    assert Title(1, duration).duration_text == expected


def test_title_stream_accessors_preserve_stream_order_and_identity():
    subtitle = Stream(8, "subtitle", "dvd_subtitle", language="eng")
    other_audio = Stream(4, "audio", "mp2", language="fra", channels=2)
    other_video = Stream(10, "video", "mpeg2video")
    other_subtitle = Stream(3, "subtitle", "dvd_subtitle", language="fra")
    data = Stream(12, "data")
    streams = (subtitle, AUDIO, VIDEO, data, other_audio, other_video, other_subtitle)
    title = Title(1, 3600, streams)

    assert title.video is VIDEO
    assert title.audio_streams == (AUDIO, other_audio)
    assert title.subtitle_streams == (subtitle, other_subtitle)
    assert title.streams is streams


def test_empty_title_stream_accessors():
    title = Title(1, None)

    assert title.video is None
    assert title.audio_streams == ()
    assert title.subtitle_streams == ()


def test_default_encode_settings():
    settings = EncodeSettings()

    assert settings.crf == 21
    assert settings.preset == "medium"
    assert settings.deinterlace == "auto"
    assert settings.stereo is True


@pytest.mark.parametrize("crf", [0, 1, 21, 50, 51])
def test_encode_settings_accepts_crf_including_boundaries(crf):
    assert EncodeSettings(crf=crf).crf == crf


@pytest.mark.parametrize("crf", [-1, 52, 21.0, 21.5, "21", None, True, False, float("nan")])
def test_encode_settings_rejects_invalid_crf(crf):
    with pytest.raises(ValueError, match="crf must be an integer between 0 and 51"):
        EncodeSettings(crf=crf)


@pytest.mark.parametrize(
    "preset",
    [
        "ultrafast",
        "superfast",
        "veryfast",
        "faster",
        "fast",
        "medium",
        "slow",
        "slower",
        "veryslow",
    ],
)
def test_encode_settings_accepts_supported_presets(preset):
    assert EncodeSettings(preset=preset).preset == preset


@pytest.mark.parametrize("preset", ["", "turbo", "Medium", " medium ", "placebo", None, 1])
def test_encode_settings_rejects_unsupported_presets(preset):
    with pytest.raises(ValueError, match="unknown x264 preset"):
        EncodeSettings(preset=preset)


@pytest.mark.parametrize("deinterlace", ["auto", "on", "off"])
def test_encode_settings_accepts_deinterlace_modes(deinterlace):
    assert EncodeSettings(deinterlace=deinterlace).deinterlace == deinterlace


@pytest.mark.parametrize("deinterlace", ["", "yes", "AUTO", " off ", None, True])
def test_encode_settings_rejects_invalid_deinterlace_modes(deinterlace):
    with pytest.raises(ValueError, match="deinterlace must be auto, on, or off"):
        EncodeSettings(deinterlace=deinterlace)


@pytest.mark.parametrize("stereo", [True, False])
def test_encode_settings_accepts_both_stereo_modes(stereo):
    assert EncodeSettings(stereo=stereo).stereo is stereo


@pytest.mark.parametrize("stereo", [0, 1, "true", "false", "", None, []])
def test_encode_settings_rejects_non_boolean_stereo(stereo):
    with pytest.raises(ValueError, match="stereo must be a boolean"):
        EncodeSettings(stereo=stereo)
