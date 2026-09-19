"""ffmpeg capabilities, cancellable probing, and explicit command construction."""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path

from .models import EncodeSettings, Stream, Title


class FFmpegError(RuntimeError):
    """a tool or media operation failed; safe to display to the user."""


class FFmpegUnavailableError(FFmpegError):
    """a tool could not be started; retrying other titles will not help."""


async def stop_process(process: asyncio.subprocess.Process) -> None:
    """reap a child after cancellation, escalating if termination is ignored."""
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    await process.wait()


async def run_capture(args: list[str], *, timeout: float = 30) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise FFmpegUnavailableError(
            f"could not run {args[0]!r}: {exc}. check its installation and path."
        ) from exc
    # keep draining pipes during termination; an undrained pipe can prevent wait() completing.
    communicate = asyncio.create_task(process.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(communicate), timeout)
    except (TimeoutError, asyncio.CancelledError) as exc:
        await stop_process(process)
        await communicate
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise FFmpegError(f"timed out after {timeout:g}s: {shlex.join(args)}") from exc
    if process.returncode:
        details = stderr.decode(errors="replace").strip()
        raise FFmpegError(
            f"command failed (exit {process.returncode}): {shlex.join(args)}\n{details}"
        )
    return stdout.decode(errors="replace")


def _has_feature(listing: str, name: str) -> bool:
    # help requests may exit zero for unknown formats; parse the actual feature tables.
    return any(
        len(fields := line.split()) >= 2 and fields[1] == name for line in listing.splitlines()
    )


async def dependency_issues(*, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> list[str]:
    issues: list[str] = []
    for tool in (ffmpeg, ffprobe):
        try:
            listing = await run_capture([tool, "-hide_banner", "-demuxers"])
            if not _has_feature(listing, "dvdvideo"):
                issues.append(
                    f"{tool}: missing dvdvideo demuxer. use FFmpeg 7+ built with "
                    "--enable-libdvdnav --enable-libdvdread and --enable-gpl "
                    "(both ffmpeg and ffprobe need it). "
                    "reinstalling Python dependencies cannot fix this."
                )
                continue
            options = await run_capture([tool, "-hide_banner", "-h", "demuxer=dvdvideo"])
            if not any(line.split()[:1] == ["-preindex"] for line in options.splitlines()):
                issues.append(
                    f"{tool}: missing dvdvideo preindex option; use a complete dvdvideo build."
                )
        except FFmpegError as exc:
            issues.append(str(exc))
    for option, kind, names in (
        ("-encoders", "encoder", ("libx264", "aac")),
        ("-filters", "filter", ("bwdif",)),
        ("-muxers", "muxer", ("mp4",)),
    ):
        try:
            listing = await run_capture([ffmpeg, "-hide_banner", option])
            for name in names:
                if not _has_feature(listing, name):
                    issues.append(
                        f"{ffmpeg}: missing {name} {kind}; install a build that includes it."
                    )
        except FFmpegError as exc:
            if str(exc) not in issues:
                issues.append(str(exc))
    return issues


def choose_english_audio(title: Title) -> Stream | None:
    return max(
        (
            stream
            for stream in title.audio_streams
            if (stream.language or "").strip().casefold() in {"eng", "en", "english"}
        ),
        key=lambda stream: (stream.channels or 0, stream.codec_name == "ac3", -stream.index),
        default=None,
    )


def build_encode_command(
    iso: str | Path,
    title: Title,
    output: str | Path,
    *,
    audio_index: int | None,
    settings: EncodeSettings = EncodeSettings(),
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    source, destination = Path(iso).expanduser().resolve(), Path(output).expanduser().resolve()
    if source == destination:
        raise FFmpegError("the output must not replace the source image.")
    if destination.suffix.casefold() != ".mp4":
        raise FFmpegError("the output must have an .mp4 extension.")
    if not 1 <= title.number <= 99:
        raise FFmpegError("dvd title numbers must be between 1 and 99.")
    if title.video is None:
        raise FFmpegError(f"title {title.number} has no video stream.")
    audio = next((stream for stream in title.audio_streams if stream.index == audio_index), None)
    if audio_index is not None and audio is None:
        raise FFmpegError(f"title {title.number} has no audio stream with index {audio_index}.")
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-n",
        "-f",
        "dvdvideo",
        "-title",
        str(title.number),
        "-i",
        str(source),
        "-map",
        f"0:{title.video.index}",
    ]
    if settings.deinterlace != "off":
        mode = "interlaced" if settings.deinterlace == "auto" else "all"
        args += ["-vf", f"bwdif=mode=send_frame:parity=auto:deint={mode}"]
    args += [
        "-c:v",
        "libx264",
        "-preset",
        settings.preset,
        "-crf",
        str(settings.crf),
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
    ]
    if audio is None:
        args += ["-an"]
    else:
        args += ["-map", f"0:{audio.index}", "-c:a", "aac", "-profile:a", "aac_low"]
        if settings.stereo:
            args += ["-ac", "2"]
        bitrate = "384k" if not settings.stereo and (audio.channels or 0) > 2 else "192k"
        args += ["-b:a", bitrate]
    args += [
        "-sn",
        "-dn",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        "-f",
        "mp4",
        str(destination),
    ]
    return args
