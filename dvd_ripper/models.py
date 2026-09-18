"""small data objects shared by the probe, encoder, and ui."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Stream:
    index: int
    codec_type: str
    codec_name: str | None = None
    language: str | None = None
    title: str | None = None
    channels: int | None = None
    channel_layout: str | None = None
    width: int | None = None
    height: int | None = None
    field_order: str | None = None


@dataclass(frozen=True)
class Title:
    number: int
    duration: float | None
    streams: tuple[Stream, ...] = ()

    @property
    def duration_text(self) -> str:
        if self.duration is None:
            return "unknown"
        hours, remainder = divmod(int(self.duration), 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02}:{minutes:02}:{seconds:02}"

    @property
    def video(self) -> Stream | None:
        return next((stream for stream in self.streams if stream.codec_type == "video"), None)

    @property
    def audio_streams(self) -> tuple[Stream, ...]:
        return tuple(stream for stream in self.streams if stream.codec_type == "audio")

    @property
    def subtitle_streams(self) -> tuple[Stream, ...]:
        return tuple(stream for stream in self.streams if stream.codec_type == "subtitle")


@dataclass(frozen=True)
class DVD:
    path: Path
    titles: tuple[Title, ...]
    warnings: tuple[str, ...] = ()

    @property
    def main_title(self) -> Title | None:
        # a duration-based suggestion, never a claim that title 1 is the movie.
        return max(
            (title for title in self.titles if title.video is not None),
            key=lambda title: (title.duration if title.duration is not None else -1, -title.number),
            default=None,
        )


@dataclass(frozen=True)
class EncodeSettings:
    crf: int = 21
    preset: str = "medium"
    deinterlace: str = "auto"
    stereo: bool = True

    def __post_init__(self) -> None:
        if type(self.crf) is not int or not 0 <= self.crf <= 51:
            raise ValueError("crf must be an integer between 0 and 51.")
        if self.preset not in {
            "ultrafast",
            "superfast",
            "veryfast",
            "faster",
            "fast",
            "medium",
            "slow",
            "slower",
            "veryslow",
        }:
            raise ValueError("unknown x264 preset.")
        if self.deinterlace not in {"auto", "on", "off"}:
            raise ValueError("deinterlace must be auto, on, or off.")
        if type(self.stereo) is not bool:
            raise ValueError("stereo must be a boolean.")
