"""discover disc images and scan ffmpeg dvdvideo titles without guessing eof."""

from __future__ import annotations

import json
import math
import os
import re
import stat
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NoReturn

from .ffmpeg import FFmpegError, FFmpegUnavailableError, run_capture
from .models import DVD, Stream, Title

MAX_DVD_TITLES = 99
# preindex performs an extra read of the title; the normal 30s probe limit is too short.
TITLE_PROBE_TIMEOUT = 180.0

# dvdvideo_ifo_open() emits this only when opt_title > tt_srpt->nr_of_srpts.
# verified against https://raw.githubusercontent.com/FFmpeg/FFmpeg/master/libavformat/dvdvideodec.c
# match a complete diagnostic line, not a filename, generic eof, or invalid-title error.
_TITLE_NOT_FOUND = re.compile(
    r"^[ \t]*(?:\[dvdvideo @ [^\]\r\n]+\][ \t]*)?"
    r"(?:\[error\][ \t]*)?Title ([1-9][0-9]*) not found[ \t]*\r?$",
    re.MULTILINE,
)


class _TitleNotFound(FFmpegError):
    """a confirmed end of the disc's title table, not a general probe failure."""


def _walk_error(error: OSError) -> NoReturn:
    # os.walk otherwise silently ignores unreadable directories.
    raise error


def _image_file(path: Path) -> Path:
    if path.suffix.casefold() not in {".iso", ".img"}:
        raise ValueError(f"unsupported image '{path}'; choose an .iso or .img file.")
    resolved = path.resolve(strict=True)
    if not stat.S_ISREG(resolved.stat().st_mode):
        raise ValueError(f"'{path}' is not a regular disc image file.")
    with resolved.open("rb"):
        pass
    return resolved


def find_isos(paths: Iterable[Path], *, recursive: bool = False) -> list[Path]:
    """return sorted, resolved .iso/.img files, deduplicated across all inputs.

    suffixes and ordering are case-insensitive, with a case-sensitive tie-breaker.
    inputs may be files or directories; invalid or unreadable explicit inputs
    raise actionable ValueError exceptions. empty directories are valid. image
    contents are not inspected. recursive discovery does not follow directory
    symlinks, preventing cycles; an explicitly supplied directory symlink works.
    """
    images: set[Path] = set()
    for supplied in paths:
        path = Path(supplied)
        try:
            path = path.expanduser()
            resolved = path.resolve(strict=True)
            mode = resolved.stat().st_mode
            if stat.S_ISREG(mode):
                images.add(_image_file(path))
            elif stat.S_ISDIR(mode):
                for root, _, files in os.walk(resolved, onerror=_walk_error):
                    for name in files:
                        if Path(name).suffix.casefold() in {".iso", ".img"}:
                            images.add(_image_file(Path(root) / name))
                    if not recursive:
                        break
            else:
                raise ValueError(f"'{path}' is not a regular disc image file or directory.")
        except (OSError, RuntimeError) as exc:
            raise ValueError(
                f"cannot discover disc images from '{path}': {exc}. "
                "check that the path exists and you have permission to read it."
            ) from exc
    return sorted(images, key=lambda image: (str(image).casefold(), str(image)))


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{field} must be a json object")
    return value


def _optional_string(value: object, field: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    return value


def _optional_integer(value: object, field: str) -> int | None:
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError(f"{field} must be a nonnegative integer or null")
    return value


def _duration(value: object, field: str) -> float | None:
    if value is None or value == "N/A":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise TypeError(f"{field} must be seconds, N/A, or null")
    try:
        duration = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{field} is not a valid duration: {value!r}") from exc
    if not math.isfinite(duration) or duration < 0:
        raise ValueError(f"{field} must be finite and nonnegative: {value!r}")
    return duration


def _parse_title(output: str, number: int) -> Title:
    data = _object(json.loads(output), "ffprobe output")
    if "error" in data:
        raise ValueError(f"ffprobe returned an error: {data['error']!r}")
    raw_streams = data.get("streams")
    if not isinstance(raw_streams, list):
        raise TypeError("streams must be a json array")
    format_data = _object(data.get("format", {}), "format")
    duration = _duration(format_data.get("duration"), "format.duration")
    streams: list[Stream] = []
    stream_durations: list[float] = []
    indices: set[int] = set()
    for position, value in enumerate(raw_streams):
        field = f"streams[{position}]"
        raw = _object(value, field)
        index = _optional_integer(raw.get("index"), f"{field}.index")
        if index is None:
            raise ValueError(f"{field}.index is required")
        if index in indices:
            raise ValueError(f"duplicate stream index {index}")
        indices.add(index)
        codec_type = _optional_string(raw.get("codec_type"), f"{field}.codec_type")
        if not codec_type:
            raise ValueError(f"{field}.codec_type must be a nonempty string")
        tags = _object(raw.get("tags", {}), f"{field}.tags")
        stream_duration = _duration(raw.get("duration"), f"{field}.duration")
        if stream_duration is not None:
            stream_durations.append(stream_duration)
        streams.append(
            Stream(
                index=index,
                codec_type=codec_type,
                codec_name=_optional_string(raw.get("codec_name"), f"{field}.codec_name"),
                language=_optional_string(tags.get("language"), f"{field}.tags.language"),
                title=_optional_string(tags.get("title"), f"{field}.tags.title"),
                channels=_optional_integer(raw.get("channels"), f"{field}.channels"),
                channel_layout=_optional_string(
                    raw.get("channel_layout"), f"{field}.channel_layout"
                ),
                width=_optional_integer(raw.get("width"), f"{field}.width"),
                height=_optional_integer(raw.get("height"), f"{field}.height"),
                field_order=_optional_string(raw.get("field_order"), f"{field}.field_order"),
            )
        )
    if duration is None:
        duration = max(stream_durations, default=None)
    return Title(number=number, duration=duration, streams=tuple(streams))


async def probe_dvd_title(path: Path, number: int, *, ffprobe: str = "ffprobe") -> Title:
    """probe one title with preindex enabled and a 180-second timeout.

    missing/null/N/A durations fall back to the longest reported stream duration,
    then None. zero is valid. malformed, negative, and non-finite durations fail
    rather than being silently replaced. a title without video is returned for
    scan_dvd to report and skip. ffmpeg failures and malformed json raise
    ffmpeg errors with path/title context, preserving unavailable-tool errors;
    cancellation propagates to run_capture.
    """
    if type(number) is not int or not 1 <= number <= MAX_DVD_TITLES:
        raise ValueError("dvd title number must be an integer between 1 and 99.")
    path = Path(path).expanduser().resolve()
    args = [
        ffprobe,
        "-v",
        "error",
        "-f",
        "dvdvideo",
        "-title",
        str(number),
        "-preindex",
        "1",
        "-show_streams",
        "-show_format",
        "-print_format",
        "json",
        "-i",
        str(path),
    ]
    try:
        output = await run_capture(args, timeout=TITLE_PROBE_TIMEOUT)
    except FFmpegError as exc:
        message = f"could not probe title {number} of '{path}':\n{exc}"
        if isinstance(exc, FFmpegUnavailableError):
            raise FFmpegUnavailableError(message) from exc
        if any(int(match[1]) == number for match in _TITLE_NOT_FOUND.finditer(str(exc))):
            raise _TitleNotFound(message) from exc
        raise FFmpegError(message) from exc
    try:
        return _parse_title(output, number)
    except (TypeError, ValueError) as exc:
        raise FFmpegError(f"malformed ffprobe data for title {number} of '{path}': {exc}") from exc


async def scan_dvd(
    path: Path,
    *,
    ffprobe: str = "ffprobe",
    on_progress: Callable[[str], None] | None = None,
) -> DVD:
    """scan title ids 1..99 sequentially, returning only titles with video.

    only the verified dvdvideo 'Title N not found' diagnostic ends the scan
    early. unreadable titles and successful json without video are skipped with
    a dvd warning (also sent to on_progress). unavailable tools and cancellation
    abort the scan. no video titles is an error, including all skip reasons. each
    probe has a 180-second timeout; preindexing can make a full scan lengthy.
    """
    path = Path(path).expanduser().resolve()
    titles: list[Title] = []
    warnings: list[str] = []
    for number in range(1, MAX_DVD_TITLES + 1):
        if on_progress is not None:
            on_progress(f"scanning title {number} of '{path.name}' (preindexing)…")
        try:
            title = await probe_dvd_title(path, number, ffprobe=ffprobe)
        except _TitleNotFound:
            break
        except FFmpegUnavailableError:
            raise
        except FFmpegError as exc:
            warning = f"skipped title {number}: {exc}"
        else:
            if any(stream.codec_type == "video" for stream in title.streams):
                titles.append(title)
                continue
            warning = f"skipped title {number}: no video stream was reported by ffprobe."
        warnings.append(warning)
        if on_progress is not None:
            on_progress(warning)
    if not titles:
        details = "\n".join(warnings)
        raise FFmpegError(
            f"no video titles found in '{path}'. check that the image is a readable "
            f"dvd-video disc and review any skipped-title errors below.\n{details}"
        )
    return DVD(path=path, titles=tuple(titles), warnings=tuple(warnings))
