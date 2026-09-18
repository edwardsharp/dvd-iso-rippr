"""command contracts and real, media-free subprocess lifecycle tests."""

import asyncio
import sys
from contextlib import suppress
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from dvd_ripper import ffmpeg
from dvd_ripper.ffmpeg import FFmpegError, build_encode_command, choose_english_audio, run_capture
from dvd_ripper.models import EncodeSettings, Stream, Title


@pytest.fixture
def title():
    # deliberately non-contiguous indices: mapping must not use audio/video ordinals.
    return Title(
        7,
        120,
        (
            Stream(1, "subtitle", "dvd_subtitle", "eng"),
            Stream(4, "video", "mpeg2video"),
            Stream(2, "audio", "ac3", "eng", channels=2),
            Stream(9, "audio", "ac3", "eng", channels=6),
            Stream(12, "audio", "dts", "fra", channels=6),
            Stream(15, "video", "mpeg2video"),
        ),
    )


def option_values(command, option):
    return [command[index + 1] for index, value in enumerate(command) if value == option]


def test_default_command_exact_mapping_codecs_and_literal_arguments(tmp_path, title):
    source = tmp_path / "Movie's & extras; literal.iso"
    output = tmp_path / "Title 7 & extras.mp4"
    executable = str(tmp_path / "tools with spaces" / "ffmpeg")

    command = build_encode_command(source, title, output, audio_index=9, ffmpeg=executable)

    assert command == [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-n",
        "-f",
        "dvdvideo",
        "-title",
        "7",
        "-i",
        str(source.resolve()),
        "-map",
        "0:4",
        "-vf",
        "bwdif=mode=send_frame:parity=auto:deint=interlaced",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "21",
        "-profile:v",
        "high",
        "-level:v",
        "4.0",
        "-pix_fmt",
        "yuv420p",
        "-maxrate",
        "10M",
        "-bufsize",
        "20M",
        "-map",
        "0:9",
        "-c:a",
        "aac",
        "-profile:a",
        "aac_low",
        "-ac",
        "2",
        "-b:a",
        "192k",
        "-sn",
        "-dn",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        "-f",
        "mp4",
        str(output.resolve()),
    ]
    assert isinstance(command, list)
    assert all(isinstance(argument, str) for argument in command)
    assert "-y" not in command
    assert not source.exists()
    assert not output.exists()


@pytest.mark.parametrize("audio_index", [2, 9, 12])
def test_explicit_audio_index_is_not_replaced_by_english_default(tmp_path, title, audio_index):
    command = build_encode_command(
        tmp_path / "disc.iso", title, tmp_path / "out.mp4", audio_index=audio_index
    )
    assert option_values(command, "-map") == ["0:4", f"0:{audio_index}"]
    assert "-an" not in command


@pytest.mark.parametrize("language", ["eng", "en", "english", " ENG ", "En", " English\t"])
def test_english_language_aliases_are_normalized(language):
    english = Stream(3, "audio", "aac", language, channels=2)
    title = Title(1, 1, (Stream(0, "audio", "ac3", "fra", channels=6), english))
    assert choose_english_audio(title) is english


@pytest.mark.parametrize(
    ("streams", "expected_index"),
    [
        pytest.param(
            (
                Stream(1, "audio", "ac3", "eng", channels=2),
                Stream(8, "audio", "dts", "eng", channels=6),
            ),
            8,
            id="channels-before-codec",
        ),
        pytest.param(
            (
                Stream(1, "audio", "dts", "eng", channels=6),
                Stream(8, "audio", "ac3", "eng", channels=6),
            ),
            8,
            id="prefer-ac3-at-equal-channels",
        ),
        pytest.param(
            (
                Stream(8, "audio", "ac3", "eng", channels=6),
                Stream(1, "audio", "ac3", "eng", channels=6),
            ),
            1,
            id="lowest-index-breaks-tie",
        ),
        pytest.param(
            (Stream(1, "audio", "ac3", "eng"), Stream(8, "audio", "aac", "eng", channels=1)),
            8,
            id="unknown-channel-count-ranks-last",
        ),
    ],
)
def test_english_audio_ranking_is_independent_of_stream_order(streams, expected_index):
    for ordering in (streams, tuple(reversed(streams))):
        selected = choose_english_audio(Title(1, 1, ordering))
        assert selected is not None
        assert selected.index == expected_index


@pytest.mark.parametrize(
    "streams",
    [
        (),
        (Stream(0, "audio", "ac3", "fra", channels=6),),
        (Stream(0, "audio", "ac3", None), Stream(1, "audio", "aac", "und")),
        (Stream(0, "video", language="eng"), Stream(1, "subtitle", language="en")),
        (Stream(0, "audio", language=""), Stream(1, "audio", language="eng-US")),
    ],
)
def test_missing_english_returns_none_without_falling_back(streams):
    assert choose_english_audio(Title(1, 1, streams)) is None


@pytest.mark.parametrize("with_audio", [True, False])
def test_explicit_silent_maps_only_video(tmp_path, title, with_audio):
    if not with_audio:
        title = replace(title, streams=(title.video,))
    command = build_encode_command(
        tmp_path / "disc.iso", title, tmp_path / "out.mp4", audio_index=None
    )
    assert option_values(command, "-map") == ["0:4"]
    assert "-an" in command
    for option in ("-c:a", "-profile:a", "-ac", "-b:a"):
        assert option not in command


@pytest.mark.parametrize("stereo", [True, False])
@pytest.mark.parametrize("channels", [None, 1, 2, 6, 8])
def test_stereo_downmix_and_surround_bitrate(tmp_path, stereo, channels):
    title = Title(1, 1, (Stream(3, "video"), Stream(0, "audio", channels=channels)))
    command = build_encode_command(
        tmp_path / "disc.iso",
        title,
        tmp_path / "out.mp4",
        audio_index=0,
        settings=EncodeSettings(stereo=stereo),
    )
    assert option_values(command, "-map") == ["0:3", "0:0"]
    assert option_values(command, "-c:a") == ["aac"]
    assert option_values(command, "-profile:a") == ["aac_low"]
    assert option_values(command, "-ac") == (["2"] if stereo else [])
    expected = "384k" if not stereo and (channels or 0) > 2 else "192k"
    assert option_values(command, "-b:a") == [expected]


@pytest.mark.parametrize(
    ("mode", "filters"),
    [
        ("auto", ["bwdif=mode=send_frame:parity=auto:deint=interlaced"]),
        ("on", ["bwdif=mode=send_frame:parity=auto:deint=all"]),
        ("off", []),
    ],
)
def test_deinterlace_modes(tmp_path, title, mode, filters):
    command = build_encode_command(
        tmp_path / "disc.iso",
        title,
        tmp_path / "out.mp4",
        audio_index=None,
        settings=EncodeSettings(deinterlace=mode),
    )
    assert option_values(command, "-vf") == filters


@pytest.mark.parametrize(("crf", "preset"), [(0, "ultrafast"), (25, "slow"), (51, "veryslow")])
def test_custom_quality_settings_and_uppercase_mp4(tmp_path, title, crf, preset):
    command = build_encode_command(
        tmp_path / "disc.iso",
        title,
        tmp_path / "out.MP4",
        audio_index=2,
        settings=EncodeSettings(crf=crf, preset=preset),
    )
    assert option_values(command, "-crf") == [str(crf)]
    assert option_values(command, "-preset") == [preset]
    assert option_values(command, "-c:v") == ["libx264"]


@pytest.mark.parametrize("filename", ["out", "out.mkv", "out.mp4.tmp", "out.iso"])
def test_invalid_output_extension_is_rejected(tmp_path, title, filename):
    with pytest.raises(FFmpegError, match=r"\.mp4 extension"):
        build_encode_command(tmp_path / "disc.iso", title, tmp_path / filename, audio_index=None)


def test_destination_must_not_resolve_to_source(tmp_path, title):
    source = tmp_path / "disc.mp4"
    alias = tmp_path / "unused" / ".." / source.name
    with pytest.raises(FFmpegError, match="must not replace the source"):
        build_encode_command(source, title, alias, audio_index=None)


@pytest.mark.parametrize("number", [0, -1, 100])
def test_invalid_title_number_is_rejected(tmp_path, title, number):
    with pytest.raises(FFmpegError, match="between 1 and 99"):
        build_encode_command(
            tmp_path / "disc.iso",
            replace(title, number=number),
            tmp_path / "out.mp4",
            audio_index=None,
        )


def test_missing_video_is_rejected(tmp_path):
    title = Title(3, 1, (Stream(0, "audio", "ac3", "eng"),))
    with pytest.raises(FFmpegError, match="title 3 has no video stream"):
        build_encode_command(tmp_path / "disc.iso", title, tmp_path / "out.mp4", audio_index=0)


@pytest.mark.parametrize("audio_index", [-1, 1, 4, 99, "2"])
def test_invalid_or_non_audio_stream_index_is_rejected(tmp_path, title, audio_index):
    with pytest.raises(FFmpegError, match=f"no audio stream with index {audio_index}"):
        build_encode_command(
            tmp_path / "disc.iso", title, tmp_path / "out.mp4", audio_index=audio_index
        )


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        *[({"crf": value}, "crf") for value in (-1, 52, 21.0, True, "21", None)],
        *[({"preset": value}, "preset") for value in ("", "placebo", "MEDIUM", "slow -y")],
        *[({"deinterlace": value}, "deinterlace") for value in ("", "yes", "AUTO", None)],
        *[({"stereo": value}, "stereo") for value in (0, 1, "false", None)],
    ],
)
def test_invalid_encode_settings_are_rejected(settings, message):
    with pytest.raises(ValueError, match=message):
        EncodeSettings(**settings)


@pytest.fixture
async def captured_processes(monkeypatch):
    """observe real children, with a fallback reap if a lifecycle assertion fails."""
    processes = []
    started = asyncio.Event()
    create_subprocess = asyncio.create_subprocess_exec

    async def start(*args, **kwargs):
        process = await create_subprocess(*args, **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(ffmpeg.asyncio, "create_subprocess_exec", start)
    yield processes, started
    for process in processes:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await asyncio.wait_for(process.wait(), 10)


@pytest.mark.asyncio
async def test_run_capture_success_uses_literal_argv_closed_stdin_and_drains_stderr():
    literal = "spaces & semicolons; 'quotes' = literal"
    script = (
        "import sys; "
        "assert sys.stdin.buffer.read() == b''; "
        "sys.stderr.buffer.write(b'warning' * 40000); sys.stderr.flush(); "
        "sys.stdout.buffer.write(sys.argv[1].encode('utf-8') + b'\\n\\xff')"
    )
    result = await asyncio.wait_for(run_capture([sys.executable, "-c", script, literal]), 10)
    assert result == literal + "\n\ufffd"


@pytest.mark.asyncio
@pytest.mark.parametrize("stderr", [b"bad input\n", b"", b"bad byte: \xff\n"])
async def test_run_capture_nonzero_reports_exit_command_and_stderr(stderr):
    script = f"import sys; sys.stdout.write('not success'); sys.stderr.buffer.write({stderr!r}); sys.exit(7)"
    with pytest.raises(FFmpegError) as error:
        await asyncio.wait_for(run_capture([sys.executable, "-c", script]), 10)
    message = str(error.value)
    assert "exit 7" in message
    assert sys.executable in message
    assert message.endswith("\n" + stderr.decode(errors="replace").strip())


@pytest.mark.asyncio
async def test_run_capture_missing_executable_has_actionable_error(tmp_path):
    missing = str(tmp_path / "missing executable")
    with pytest.raises(FFmpegError) as error:
        await run_capture([missing])
    assert "could not run" in str(error.value)
    assert "missing executable" in str(error.value)
    assert "installation and PATH" in str(error.value)
    assert isinstance(error.value.__cause__, OSError)


@pytest.mark.asyncio
async def test_run_capture_timeout_terminates_and_reaps_real_child(captured_processes):
    processes, _ = captured_processes
    with pytest.raises(FFmpegError, match=r"timed out after 0\.1s") as error:
        await asyncio.wait_for(
            run_capture([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.1), 10
        )
    assert isinstance(error.value.__cause__, TimeoutError)
    assert len(processes) == 1
    assert processes[0].returncode is not None


@pytest.mark.asyncio
async def test_run_capture_cancellation_propagates_after_reaping(captured_processes, monkeypatch):
    processes, started = captured_processes
    stop = AsyncMock(wraps=ffmpeg.stop_process)
    monkeypatch.setattr(ffmpeg, "stop_process", stop)
    task = asyncio.create_task(
        run_capture([sys.executable, "-c", "import time; time.sleep(60)"], timeout=60)
    )
    try:
        await asyncio.wait_for(started.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        assert len(processes) == 1
        stop.assert_awaited_once_with(processes[0])
        assert processes[0].returncode is not None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)
