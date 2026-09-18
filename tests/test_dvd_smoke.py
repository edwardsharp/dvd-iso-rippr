"""opt-in end-to-end dvd scan and short conversion; never modify the input image."""

import asyncio
import json
from pathlib import Path

import pytest

from dvd_ripper import encoder
from dvd_ripper.ffmpeg import (
    build_encode_command,
    choose_english_audio,
    dependency_issues,
    run_capture,
)
from dvd_ripper.scanner import scan_dvd
from dvd_ripper.tool_setup import select_tools


async def test_dvd_scan_and_sample(pytestconfig, tmp_path, monkeypatch):
    supplied = pytestconfig.getoption("--dvd-iso")
    if not supplied:
        pytest.skip("supply --dvd-iso path to opt into the real dvd test")
    source = Path(supplied).expanduser().resolve(strict=True)
    before = source.stat()
    assert source.is_file(), "the dvd image must be a regular file"
    ffmpeg, ffprobe = select_tools()
    issues = await dependency_issues(ffmpeg=ffmpeg, ffprobe=ffprobe)
    assert not issues, "\n".join(issues) + "\nrun python -m dvd_ripper --setup-ffmpeg"
    dvd = await asyncio.wait_for(scan_dvd(source, ffprobe=ffprobe, on_progress=print), timeout=600)
    title = dvd.main_title
    assert title is not None
    print(f"titles: {[(item.number, item.duration_text) for item in dvd.titles]}")
    print(f"sample title: {title.number}; streams: {title.streams}")
    # this test explicitly falls back to the first track to exercise audio on non-english discs.
    audio = choose_english_audio(title) or next(iter(title.audio_streams), None)

    def sample_command(*args, **kwargs):
        command = build_encode_command(*args, **kwargs)
        # limit output duration without replacing the dvd input or any encoding settings.
        command[-1:-1] = ["-t", "5"]
        return command

    monkeypatch.setattr(encoder, "build_encode_command", sample_command)
    output = tmp_path / "dvd-sample.mp4"
    await asyncio.wait_for(
        encoder.encode_title(
            source, title, output, audio_index=audio.index if audio else None, ffmpeg=ffmpeg
        ),
        timeout=300,
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
    video = next(stream for stream in data["streams"] if stream["codec_type"] == "video")
    assert video["codec_name"] == "h264"
    assert video["pix_fmt"] == "yuv420p"
    if audio is not None:
        encoded_audio = next(
            stream for stream in data["streams"] if stream["codec_type"] == "audio"
        )
        assert encoded_audio["codec_name"] == "aac"
        assert encoded_audio["channels"] == 2
    assert 0 < float(data["format"]["duration"]) <= 6
    assert not any(stream["codec_type"] == "subtitle" for stream in data["streams"])
    after = source.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    print(f"sample passed: {output}")
