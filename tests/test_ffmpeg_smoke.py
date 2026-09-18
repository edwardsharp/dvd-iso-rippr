"""exercise real encoding with synthetic media, not dvd demuxing or a user's images."""

import json
import shutil
from pathlib import Path

import pytest

from dvd_ripper import encoder
from dvd_ripper.ffmpeg import build_encode_command, run_capture
from dvd_ripper.models import EncodeSettings, Stream, Title
from dvd_ripper.tool_setup import select_tools


@pytest.mark.parametrize("deinterlace,stereo", [("auto", True), ("on", False), ("off", True)])
async def test_ffmpeg_encoding_smoke(tmp_path, monkeypatch, deinterlace, stereo):
    selected_ffmpeg, selected_ffprobe = select_tools()
    ffmpeg, ffprobe = shutil.which(selected_ffmpeg), shutil.which(selected_ffprobe)
    if ffmpeg is None or ffprobe is None:
        pytest.skip("optional ffmpeg smoke test needs ffmpeg and ffprobe")
    encoders = await run_capture([ffmpeg, "-hide_banner", "-encoders"])
    filters = await run_capture([ffmpeg, "-hide_banner", "-filters"])
    if not all(name in encoders for name in ("libx264", "aac", "ac3", "mpeg2video")):
        pytest.skip("optional ffmpeg smoke test needs h.264/aac and mpeg-2/ac3 encoders")
    if not all(name in filters for name in ("bwdif", "color", "anullsrc")):
        pytest.skip("optional ffmpeg smoke test needs bwdif and synthetic input filters")
    source = tmp_path / "synthetic.mkv"
    await run_capture(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-n",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=720x480:r=30000/1001",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=5.1:sample_rate=48000",
            "-t",
            "0.3",
            "-c:v",
            "mpeg2video",
            "-c:a",
            "ac3",
            str(source),
        ]
    )
    title = Title(
        1,
        0.3,
        (
            Stream(0, "video", "mpeg2video", width=720, height=480),
            Stream(1, "audio", "ac3", language="eng", channels=6),
        ),
    )

    def synthetic_command(*args, **kwargs):
        command = build_encode_command(*args, **kwargs)
        # only replace the dvd-specific input options. all production encoding,
        # stream mapping, progress, staging and publication code stays in use.
        start = command.index("-f")
        end = command.index("-i")
        assert command[start:end] == ["-f", "dvdvideo", "-title", "1"]
        del command[start:end]
        return command

    monkeypatch.setattr(encoder, "build_encode_command", synthetic_command)
    output = tmp_path / "output with spaces" / "movie.mp4"
    updates = []
    await encoder.encode_title(
        source,
        title,
        output,
        audio_index=1,
        settings=EncodeSettings(deinterlace=deinterlace, stereo=stereo),
        ffmpeg=ffmpeg,
        on_progress=updates.append,
    )
    data = json.loads(
        await run_capture(
            [
                ffprobe,
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(output),
            ]
        )
    )
    video, audio = data["streams"]
    assert video["codec_name"] == "h264"
    assert video["pix_fmt"] == "yuv420p"
    assert (video["width"], video["height"]) == (720, 480)
    assert audio["codec_name"] == "aac"
    assert audio["profile"] == "LC"
    assert audio["channels"] == (2 if stereo else 6)
    assert updates and updates[-1].seconds > 0
    assert not list(output.parent.glob(".dvd-ripper-*"))
    assert source.is_file()
    # faststart places the movie index before the media data.
    content = Path(output).read_bytes()
    assert 0 < content.index(b"moov") < content.index(b"mdat")
