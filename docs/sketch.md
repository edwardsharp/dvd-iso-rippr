Absolutely. I’d make v1 deliberately pragmatic: **Textual UI + `ffmpeg-python` \+ direct `ffprobe`/FFmpeg subprocesses**, with DVD title discovery as the first-class operation.

 One correction to my previous answer: `ffmpeg-python` is useful for constructing commands, but for this particular project I’d use **`subprocess` for probing/running FFmpeg** and keep `ffmpeg-python` optional. That makes the code easier to debug and gives us complete control over FFmpeg's DVD-specific arguments.

 Here’s a working starting point.

 ## Project layout

```
dvd-ripper/
├── pyproject.toml
├── README.md
└── dvd_ripper/
    ├── __init__.py
    ├── __main__.py
    ├── app.py
    ├── ffmpeg.py
    ├── models.py
    └── scanner.py
```

 Install it with:

```
cd dvd-ripper
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

 You also need an FFmpeg build with the `dvdvideo` demuxer:

```
ffmpeg -h demuxer=dvdvideo
```

---

 ## `pyproject.toml`

 pyproject.toml

```
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "dvd-ripper"
version = "0.1.0"
description = "Interactive DVD ISO to H.264/AAC MP4 converter"
requires-python = ">=3.10"
dependencies = [
    "textual>=0.70",
]

[project.scripts]
dvd-ripper = "dvd_ripper.app:main"

[tool.setuptools.packages.find]
include = ["dvd_ripper*"]
```

 I left `ffmpeg-python` out intentionally for now. Once the basic program is working, we can decide whether its command-building API actually improves things.

---

 # Data model

 The first useful abstraction is a DVD title.

 ## `dvd_ripper/models.py`

 dvd\_ripper/models.py

```
from dataclasses import dataclass, field

@dataclass
class Stream:
    index: int
    codec_type: str
    codec_name: str | None = None
    language: str | None = None
    title: str | None = None
    channels: int | None = None
    channel_layout: str | None = None

@dataclass
class Title:
    number: int
    duration: float
    streams: list[Stream] = field(default_factory=list)

    @property
    def duration_text(self) -> str:
        total = int(self.duration)
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)

        if hours:
            return f"{hours}:{minutes:02}:{seconds:02}"

        return f"{minutes}:{seconds:02}"

    @property
    def video(self) -> Stream | None:
        return next(
            (s for s in self.streams if s.codec_type == "video"),
            None,
        )

    @property
    def audio_streams(self) -> list[Stream]:
        return [
            s for s in self.streams
            if s.codec_type == "audio"
        ]

    @property
    def subtitle_streams(self) -> list[Stream]:
        return [
            s for s in self.streams
            if s.codec_type == "subtitle"
        ]

    @property
    def likely_main_feature(self) -> bool:
        """
        Very deliberately simple heuristic for v1.

        The longest title is considered the likely main feature.
        """
        return False

@dataclass
class DVD:
    path: str
    titles: list[Title]
```

 We'll fix the `likely_main_feature` property when the scanner has all the titles available; I prefer keeping that heuristic outside the data object.

---

 # FFmpeg/DV​D scanner

 Here's where the interesting stuff starts.

 ## `dvd_ripper/ffmpeg.py`

 dvd\_ripper/ffmpeg.py

````
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .models import Stream, Title

class FFmpegError(RuntimeError):
    pass

def run_command(
    args: list[str],
    *,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            text=True,
            capture_output=capture_output,
            check=True,
        )
    except FileNotFoundError as exc:
        raise FFmpegError(
            f"Could not find executable: {args[0]}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr or ""
        raise FFmpegError(
            f"Command failed:\n\n{' '.join(args)}\n\n{stderr}"
        ) from exc

def probe_dvd_title(
    iso_path: str | Path,
    title_number: int,
) -> Title:
    """
    Probe one DVD title through FFmpeg's dvdvideo demuxer.

    Example equivalent command:

        ffprobe -f dvdvideo -title 1 -print_format json -show_streams ...
    """

    cmd = [
        "ffprobe",
        "-v", "error",
        "-f", "dvdvideo",
        "-title", str(title_number),
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        str(iso_path),
    ]

    result = run_command(cmd)

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise FFmpegError(
            f"Could not parse ffprobe output for title {title_number}"
        ) from exc

    streams: list[Stream] = []

    for raw in data.get("streams", []):
        tags = raw.get("tags", {})

        streams.append(
            Stream(
                index=raw["index"],
                codec_type=raw.get("codec_type", ""),
                codec_name=raw.get("codec_name"),
                language=tags.get("language"),
                title=tags.get("title"),
                channels=raw.get("channels"),
                channel_layout=raw.get("channel_layout"),
            )
        )

    duration = float(
        data.get("format", {}).get("duration") or 0
    )

    return Title(
        number=title_number,
        duration=duration,
        streams=streams,
    )

def probe_titles(
    iso_path: str | Path,
    *,
    max_titles: int = 99,
) -> list[Title]:
    """
    Probe titles until FFmpeg reports that the title doesn't exist.

    DVD title numbering starts at 1.
    """

    titles: list[Title] = []

    for number in range(1, max_titles + 1):
        try:
            title = probe_dvd_title(iso_path, number)
        except FFmpegError:
            # Once titles stop existing, we're done.
            if titles:
                break
            raise

        # Ignore pathological/empty titles.
        if title.duration > 0:
            titles.append(title)

    return titles

def choose_english_audio(title: Title) -> Stream | None:
    """
    Select the most useful English audio stream.

    Preference:
      1. English language
      2. More channels
      3. AC3
    """

    candidates = [
        s for s in title.audio_streams
        if (s.language or "").lower() in {
            "eng",
            "en",
            "english",
        }
    ]

    if not candidates:
        return None

    def score(stream: Stream) -> tuple[int, int, int]:
        channels = stream.channels or 0
        ac3 = int(stream.codec_name == "ac3")
        return (
            channels,
            ac3,
            -stream.index,
        )

    return max(candidates, key=score)

def choose_english_subtitle(title: Title) -> Stream | None:
    candidates = [
        s for s in title.subtitle_streams
        if (s.language or "").lower() in {
            "eng",
            "en",
            "english",
        }
    ]

    if not candidates:
        return None

    return candidates[0]

def build_encode_command(
    iso_path: str | Path,
    title: Title,
    output_path: str | Path,
) -> list[str]:
    """
    Construct the actual FFmpeg command.

    v1 intentionally keeps this conservative.
    """

    audio = choose_english_audio(title)

    if audio is None:
        raise FFmpegError(
            f"No English audio found for title {title.number}"
        )

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-y",

        # DVD input.
        "-f", "dvdvideo",
        "-title", str(title.number),
        "-i", str(iso_path),

        # Video.
        "-map", "0:v:0",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "21",
        "-profile:v", "high",
        "-level", "4.0",
        "-pix_fmt", "yuv420p",

        # Audio.
        "-map", f"0:{audio.index}",
        "-c:a", "aac",
        "-b:a", "192k",

        # Browser-friendly MP4.
        "-movflags", "+faststart",

        str(output_path),
    ]

    return cmd
:::
---

There is one thing worth calling out here: **DVD stream indexes and FFmpeg's `0:a:0` stream selectors aren't necessarily the same thing**, so for now I'm mapping the actual ffprobe stream index with `-map 0:<index>`. That's much less magical.

---

# Scanner

## `dvd_ripper/scanner.py`

:::writing{variant="document" id="42917" title="dvd_ripper/scanner.py"}
```python
from pathlib import Path

from .ffmpeg import probe_titles
from .models import DVD

ISO_SUFFIXES = {".iso", ".img"}

def find_isos(path: Path) -> list[Path]:
    path = path.expanduser().resolve()

    if path.is_file():
        if path.suffix.lower() in ISO_SUFFIXES:
            return [path]
        return []

    if not path.is_dir():
        return []

    return sorted(
        p for p in path.iterdir()
        if p.is_file()
        and p.suffix.lower() in ISO_SUFFIXES
    )

def scan_dvd(path: Path) -> DVD:
    titles = probe_titles(path)

    return DVD(
        path=str(path),
        titles=titles,
    )
````

---

 # Textual UI

 Now the fun part.

 I'm intentionally keeping the UI simple enough that you can actually understand and modify it.

 ## `dvd_ripper/app.py`

 dvd\_ripper/app.py

```
from __future__ import annotations

import sys
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Label,
    ListItem,
    ListView,
    Static,
)

from .ffmpeg import (
    FFmpegError,
    build_encode_command,
    choose_english_audio,
    choose_english_subtitle,
)
from .models import DVD, Title
from .scanner import find_isos, scan_dvd

class TitleRow(ListItem):
    def __init__(
        self,
        title: Title,
        selected: bool,
        *,
        id: str,
    ) -> None:
        super().__init__(id=id)

        self.title = title
        self.selected = selected

    def compose(self) -> ComposeResult:
        video = self.title.video

        if video:
            resolution = ""
        else:
            resolution = "no video"

        yield Checkbox(
            self.label(),
            value=self.selected,
            id=f"check-{self.title.number}",
        )

    def label(self) -> str:
        video = self.title.video

        if video:
            resolution = "DVD video"
        else:
            resolution = "no video"

        audio = choose_english_audio(self.title)

        if audio:
            channels = (
                audio.channel_layout
                or f"{audio.channels or '?'}ch"
            )
            audio_text = f"ENG {channels}"
        else:
            audio_text = "no ENG audio"

        subtitle = choose_english_subtitle(self.title)
        subtitle_text = "ENG subs" if subtitle else "no ENG subs"

        return (
            f"Title {self.title.number:<2} "
            f"{self.title.duration_text:>8}  "
            f"{resolution:<10} "
            f"{audio_text:<12} "
            f"{subtitle_text}"
        )

class DVDPage(Vertical):
    def __init__(self, dvd: DVD) -> None:
        super().__init__()
        self.dvd = dvd

    def compose(self) -> ComposeResult:
        longest = max(
            self.dvd.titles,
            key=lambda t: t.duration,
        )

        yield Label(
            f"[b]{Path(self.dvd.path).name}[/b]\n"
            f"{self.dvd.path}"
        )

        yield Static("")

        yield Label(
            "[b]DVD titles[/b]  "
            "(longest title is selected as the main feature)"
        )

        list_view = ListView(id="title-list")

        for title in self.dvd.titles:
            row = TitleRow(
                title,
                selected=(title.number == longest.number),
                id=f"title-{title.number}",
            )
            list_view.append(row)

        yield list_view

        yield Static("")

        with Horizontal(id="actions"):
            yield Button(
                "Main only",
                id="main-only",
                variant="primary",
            )
            yield Button(
                "Select all",
                id="select-all",
            )
            yield Button(
                "Start",
                id="start",
                variant="success",
            )

        yield Label(
            "English audio is selected automatically. "
            "Subtitle extraction is not yet enabled.",
            id="status",
        )

class DVDRipperApp(App):
    TITLE = "DVD Ripper"

    CSS = """
    Screen {
        layout: vertical;
    }

    #content {
        height: 1fr;
        padding: 1 2;
    }

    #title-list {
        height: 1fr;
        border: round $primary;
        margin: 1 0;
    }

    TitleRow {
        height: 2;
    }

    #actions {
        height: 3;
        align: left middle;
    }

    Button {
        margin-right: 1;
    }

    #status {
        height: 3;
        color: $text-muted;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
    ]

    def __init__(self, paths: list[Path]) -> None:
        super().__init__()
        self.paths = paths
        self.dvds: list[DVD] = []
        self.current_dvd: DVD | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(id="content")
        yield Footer()

    async def on_mount(self) -> None:
        content = self.query_one("#content", Container)

        if not self.paths:
            content.mount(
                Label(
                    "No ISO files found.\n\n"
                    "Usage:\n"
                    "  dvd-ripper movie.iso\n"
                    "  dvd-ripper ~/dvd-isos/"
                )
            )
            return

        # v1: scan the first ISO.
        # We'll add a proper multi-disc queue next.
        try:
            dvd = scan_dvd(self.paths[0])
        except FFmpegError as exc:
            content.mount(
                Label(
                    f"[red]Could not scan DVD:[/red]\n\n{exc}"
                )
            )
            return

        self.current_dvd = dvd
        await content.mount(DVDPage(dvd))

    def selected_titles(self) -> list[Title]:
        if not self.current_dvd:
            return []

        result = []

        for row in self.query(TitleRow):
            checkbox = row.query_one(Checkbox)

            if checkbox.value:
                result.append(row.title)

        return result

    async def on_button_pressed(
        self,
        event: Button.Pressed,
    ) -> None:
        if event.button.id == "main-only":
            await self.select_main_only()

        elif event.button.id == "select-all":
            await self.select_all()

        elif event.button.id == "start":
            await self.start_encoding()

    async def select_main_only(self) -> None:
        if not self.current_dvd:
            return

        longest = max(
            self.current_dvd.titles,
            key=lambda t: t.duration,
        )

        for row in self.query(TitleRow):
            checkbox = row.query_one(Checkbox)
            checkbox.value = row.title.number == longest.number

    async def select_all(self) -> None:
        for row in self.query(TitleRow):
            row.query_one(Checkbox).value = True

    async def start_encoding(self) -> None:
        if not self.current_dvd:
            return

        selected = self.selected_titles()

        if not selected:
            self.notify(
                "Select at least one title.",
                severity="warning",
            )
            return

        iso = Path(self.current_dvd.path)

        # v1 output directory.
        output_dir = iso.with_suffix("")
        output_dir.mkdir(exist_ok=True)

        for title in selected:
            output = output_dir / (
                f"{iso.stem} - Title {title.number}.mp4"
            )

            try:
                command = build_encode_command(
                    iso,
                    title,
                    output,
                )
            except FFmpegError as exc:
                self.notify(
                    str(exc),
                    severity="error",
                )
                continue

            # For v1, don't actually run it yet.
            # Display the command so we can verify everything.
            command_text = " ".join(
                f'"{x}"' if " " in x else x
                for x in command
            )

            self.notify(
                f"Would run:\n{command_text}",
                timeout=10,
            )

def main() -> None:
    paths: list[Path] = []

    for argument in sys.argv[1:]:
        paths.extend(find_isos(Path(argument)))

    app = DVDRipperApp(paths)
    app.run()

if __name__ == "__main__":
    main()
```

 And:

 ## `dvd_ripper/__main__.py`

 dvd\_ripper/\_\_main\_\_.py

```
from .app import main

main()
```

 ## `dvd_ripper/__init__.py`

 This can simply be empty:

```

```

---

 # Running it

 With the virtual environment active:

```
dvd-ripper ~/dvds/movie.iso
```

 or:

```
dvd-ripper ~/dvds/
```

 At this point it should:

 1. find the ISO
2. invoke `ffprobe`
3. enumerate DVD titles
4. identify the longest title
5. inspect English audio
6. inspect English subtitles
7. present everything in a Textual interface
8. let you select titles
9. construct the eventual FFmpeg commands

 **It deliberately does not encode yet.**

 That's intentional. I'd want to verify the title/stream detection on a few real DVDs before letting it chew through a directory of ISOs.

---

 # The next thing I'd change

 The current UI is basically a proof of concept. The first real version should have a **two-level interface**:

```
┌─────────────────────────────────────────────────────┐
│ DVD Ripper                                           │
├─────────────────────────────────────────────────────┤
│                                                       │
│ ISO files                                             │
│                                                       │
│ ● Blade Runner.iso                                    │
│ ● Alien.iso                                           │
│ ● The Thing.iso                                       │
│                                                       │
│                    [ Configure DVD ]                  │
└─────────────────────────────────────────────────────┘
```

 Then:

```
┌─────────────────────────────────────────────────────┐
│ Blade Runner.iso                                     │
├─────────────────────────────────────────────────────┤
│                                                       │
│ ☑ 1   01:57:32   Main feature                        │
│ ☐ 2   00:03:12   Trailer                             │
│ ☐ 3   00:07:48   Featurette                          │
│ ☐ 4   00:01:21   Deleted scene                       │
│                                                       │
│ Audio:                                               │
│   ● English — AC3 5.1                               │
│   ○ English — AC3 2.0                               │
│   ○ French — AC3 5.1                                │
│                                                       │
│ Subtitles:                                           │
│   ☑ English                                          │
│                                                       │
│ Output: /movies/Blade Runner/                        │
│                                                       │
│ [ Encode ] [ Back ]                                  │
└─────────────────────────────────────────────────────┘
```

 And **then** I'd implement the actual FFmpeg execution with a worker thread, parsing FFmpeg's `-progress pipe:1` output so the TUI can show:

```
Encoding Blade Runner — Title 1

████████████████████████░░░░░░░░  67%

01:08:14 / 01:42:31
Speed: 2.14x
ETA: 16:07

Output: 1.84 GB
```

 That's preferable to scraping FFmpeg's human-readable stderr output.

 ### One other design change I'd make now

 I'd make the output naming configurable:

```
Blade Runner/
├── Blade Runner.mp4
├── Blade Runner - Trailer.mp4
├── Blade Runner - Featurette.mp4
└── Blade Runner.en.srt
```

 rather than the current deliberately dumb:

```
Blade Runner/
├── Blade Runner - Title 1.mp4
├── Blade Runner - Title 2.mp4
└── ...
```

 We can get there once we've got a reliable title-selection workflow.

 The **big remaining technical piece is DVD subtitle OCR**. I would keep that separate from the initial encoder because DVD subtitles are bitmap streams; getting a clean `.srt` requires OCR rather than simply remuxing a text subtitle stream.
