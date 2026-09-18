"""run one encode safely, leaving source images and existing outputs untouched."""

from __future__ import annotations

import asyncio
import math
import os
import shutil
import tempfile
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from itertools import count
from pathlib import Path

from .ffmpeg import FFmpegError, build_encode_command, stop_process
from .models import EncodeSettings, Title


@dataclass(frozen=True)
class Progress:
    seconds: float = 0
    speed: str = "N/A"
    total_size: int | None = None


def parse_progress(values: Mapping[str, str]) -> Progress:
    seconds = 0.0
    try:
        # ffmpeg's historical out_time_ms is also microseconds, despite its name.
        raw = values.get("out_time_us", values.get("out_time_ms"))
        if raw is not None:
            seconds = float(raw) / 1_000_000
        else:
            timestamp = values.get("out_time", "0:0:0")
            hours, minutes, secs = timestamp.lstrip("-").split(":")
            seconds = float(hours) * 3600 + float(minutes) * 60 + float(secs)
            if timestamp.startswith("-"):
                seconds = -seconds
    except ValueError:
        pass
    if not math.isfinite(seconds) or seconds < 0:
        seconds = 0.0
    try:
        size = max(0, int(values["total_size"]))
    except (KeyError, ValueError):
        size = None
    return Progress(seconds=seconds, speed=values.get("speed", "N/A"), total_size=size)


def _output_candidates(path: Path) -> Iterator[Path]:
    yield path
    for number in count(1):
        yield path.with_stem(f"{path.stem} ({number})")


def available_output_path(path: Path) -> Path:
    """find a free name without reserving it; existing numeric suffixes stay literal."""
    return next(
        candidate for candidate in _output_candidates(path) if not os.path.lexists(candidate)
    )


def output_path(
    iso: Path, title: Title, output_dir: Path | None = None, *, unique: bool = True
) -> Path:
    """preview an available name, or get a stable encode base with unique=false.

    callers can plan with unique=false, preview available_output_path(base), then
    pass base to encode_title and use its returned path as the published filename.
    previews are best-effort and do not reserve a filename.
    """
    iso = iso.expanduser().resolve()
    root = output_dir.expanduser().resolve() if output_dir is not None else iso.parent
    base = root / iso.stem / f"{iso.stem} - Title {title.number}.mp4"
    return available_output_path(base) if unique else base


async def _run_encode(args: list[str], on_progress: Callable[[Progress], None] | None) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise FFmpegError(f"could not start {args[0]!r}: {exc}") from exc
    assert process.stdout is not None and process.stderr is not None
    errors: deque[bytes] = deque(maxlen=32)

    async def drain_errors() -> None:
        while chunk := await process.stderr.read(4096):
            errors.append(chunk)

    stderr_task = asyncio.create_task(drain_errors())
    values: dict[str, str] = {}
    try:
        while line := await process.stdout.readline():
            key, separator, value = line.decode(errors="replace").strip().partition("=")
            if separator:
                values[key] = value
                if key == "progress":
                    if on_progress is not None:
                        on_progress(parse_progress(values))
                    values.clear()
        await process.wait()
        await stderr_task
        if process.returncode:
            details = b"".join(errors).decode(errors="replace").strip()
            raise FFmpegError(f"ffmpeg failed (exit {process.returncode}):\n{details}")
    finally:
        # also covers cancellation and exceptions raised by a progress callback.
        # drain stdout during termination so a blocked pipe cannot prevent reaping.
        drain_stdout = asyncio.create_task(process.stdout.read())
        await stop_process(process)
        await asyncio.gather(stderr_task, drain_stdout)


async def encode_title(
    iso: Path,
    title: Title,
    output: Path,
    *,
    audio_index: int | None,
    settings: EncodeSettings = EncodeSettings(),
    ffmpeg: str = "ffmpeg",
    on_progress: Callable[[Progress], None] | None = None,
) -> Path:
    """stage on the destination filesystem; return the atomically published path.

    output is the literal base: collisions append increasing numeric suffixes
    without re-encoding. pass a stable base, not a preview, to avoid nested suffixes.
    completed staging files are retained if the filesystem cannot publish them,
    e.g. on a filesystem without hard-link support. the error gives a recovery path.
    """
    output = output.expanduser().absolute()
    source = iso.expanduser().absolute()
    # resolve parent aliases, but treat a different final output symlink as a collision.
    requested_entry = output.parent.resolve() / output.name
    if output == source or requested_entry in (
        source.parent.resolve() / source.name,
        source.resolve(),
    ):
        raise FFmpegError("the output must not replace the source image.")
    # validate a free filename, not an occupied symlink's target, before creating directories.
    build_encode_command(
        iso,
        title,
        available_output_path(output),
        audio_index=audio_index,
        settings=settings,
        ffmpeg=ffmpeg,
    )
    staging: Path | None = None
    retain = False
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".dvd-ripper-", dir=output.parent))
        partial = staging / output.name
        command = build_encode_command(
            iso,
            title,
            partial,
            audio_index=audio_index,
            settings=settings,
            ffmpeg=ffmpeg,
        )
        await _run_encode(command, on_progress)
        if not partial.is_file() or partial.stat().st_size == 0:
            raise FFmpegError("ffmpeg exited successfully but produced no nonempty mp4.")
        candidates = _output_candidates(output)
        while True:
            candidate = next(candidates)
            try:
                # the link itself checks for collisions, including publication races.
                os.link(partial, candidate)
            except FileExistsError:
                # let cancellation and other jobs run even under repeated contention.
                await asyncio.sleep(0)
            except OSError as exc:
                retain = True
                raise FFmpegError(
                    f"encoded successfully but could not publish {candidate}: {exc}. "
                    f"existing files were not replaced. recover the completed mp4 from {partial}. "
                    "the destination filesystem must support hard links for automatic publishing."
                ) from exc
            else:
                return candidate
    except OSError as exc:
        raise FFmpegError(f"cannot write output {output}: {exc}") from exc
    finally:
        if staging is not None and not retain:
            shutil.rmtree(staging)
