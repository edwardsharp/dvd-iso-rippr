"""progress parsing, safe publication, and media-free encoding subprocess tests."""

import asyncio
import errno
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from dvd_ripper import encoder
from dvd_ripper.encoder import (
    Progress,
    _run_encode,
    available_output_path,
    encode_title,
    output_path,
    parse_progress,
)
from dvd_ripper.ffmpeg import FFmpegError, build_encode_command
from dvd_ripper.models import EncodeSettings, Stream, Title


@pytest.mark.parametrize("key", ["out_time_us", "out_time_ms"])
@pytest.mark.parametrize(("raw", "seconds"), [("0", 0), ("1", 0.000001), ("1500000", 1.5)])
def test_progress_time_fields_are_microseconds_including_historical_ms(key, raw, seconds):
    assert parse_progress({key: raw}).seconds == pytest.approx(seconds)


def test_progress_prefers_microseconds_over_historical_ms_and_timestamp():
    progress = parse_progress(
        {
            "out_time_us": "2500000",
            "out_time_ms": "9000000",
            "out_time": "01:00:00",
            "speed": "1.25x",
            "total_size": "4096",
        }
    )
    assert progress == Progress(seconds=2.5, speed="1.25x", total_size=4096)


@pytest.mark.parametrize(
    ("timestamp", "seconds"),
    [("00:00:00.000000", 0), ("01:02:03.125000", 3723.125), ("123:00:00", 442800)],
)
def test_progress_falls_back_to_timestamp(timestamp, seconds):
    assert parse_progress({"out_time": timestamp}).seconds == pytest.approx(seconds)


@pytest.mark.parametrize("key", ["out_time_us", "out_time_ms"])
@pytest.mark.parametrize("raw", ["", "garbage", "N/A", "-1", "-1500000", "nan", "inf", "-inf"])
def test_invalid_microsecond_progress_is_zero(key, raw):
    assert parse_progress({key: raw}).seconds == 0


@pytest.mark.parametrize(
    "timestamp",
    ["", "N/A", "not a time", "00:01", "0:0:0:0", "00:00:bad", "nan:0:0", "0:0:inf", "-01:00:00"],
)
def test_malformed_nonfinite_or_negative_timestamp_is_zero(timestamp):
    assert parse_progress({"out_time": timestamp}).seconds == 0


def test_negative_timestamp_with_zero_hours_is_not_positive_progress():
    assert parse_progress({"out_time": "-00:00:01.500000"}).seconds == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0", 0), ("12345", 12345), ("-10", 0), ("N/A", None), ("bad", None), ("1.5", None)],
)
def test_progress_total_size_is_optional_and_never_negative(raw, expected):
    assert parse_progress({"total_size": raw}).total_size == expected


def test_empty_and_na_progress_preserve_safe_defaults():
    assert parse_progress({}) == Progress()
    assert parse_progress({"out_time_us": "N/A", "speed": "N/A", "total_size": "N/A"}) == Progress()


@pytest.mark.parametrize("name", ["Movie.iso", "Movie.v2.IMG", "Film's & extras.iso"])
@pytest.mark.parametrize("custom_output", [False, True])
@pytest.mark.parametrize("unique", [False, True])
def test_output_naming_uses_disc_stem_and_title_number(tmp_path, name, custom_output, unique):
    source = tmp_path / "images" / name
    title = Title(12, None)
    root = tmp_path / "exports" if custom_output else source.parent
    actual = output_path(source, title, root if custom_output else None, unique=unique)
    assert actual == root.resolve() / source.stem / f"{source.stem} - Title 12.mp4"
    assert not actual.parent.exists()


def test_output_naming_resolves_relative_paths_and_expands_home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    title = Title(1, None)
    assert output_path(Path("images") / ".." / "Movie.iso", title) == (
        tmp_path.resolve() / "Movie" / "Movie - Title 1.mp4"
    )
    assert output_path(Path("~/images/Movie.iso"), title, Path("~/exports")) == (
        tmp_path.resolve() / "exports" / "Movie" / "Movie - Title 1.mp4"
    )


@pytest.mark.parametrize("custom_output", [False, True])
def test_output_preview_skips_collisions_but_base_stays_stable(tmp_path, custom_output):
    source = tmp_path / "Movie.iso"
    title = Title(7, None)
    root = tmp_path / "exports" if custom_output else None
    base = output_path(source, title, root, unique=False)
    base.parent.mkdir(parents=True)
    base.write_bytes(b"existing output")
    first = base.with_stem(f"{base.stem} (1)")
    assert output_path(source, title, root) == first
    assert available_output_path(base) == first
    assert not first.exists()
    first.write_bytes(b"first output")
    assert output_path(source, title, root) == base.with_stem(f"{base.stem} (2)")
    assert output_path(source, title, root, unique=False) == base
    assert base.read_bytes() == b"existing output"
    assert first.read_bytes() == b"first output"
    assert set(base.parent.iterdir()) == {base, first}


@pytest.mark.parametrize("name", ["movie.mp4", "movie.v2.mp4", "movie", "movie (1).mp4"])
def test_available_output_path_preserves_literal_stem(tmp_path, monkeypatch, name):
    monkeypatch.chdir(tmp_path)
    base = Path(name)
    assert available_output_path(base) == base
    assert not base.exists()
    base.write_bytes(b"existing output")
    candidate = base.with_stem(f"{base.stem} (1)")
    assert available_output_path(base) == candidate
    assert base.read_bytes() == b"existing output"
    assert not candidate.exists()


def test_available_output_path_skips_files_directories_and_symlinks(tmp_path):
    base = tmp_path / "movie.mp4"
    first, second, third, fourth, fifth = [
        base.with_stem(f"{base.stem} ({number})") for number in range(1, 6)
    ]
    base.write_bytes(b"existing output")
    first.mkdir()
    (first / "keep.txt").write_text("keep")
    symlink_or_skip(second, base)
    missing = tmp_path / "missing.mp4"
    symlink_or_skip(third, missing)
    fifth.write_bytes(b"later output")

    assert available_output_path(base) == fourth
    assert not fourth.exists()
    assert base.read_bytes() == b"existing output"
    assert (first / "keep.txt").read_text() == "keep"
    assert second.readlink() == base
    assert third.readlink() == missing
    assert not missing.exists()
    assert fifth.read_bytes() == b"later output"


@pytest.fixture
def encode_case(tmp_path):
    source = tmp_path / "Movie.iso"
    source.write_bytes(b"original source image")
    title = Title(
        7, 120, (Stream(4, "video", "mpeg2video"), Stream(9, "audio", "ac3", "eng", channels=6))
    )
    output = tmp_path / "exports" / "Movie - Title 7.mp4"
    return source, title, output


@pytest.fixture
def run_encode(monkeypatch):
    runner = AsyncMock()
    monkeypatch.setattr(encoder, "_run_encode", runner)
    return runner


def symlink_or_skip(link, target):
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"file symlinks are not available: {exc}")


@pytest.mark.asyncio
async def test_encode_stages_then_publishes_and_preserves_source(encode_case, run_encode):
    source, title, output = encode_case
    settings = EncodeSettings(crf=23, preset="slow", stereo=False, deinterlace="off")
    callback = Mock()
    staged_paths = []

    async def write_encoded_file(command, on_progress):
        partial = Path(command[-1])
        staged_paths.append(partial)
        assert partial.parent.parent == output.parent
        assert partial.parent.name.startswith(".dvd-ripper-")
        assert partial.name == output.name
        assert partial.parent.is_dir()
        assert not partial.exists()
        assert not output.exists()
        assert command == build_encode_command(
            source, title, partial, audio_index=9, settings=settings, ffmpeg="custom ffmpeg"
        )
        assert "-n" in command
        assert "-y" not in command
        assert on_progress is callback
        partial.write_bytes(b"completed MP4")
        on_progress(Progress(seconds=120, speed="2x", total_size=13))
        assert not output.exists()

    run_encode.side_effect = write_encoded_file
    published = await encode_title(
        source,
        title,
        output,
        audio_index=9,
        settings=settings,
        ffmpeg="custom ffmpeg",
        on_progress=callback,
    )

    assert published == output
    run_encode.assert_awaited_once()
    callback.assert_called_once_with(Progress(seconds=120, speed="2x", total_size=13))
    assert output.read_bytes() == b"completed MP4"
    assert source.read_bytes() == b"original source image"
    assert list(output.parent.iterdir()) == [output]
    assert not staged_paths[0].parent.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", ["file", "directory", "symlink", "dangling-symlink"])
@pytest.mark.parametrize("numbered", [False, True])
async def test_existing_output_is_never_overwritten(encode_case, run_encode, existing, numbered):
    source, title, output = encode_case
    if numbered:
        output = output.with_stem(f"{output.stem} (1)")
    output.parent.mkdir()
    target = source.parent / "target.mp4"
    if existing == "file":
        output.write_bytes(b"existing output")
    elif existing == "directory":
        output.mkdir()
        (output / "keep.txt").write_text("keep")
    else:
        if existing == "symlink":
            target.write_bytes(b"existing target")
        symlink_or_skip(output, target)

    async def complete(command, on_progress):
        assert "-n" in command
        assert "-y" not in command
        Path(command[-1]).write_bytes(b"completed mp4")

    run_encode.side_effect = complete
    published = await encode_title(source, title, output, audio_index=9)

    run_encode.assert_awaited_once()
    assert published == output.with_stem(f"{output.stem} (1)")
    assert published.read_bytes() == b"completed mp4"
    assert set(output.parent.iterdir()) == {output, published}
    assert source.read_bytes() == b"original source image"
    if existing == "file":
        assert output.read_bytes() == b"existing output"
    elif existing == "directory":
        assert (output / "keep.txt").read_text() == "keep"
    else:
        assert output.is_symlink()
        assert output.readlink() == target
        if existing == "symlink":
            assert target.read_bytes() == b"existing target"
        else:
            assert not target.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("target_kind", ["mkv", "iso", "dangling-extensionless", "source"])
async def test_existing_output_symlink_target_does_not_block_encoding(
    encode_case, run_encode, target_kind
):
    source, title, output = encode_case
    root = output.parent
    output = output_path(source, title, root, unique=False)
    output.parent.mkdir(parents=True)
    if target_kind == "source":
        target = source
    elif target_kind == "dangling-extensionless":
        target = source.parent / "missing"
    else:
        target = source.parent / f"target.{target_kind}"
        target.write_bytes(b"existing target")
    symlink_or_skip(output, target)
    link_inode = output.lstat().st_ino
    target_contents = target.read_bytes() if target.exists() else None
    preview = output_path(source, title, root)
    assert preview == output.with_stem(f"{output.stem} (1)")

    async def complete(command, on_progress):
        Path(command[-1]).write_bytes(b"completed mp4")

    run_encode.side_effect = complete
    published = await encode_title(source, title, output, audio_index=9)

    run_encode.assert_awaited_once()
    assert published == preview
    assert published.read_bytes() == b"completed mp4"
    assert output.is_symlink()
    assert output.readlink() == target
    assert output.lstat().st_ino == link_inode
    if target_contents is None:
        assert not encoder.os.path.lexists(target)
    else:
        assert target.read_bytes() == target_contents
    assert source.read_bytes() == b"original source image"
    assert set(output.parent.iterdir()) == {output, published}


@pytest.mark.asyncio
async def test_repeated_encodes_increment_from_stable_base(encode_case, run_encode):
    source, title, _ = encode_case
    base = output_path(source, title, unique=False)

    async def complete(command, on_progress):
        Path(command[-1]).write_bytes(f"encode {run_encode.await_count}".encode())

    run_encode.side_effect = complete
    published = []
    for number in range(3):
        expected = base if number == 0 else base.with_stem(f"{base.stem} ({number})")
        assert output_path(source, title) == expected
        assert output_path(source, title, unique=False) == base
        published.append(await encode_title(source, title, base, audio_index=9))
        assert published[-1] == expected

    assert run_encode.await_count == 3
    assert [path.read_bytes() for path in published] == [b"encode 1", b"encode 2", b"encode 3"]
    assert set(base.parent.iterdir()) == set(published)
    assert source.read_bytes() == b"original source image"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "alias",
    [
        "same-path",
        "mp4-source",
        "equivalent-parent",
        "parent-symlink",
        "source-symlink",
        "source-symlink-parent",
        "resolved-source",
    ],
)
async def test_source_output_is_refused_before_suffixing(
    encode_case, run_encode, monkeypatch, alias
):
    source, title, _ = encode_case
    root = source.parent
    if alias == "mp4-source":
        source = source.with_suffix(".mp4")
        source.write_bytes(b"original source image")
    elif alias in {"source-symlink", "source-symlink-parent", "resolved-source"}:
        link = source.with_name("source-link.mp4")
        symlink_or_skip(link, source)
        source = link
    output = source
    if alias == "equivalent-parent":
        child = root / "child"
        child.mkdir()
        output = child / ".." / source.name
    elif alias in {"parent-symlink", "source-symlink-parent"}:
        parent_alias = root / "parent-link"
        symlink_or_skip(parent_alias, root)
        output = parent_alias / source.name
    elif alias == "resolved-source":
        output = source.resolve()
    before = set(root.rglob("*"))
    candidates = Mock(side_effect=AssertionError("must validate before suffixing"))
    monkeypatch.setattr(encoder, "_output_candidates", candidates)

    with pytest.raises(FFmpegError, match="the output must not replace the source image"):
        await encode_title(source, title, output, audio_index=9)

    candidates.assert_not_called()
    run_encode.assert_not_awaited()
    assert source.read_bytes() == b"original source image"
    assert set(root.rglob("*")) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invalid", "message"),
    [
        ("extension", r"the output must have an \.mp4 extension"),
        ("title-number", "dvd title numbers must be between 1 and 99"),
        ("video", "title 7 has no video stream"),
        ("audio-index", "title 7 has no audio stream with index 4"),
        ("source", "the output must not replace the source image"),
    ],
)
@pytest.mark.parametrize("occupied", [False, True])
async def test_invalid_encode_request_has_no_filesystem_or_process_side_effects(
    encode_case, run_encode, invalid, message, occupied
):
    source, title, output = encode_case
    original_output = output
    audio_index = 9
    if invalid == "extension":
        output = output.with_suffix(".mkv")
    elif invalid == "title-number":
        title = replace(title, number=100)
    elif invalid == "video":
        title = replace(title, streams=title.audio_streams)
    elif invalid == "audio-index":
        audio_index = 4
    else:
        output = source

    if occupied and invalid != "source":
        output.parent.mkdir()
        symlink_or_skip(output, source)
    before = set(source.parent.rglob("*"))

    with pytest.raises(FFmpegError, match=message):
        await encode_title(source, title, output, audio_index=audio_index)

    run_encode.assert_not_awaited()
    assert set(source.parent.rglob("*")) == before
    if not occupied or invalid == "source":
        assert not original_output.parent.exists()
    else:
        assert output.readlink() == source
    assert source.read_bytes() == b"original source image"


@pytest.mark.asyncio
@pytest.mark.parametrize("error_kind", ["encode", "write"])
async def test_failed_encode_removes_partial_and_staging(encode_case, run_encode, error_kind):
    source, title, output = encode_case
    failure = (
        FFmpegError("bad frame") if error_kind == "encode" else OSError(errno.ENOSPC, "disk full")
    )

    async def fail(command, on_progress):
        Path(command[-1]).write_bytes(b"incomplete MP4")
        raise failure

    run_encode.side_effect = fail
    with pytest.raises(FFmpegError) as error:
        await encode_title(source, title, output, audio_index=9)
    if error_kind == "encode":
        assert error.value is failure
    else:
        assert "cannot write output" in str(error.value)
        assert error.value.__cause__ is failure
    assert not output.exists()
    assert list(output.parent.iterdir()) == []
    assert source.read_bytes() == b"original source image"


@pytest.mark.asyncio
@pytest.mark.parametrize("occupied", ["none", "existing", "concurrent"])
async def test_cancelling_encode_removes_partial_and_staging(encode_case, run_encode, occupied):
    source, title, output = encode_case
    started = asyncio.Event()
    if occupied == "existing":
        output.parent.mkdir()
        output.write_bytes(b"another writer's mp4")

    async def pending_encode(command, on_progress):
        Path(command[-1]).write_bytes(b"incomplete MP4")
        if occupied == "concurrent":
            output.write_bytes(b"another writer's mp4")
        started.set()
        await asyncio.Event().wait()

    run_encode.side_effect = pending_encode
    task = asyncio.create_task(encode_title(source, title, output, audio_index=9))
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        if occupied == "none":
            assert not output.exists()
            assert list(output.parent.iterdir()) == []
        else:
            assert output.read_bytes() == b"another writer's mp4"
            assert list(output.parent.iterdir()) == [output]
        assert source.read_bytes() == b"original source image"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("produced", ["missing", "empty", "directory"])
async def test_success_without_nonempty_file_is_rejected_and_cleaned(
    encode_case, run_encode, produced
):
    source, title, output = encode_case

    async def no_usable_output(command, on_progress):
        partial = Path(command[-1])
        if produced == "empty":
            partial.touch()
        elif produced == "directory":
            partial.mkdir()

    run_encode.side_effect = no_usable_output
    with pytest.raises(FFmpegError, match="produced no nonempty mp4"):
        await encode_title(source, title, output, audio_index=9)
    assert not output.exists()
    assert list(output.parent.iterdir()) == []
    assert source.read_bytes() == b"original source image"


@pytest.mark.asyncio
@pytest.mark.parametrize("error_number", [errno.EXDEV, errno.EOPNOTSUPP, errno.EPERM])
@pytest.mark.parametrize("occupied", [False, True])
async def test_hardlink_failure_retains_completed_file_and_reports_recovery(
    encode_case, run_encode, monkeypatch, error_number, occupied
):
    source, title, output = encode_case
    failure = OSError(error_number, "hard links unavailable")
    candidate = output
    if occupied:
        output.parent.mkdir()
        output.write_bytes(b"existing output")
        candidate = output.with_stem(f"{output.stem} (1)")
    link = Mock(
        side_effect=[FileExistsError(errno.EEXIST, "file exists"), failure] if occupied else failure
    )
    monkeypatch.setattr(encoder.os, "link", link)

    async def complete(command, on_progress):
        Path(command[-1]).write_bytes(b"completed MP4")

    run_encode.side_effect = complete
    with pytest.raises(FFmpegError) as error:
        await encode_title(source, title, output, audio_index=9)

    run_encode.assert_awaited_once()
    partial = Path(run_encode.call_args.args[0][-1])
    attempts = [output, candidate] if occupied else [output]
    assert [entry.args for entry in link.call_args_list] == [(partial, path) for path in attempts]
    assert error.value.__cause__ is failure
    assert f"encoded successfully but could not publish {candidate}" in str(error.value)
    assert f"recover the completed mp4 from {partial}" in str(error.value)
    assert "hard links" in str(error.value)
    assert partial.read_bytes() == b"completed MP4"
    assert not candidate.exists()
    if occupied:
        assert output.read_bytes() == b"existing output"
        assert set(output.parent.iterdir()) == {partial.parent, output}
    else:
        assert list(output.parent.iterdir()) == [partial.parent]
    assert source.read_bytes() == b"original source image"


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent", ["file", "directory", "dangling-symlink", "source-symlink"])
async def test_output_created_during_encode_is_not_replaced(encode_case, run_encode, concurrent):
    source, title, output = encode_case
    target = source if concurrent == "source-symlink" else source.parent / "absent.mp4"
    # check symlink privileges before entering the encode, so a skip leaves no partial job.
    if concurrent in {"dangling-symlink", "source-symlink"}:
        probe = source.parent / "symlink-check.mp4"
        symlink_or_skip(probe, target)
        probe.unlink()

    async def complete_with_competitor(command, on_progress):
        Path(command[-1]).write_bytes(b"our completed MP4")
        if concurrent == "file":
            output.write_bytes(b"another writer's MP4")
        elif concurrent == "directory":
            output.mkdir()
            (output / "keep.txt").write_text("keep")
        else:
            output.symlink_to(target)

    run_encode.side_effect = complete_with_competitor
    published = await encode_title(source, title, output, audio_index=9)

    run_encode.assert_awaited_once()
    partial = Path(run_encode.call_args.args[0][-1])
    assert published == output.with_stem(f"{output.stem} (1)")
    assert published.read_bytes() == b"our completed MP4"
    assert not partial.parent.exists()
    assert set(output.parent.iterdir()) == {published, output}
    assert source.read_bytes() == b"original source image"
    if concurrent == "file":
        assert output.read_bytes() == b"another writer's MP4"
    elif concurrent == "directory":
        assert (output / "keep.txt").read_text() == "keep"
    else:
        assert output.is_symlink()
        assert output.readlink() == target
        if concurrent == "dangling-symlink":
            assert not target.exists()


@pytest.mark.asyncio
async def test_publication_races_retry_each_suffix_without_reencoding(
    encode_case, run_encode, monkeypatch
):
    source, title, output = encode_case
    missing = source.parent / "missing.mp4"
    probe = source.parent / "symlink-check.mp4"
    symlink_or_skip(probe, missing)
    probe.unlink()
    real_link = encoder.os.link
    attempts = []

    def compete(partial, candidate):
        assert not encoder.os.path.lexists(candidate)
        attempts.append(candidate)
        if len(attempts) == 1:
            candidate.write_bytes(b"another writer's mp4")
        elif len(attempts) == 2:
            candidate.mkdir()
            (candidate / "keep.txt").write_text("keep")
        elif len(attempts) == 3:
            candidate.symlink_to(source)
        elif len(attempts) == 4:
            candidate.symlink_to(missing)
        real_link(partial, candidate)

    link = Mock(side_effect=compete)
    monkeypatch.setattr(encoder.os, "link", link)

    async def complete(command, on_progress):
        Path(command[-1]).write_bytes(b"our completed mp4")

    run_encode.side_effect = complete
    published = await encode_title(source, title, output, audio_index=9)

    run_encode.assert_awaited_once()
    partial = Path(run_encode.call_args.args[0][-1])
    expected = [output] + [output.with_stem(f"{output.stem} ({number})") for number in range(1, 5)]
    assert attempts == expected
    assert [entry.args for entry in link.call_args_list] == [(partial, path) for path in expected]
    assert published == expected[-1]
    assert published.read_bytes() == b"our completed mp4"
    assert output.read_bytes() == b"another writer's mp4"
    assert (expected[1] / "keep.txt").read_text() == "keep"
    assert expected[2].readlink() == source
    assert expected[3].readlink() == missing
    assert not missing.exists()
    assert source.read_bytes() == b"original source image"
    assert not partial.parent.exists()
    assert set(output.parent.iterdir()) == set(expected)


@pytest.mark.asyncio
async def test_concurrent_encodes_publish_distinct_outputs(encode_case, run_encode):
    source, title, output = encode_case
    ready = asyncio.Event()

    async def complete(command, on_progress):
        Path(command[-1]).write_bytes(f"encode {run_encode.await_count}".encode())
        if run_encode.await_count == 3:
            ready.set()
        await ready.wait()

    run_encode.side_effect = complete
    published = await asyncio.wait_for(
        asyncio.gather(*(encode_title(source, title, output, audio_index=9) for _ in range(3))), 5
    )

    assert run_encode.await_count == 3
    expected = {
        output,
        output.with_stem(f"{output.stem} (1)"),
        output.with_stem(f"{output.stem} (2)"),
    }
    assert set(published) == expected
    assert {path.read_bytes() for path in published} == {b"encode 1", b"encode 2", b"encode 3"}
    assert set(output.parent.iterdir()) == expected
    assert source.read_bytes() == b"original source image"


@pytest.mark.asyncio
async def test_cancelling_publication_retries_removes_only_private_staging(
    encode_case, run_encode, monkeypatch
):
    source, title, output = encode_case
    started = asyncio.Event()
    real_link = encoder.os.link
    competitors = []

    def compete(partial, candidate):
        candidate.write_bytes(b"another writer's mp4")
        competitors.append(candidate)
        started.set()
        real_link(partial, candidate)

    monkeypatch.setattr(encoder.os, "link", compete)

    async def complete(command, on_progress):
        Path(command[-1]).write_bytes(b"our completed mp4")

    run_encode.side_effect = complete
    task = asyncio.create_task(encode_title(source, title, output, audio_index=9))
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        run_encode.assert_awaited_once()
        assert competitors
        assert set(output.parent.iterdir()) == set(competitors)
        assert all(path.read_bytes() == b"another writer's mp4" for path in competitors)
        assert not Path(run_encode.call_args.args[0][-1]).parent.exists()
        assert source.read_bytes() == b"original source image"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)


@pytest.mark.asyncio
async def test_run_encode_emits_complete_progress_blocks_and_drains_stderr():
    lines = "\n".join(
        [
            "not a key-value line",
            "frame=1",
            "out_time_us=1500000",
            "speed=2x",
            "total_size=12",
            "progress=continue",
            "out_time_ms=2500000",
            "progress=continue",
            "out_time_us=N/A",
            "total_size=N/A",
            "speed=N/A",
            "progress=end",
            "out_time_us=999999999",
            "",
        ]
    )
    script = (
        "import sys; assert sys.stdin.buffer.read() == b''; "
        "sys.stderr.buffer.write(b'warning' * 40000); sys.stderr.flush(); "
        f"sys.stdout.write({lines!r})"
    )
    updates = []
    await asyncio.wait_for(_run_encode([sys.executable, "-c", script], updates.append), 10)
    assert updates == [
        Progress(seconds=1.5, speed="2x", total_size=12),
        Progress(seconds=2.5),
        Progress(),
    ]


@pytest.mark.asyncio
async def test_run_encode_can_ignore_progress_without_callback():
    await asyncio.wait_for(
        _run_encode([sys.executable, "-c", "print('out_time_us=1000000\\nprogress=end')"], None), 10
    )


@pytest.mark.asyncio
async def test_run_encode_failure_reports_exit_and_bounded_stderr_tail():
    script = (
        "import sys; "
        "sys.stderr.buffer.write(b'discard this prefix\\n' + b'x' * 200000 + b'\\nlast error: \\xff\\n'); "
        "sys.exit(7)"
    )
    with pytest.raises(FFmpegError) as error:
        await asyncio.wait_for(_run_encode([sys.executable, "-c", script], None), 10)
    message = str(error.value)
    assert message.startswith("ffmpeg failed (exit 7):\n")
    assert message.endswith("last error: \ufffd")
    assert "discard this prefix" not in message
    assert len(message) <= len("ffmpeg failed (exit 7):\n") + 32 * 4096


@pytest.mark.asyncio
async def test_run_encode_failure_with_empty_stderr_still_reports_exit():
    with pytest.raises(FFmpegError, match="ffmpeg failed \\(exit 3\\)"):
        await asyncio.wait_for(_run_encode([sys.executable, "-c", "raise SystemExit(3)"], None), 10)


@pytest.mark.asyncio
async def test_run_encode_missing_executable_is_wrapped(tmp_path):
    with pytest.raises(FFmpegError, match="could not start") as error:
        await _run_encode([str(tmp_path / "missing executable")], None)
    assert isinstance(error.value.__cause__, OSError)


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", ["cancel", "callback-error"])
async def test_run_encode_reaps_child_when_interrupted(monkeypatch, interrupt):
    started = asyncio.Event()
    failure = RuntimeError("progress callback failed")
    stop = AsyncMock(wraps=encoder.stop_process)
    monkeypatch.setattr(encoder, "stop_process", stop)

    def callback(progress):
        started.set()
        if interrupt == "callback-error":
            raise failure

    script = "import time; print('out_time_us=1\\nprogress=continue', flush=True); time.sleep(60)"
    task = asyncio.create_task(_run_encode([sys.executable, "-c", script], callback))
    try:
        await asyncio.wait_for(started.wait(), 10)
        if interrupt == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
        else:
            with pytest.raises(RuntimeError) as error:
                await asyncio.wait_for(task, 10)
            assert error.value is failure
        stop.assert_awaited_once()
        assert stop.call_args.args[0].returncode is not None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)
