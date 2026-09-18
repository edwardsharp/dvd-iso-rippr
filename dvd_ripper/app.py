"""interactive dvd inspection and encoding, with one cancellable job at a time."""

from __future__ import annotations

import asyncio
import shlex
import tempfile
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, Footer, Input, ProgressBar, RichLog, Select, Static

from .encoder import Progress, available_output_path, encode_title, output_path
from .ffmpeg import FFmpegError, build_encode_command, choose_english_audio
from .models import DVD, EncodeSettings, Title
from .scanner import scan_dvd

SILENT = -1
EncodeItem = tuple[Title, Path, int | None]


class TitleRow(Vertical):
    def __init__(self, title: Title, *, main: bool) -> None:
        super().__init__(id=f"row-{title.number}", classes="title-row")
        self.title = title
        self.main = main

    def compose(self) -> ComposeResult:
        title = self.title
        label = f"title {title.number} · {title.duration_text}"
        if self.main:
            label += " · main candidate (longest-title heuristic)"
        yield Checkbox(Text(label), value=self.main, id=f"title-{title.number}", disabled=True)
        audio_options = [(Text("silent — no audio"), SILENT)]
        for stream in title.audio_streams:
            parts = [f"stream {stream.index}", stream.language or "unknown language"]
            parts.append(stream.codec_name or "unknown codec")
            if stream.channels is not None:
                parts.append(f"{stream.channels} channels")
            if stream.title:
                parts.append(stream.title)
            audio_options.append((Text(" · ".join(parts)), stream.index))
        preferred = choose_english_audio(title) or next(iter(title.audio_streams), None)
        yield Select(
            audio_options,
            value=preferred.index if preferred is not None else SILENT,
            allow_blank=False,
            id=f"audio-{title.number}",
            disabled=True,
        )
        if not title.audio_streams:
            yield Static("no audio tracks detected; silent selected automatically.", markup=False)
        video = title.video
        details = (
            "no video stream" if video is None else f"video: {video.codec_name or 'unknown codec'}"
        )
        if video is not None and video.width and video.height:
            details += f" · {video.width}×{video.height}"
        subtitles = ", ".join(
            f"{stream.index}: {stream.language or 'unknown language'}"
            for stream in title.subtitle_streams
        )
        details += f" | subtitles: {subtitles or 'none detected'} (unsupported)"
        yield Static(details, markup=False)


class DVDRipperApp(App[None]):
    """inspect provided discs and encode selected titles; command display is optional."""

    TITLE = "dvd iso ripper"
    BINDINGS = [("q", "quit", "quit"), ("ctrl+c", "quit", "quit")]
    CSS = """
    #content { height: 1fr; padding: 0 1; }
    .controls { height: auto; }
    #disc, #output-directory { width: 1fr; }
    #scan { width: 12; min-width: 8; }
    #use-output { width: 17; min-width: 12; }
    #output-hint { height: auto; color: $text-muted; }
    #stereo, #deinterlace { width: 1fr; }
    #title-actions, #command-actions { height: 3; align-vertical: middle; }
    #title-actions Static { width: 1fr; }
    #main, #all { width: 8; min-width: 8; margin-left: 1; }
    #preview-button { width: 25; min-width: 20; }
    #titles { height: auto; }
    .title-row { height: auto; padding: 0 1; margin-bottom: 1; }
    .title-row Checkbox { width: 1fr; }
    #preview { height: 6; border: round $primary; }
    #log { height: 8; border: round $secondary; }
    #error { height: auto; max-height: 10; overflow-y: auto; color: $error; border: round $error; }

    #status { height: auto; max-height: 3; padding: 0 1; }
    #progress { height: 1; padding: 0 1; }
    #encode { width: 1fr; height: 3; }
    """

    def __init__(
        self,
        paths: list[Path],
        *,
        output_dir: Path | None = None,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
    ) -> None:
        super().__init__()
        self.paths = [Path(path) for path in paths]
        self.output_dir = output_dir.expanduser().resolve() if output_dir is not None else None
        self._output_per_disc = output_dir is None
        self._confirmed_output_text = str(self.output_dir) if self.output_dir is not None else None
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self._disc_index = 0
        self._dvd: DVD | None = None
        self._job: asyncio.Task[None] | None = None
        self._job_name = ""
        self._cancelling = False
        self._ripper_closing = False

    def _suggested_output(self) -> str:
        if self.output_dir is not None:
            return str(self.output_dir)
        return str(self.paths[self._disc_index].expanduser().resolve().parent) if self.paths else ""

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="content"):
            with Horizontal(classes="controls"):
                yield Select(
                    [(Text(str(path)), index) for index, path in enumerate(self.paths)],
                    value=0 if self.paths else Select.BLANK,
                    allow_blank=not self.paths,
                    prompt="no discs provided",
                    id="disc",
                )
                yield Button("scan", id="scan")
            yield Static("output directory", markup=False)
            with Horizontal(classes="controls"):
                yield Input(
                    self._suggested_output(),
                    placeholder="choose an output directory",
                    id="output-directory",
                )
                yield Button("use directory", id="use-output")
            yield Static("", id="output-hint", markup=False)
            with Horizontal(classes="controls"):
                yield Checkbox("stereo aac (off: keep surround)", value=True, id="stereo")
                yield Select(
                    [
                        ("deinterlace: auto", "auto"),
                        ("deinterlace: on", "on"),
                        ("deinterlace: off", "off"),
                    ],
                    value="auto",
                    allow_blank=False,
                    id="deinterlace",
                )
            yield Static(
                "subtitles/ocr are unsupported; no subtitles will be output.", markup=False
            )
            with Horizontal(id="title-actions"):
                yield Static("titles", markup=False)
                yield Button("main", id="main")
                yield Button("all", id="all")
            with Vertical(id="titles"):
                yield Static("waiting for scan…", markup=False)
            with Horizontal(id="command-actions"):
                yield Button("show ffmpeg command", id="preview-button")
            yield RichLog(id="preview", wrap=True, markup=False, highlight=False)
            yield Static("", id="error", markup=False)
            yield RichLog(id="log", wrap=True, markup=False, highlight=False, max_lines=500)
        yield Static("ready", id="status", markup=False)
        yield ProgressBar(total=1, show_eta=False, id="progress")
        yield Button("encode", id="encode", variant="success")
        yield Footer()

    def on_mount(self) -> None:
        self._clear_error()
        self._invalidate_preview()
        self._update_output_hint()
        if self.paths:
            self._start_job("scan", self._scan)
        else:
            self.query_one("#titles", Vertical).query_one(Static).update("no discs provided.")
            self._set_status("no discs provided.")
            self._refresh_controls()

    def _set_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

    def _write_log(self, message: str) -> None:
        self.query_one("#log", RichLog).write(Text(message))

    def _clear_error(self) -> None:
        panel = self.query_one("#error", Static)
        panel.update("")
        panel.display = False

    def _report_error(self, message: str) -> None:
        self._set_status(message.splitlines()[0])
        self._write_log(message)
        panel = self.query_one("#error", Static)
        panel.update(message)
        panel.display = True
        panel.scroll_visible()

    def _update_output_hint(self) -> None:
        hint = (
            f"output confirmed: {self.output_dir} · existing files get numbered suffixes."
            if self.output_dir is not None
            else "choose an output directory and click use directory (or press enter); the default is beside this ISO."
        )
        self.query_one("#output-hint", Static).update(hint)

    def _refresh_controls(self) -> None:
        busy = self._job is not None or self._ripper_closing
        has_titles = self._dvd is not None and bool(self._dvd.titles)
        for control in self.query("Select, Checkbox, Input"):
            control.disabled = busy
        self.query_one("#disc", Select).disabled = busy or not self.paths
        self.query_one("#scan", Button).disabled = busy or not self.paths
        self.query_one("#use-output", Button).disabled = busy
        for name in ("main", "all", "preview-button"):
            self.query_one(f"#{name}", Button).disabled = busy or not has_titles
        button = self.query_one("#encode", Button)
        if self._job is not None:
            button.label = (
                "cancelling…"
                if self._cancelling
                else ("cancel encoding" if self._job_name == "encode" else "cancel scan")
            )
            button.variant = "warning"
            button.disabled = self._cancelling or self._ripper_closing
        else:
            button.label = "encode"
            button.variant = "success"
            # when titles exist, validation errors belong in the UI, not a dead button.
            button.disabled = self._ripper_closing or not has_titles

    def _invalidate_preview(self) -> None:
        self.query_one("#preview", RichLog).clear().write(
            "command display is optional; encode uses your current choices."
        )

    def _read_output_directory(self) -> Path:
        text = self.query_one("#output-directory", Input).value
        if not text.strip():
            raise ValueError("enter an output directory, then click use directory.")
        path = Path(text).expanduser().resolve()
        if path.exists() and not path.is_dir():
            raise ValueError(f"output is not a directory: {path}")
        return path

    @staticmethod
    def _prepare_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        # test actual write access without touching any existing output file.
        with tempfile.TemporaryFile(dir=path):
            pass

    def _confirm_output(self) -> None:
        try:
            path = self._read_output_directory()
            self._prepare_directory(path)
        except (ValueError, OSError, RuntimeError) as exc:
            self._report_error(f"cannot use output directory: {exc}")
            return
        self.output_dir = path
        self._confirmed_output_text = str(path)
        self.query_one("#output-directory", Input).value = str(path)
        self._clear_error()
        self._update_output_hint()
        self._invalidate_preview()
        self._set_status("output confirmed. review titles/audio, then encode.")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "output-directory" and event.value != self._confirmed_output_text:
            self.output_dir = None
            self._confirmed_output_text = None
            self._update_output_hint()
            self._invalidate_preview()
            self._clear_error()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "output-directory" and self._job is None:
            self._confirm_output()

    def _start_job(self, name: str, operation: Callable[[], Coroutine[Any, Any, None]]) -> None:
        if self._job is not None or self._ripper_closing:
            return
        self._clear_error()
        self._job_name = name
        self._cancelling = False
        self._job = asyncio.create_task(operation(), name=f"dvd-ripper-{name}")
        self._job.add_done_callback(self._job_finished)
        self.query_one("#progress", ProgressBar).update(total=None, progress=0)
        self._refresh_controls()

    def _job_finished(self, task: asyncio.Task[None]) -> None:
        error: Exception | None = None
        cancelled = task.cancelled() or self._cancelling
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            error = exc
        if task is not self._job:
            return
        self._job = None
        self._cancelling = False
        if self._ripper_closing:
            return
        if error is not None:
            self._report_error(f"{self._job_name} failed: {error}")
        elif cancelled:
            self._set_status("cancelled; completed files are kept.")
            self._write_log(f"{self._job_name} cancelled; completed files are kept.")
        if error is not None or cancelled:
            self.query_one("#progress", ProgressBar).update(total=1, progress=0)
        self._refresh_controls()
        if self._job_name == "scan" and self._dvd and self.output_dir is None:
            self.query_one("#output-directory", Input).focus()

    async def _scan(self) -> None:
        path = self.paths[self._disc_index]
        self._dvd = None
        self._invalidate_preview()
        titles = self.query_one("#titles", Vertical)
        await titles.remove_children()
        self._set_status(f"scanning {path}…")
        task = asyncio.current_task()

        def report(message: str) -> None:
            if self._job is task and not self._cancelling and not self._ripper_closing:
                self._set_status(message)
                self._write_log(message)

        dvd = await scan_dvd(path, ffprobe=self.ffprobe, on_progress=report)
        if self._cancelling or self._ripper_closing:
            return
        self._dvd = dvd
        if dvd.titles:
            await titles.mount(
                *(TitleRow(title, main=title == dvd.main_title) for title in dvd.titles)
            )
        else:
            await titles.mount(Static("no playable titles found.", markup=False))
        for warning in dvd.warnings:
            self._write_log(f"warning: {warning}")
        next_step = (
            "review titles/audio, then encode."
            if self.output_dir is not None
            else "choose an output directory, then click use directory."
        )
        self._set_status(f"found {len(dvd.titles)} title(s). {next_step}")
        self._write_log(f"scan complete: {len(dvd.titles)} title(s).")
        self.query_one("#progress", ProgressBar).update(total=1, progress=0)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "disc":
            if event.value is Select.BLANK or event.value == self._disc_index:
                return
            if self._job is not None or self._ripper_closing:
                event.select.value = self._disc_index
                return
            self._disc_index = int(event.value)
            if self._output_per_disc:
                self.output_dir = None
                self._confirmed_output_text = None
                self.query_one("#output-directory", Input).value = self._suggested_output()
                self._update_output_hint()
            self._start_job("scan", self._scan)
        else:
            self._invalidate_preview()
            self._clear_error()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        self._invalidate_preview()
        self._clear_error()

    def _configuration(self) -> tuple[tuple[EncodeItem, ...], EncodeSettings]:
        if self._dvd is None:
            raise ValueError("scan a disc first.")
        root = self._read_output_directory()
        if self.output_dir is None or root != self.output_dir:
            self.query_one("#output-directory", Input).focus()
            raise ValueError(
                "confirm your output directory with use directory (or press enter) before encoding."
            )
        settings = EncodeSettings(
            stereo=self.query_one("#stereo", Checkbox).value,
            deinterlace=str(self.query_one("#deinterlace", Select).value),
        )
        items: list[EncodeItem] = []
        for title in self._dvd.titles:
            if not self.query_one(f"#title-{title.number}", Checkbox).value:
                continue
            audio = self.query_one(f"#audio-{title.number}", Select).value
            if audio is Select.BLANK:
                raise ValueError(f"title {title.number}: choose an audio stream or silent.")
            items.append(
                (
                    title,
                    output_path(self._dvd.path, title, root, unique=False),
                    None if audio == SILENT else int(audio),
                )
            )
        if not items:
            raise ValueError("select at least one title, or click main/all above the titles.")
        return tuple(items), settings

    def _commands(self, plan: tuple[EncodeItem, ...], settings: EncodeSettings) -> list[list[str]]:
        assert self._dvd is not None
        return [
            build_encode_command(
                self._dvd.path,
                title,
                available_output_path(output),
                audio_index=audio,
                settings=settings,
                ffmpeg=self.ffmpeg,
            )
            for title, output, audio in plan
        ]

    def _show_commands(self, commands: list[list[str]]) -> None:
        preview = self.query_one("#preview", RichLog).clear()
        preview.write("filenames shown are estimates; collisions get numbered at completion.")
        for command in commands:
            preview.write(Text(shlex.join(command)))

    def _preview(self) -> None:
        self._clear_error()
        try:
            plan, settings = self._configuration()
            self._show_commands(self._commands(plan, settings))
        except (ValueError, FFmpegError, OSError, RuntimeError) as exc:
            self._report_error(str(exc))
            return
        self._set_status(
            "ffmpeg command shown. encode starts conversion; no subtitles are included."
        )
        self.query_one("#preview", RichLog).scroll_visible()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        name = event.button.id
        if name == "encode" and self._job is not None:
            self._request_cancel()
            return
        if self._job is not None or self._ripper_closing:
            return
        if name == "scan" and self.paths:
            self._start_job("scan", self._scan)
        elif name == "use-output":
            self._confirm_output()
        elif name in ("main", "all") and self._dvd is not None:
            for title in self._dvd.titles:
                self.query_one(f"#title-{title.number}", Checkbox).value = (
                    name == "all" or title == self._dvd.main_title
                )
            self._invalidate_preview()
        elif name == "preview-button":
            self._preview()
        elif name == "encode":
            try:
                plan, settings = self._configuration()
                commands = self._commands(plan, settings)
                assert self._dvd is not None and self.output_dir is not None
                self._prepare_directory(self.output_dir)
            except (ValueError, FFmpegError, OSError, RuntimeError) as exc:
                self._report_error(f"cannot start encoding: {exc}")
                return
            self._show_commands(commands)
            iso = self._dvd.path
            self._start_job("encode", lambda: self._encode(iso, plan, settings))

    async def _encode(
        self, iso: Path, plan: tuple[EncodeItem, ...], settings: EncodeSettings
    ) -> None:
        task = asyncio.current_task()
        for index, (title, output, audio) in enumerate(plan, 1):
            self._set_status(f"encoding title {title.number} ({index}/{len(plan)})…")
            self._write_log(f"encoding title {title.number} → {available_output_path(output)}")
            duration = title.duration if title.duration is not None and title.duration > 0 else None
            self.query_one("#progress", ProgressBar).update(total=duration, progress=0)

            def report(progress: Progress) -> None:
                if self._job is not task or self._cancelling or self._ripper_closing:
                    return
                seconds = max(0, progress.seconds)
                self.query_one("#progress", ProgressBar).update(
                    progress=min(seconds, duration) if duration else seconds
                )
                self._set_status(
                    f"title {title.number} ({index}/{len(plan)}): {seconds:.1f}s · {progress.speed}"
                )

            published = await encode_title(
                iso,
                title,
                output,
                audio_index=audio,
                settings=settings,
                ffmpeg=self.ffmpeg,
                on_progress=report,
            )
            if self._cancelling or self._ripper_closing:
                return
            self._write_log(f"completed: {published}")
        self.query_one("#progress", ProgressBar).update(total=1, progress=1)
        self._set_status(f"completed {len(plan)} title(s). see the log for output paths.")

    def _request_cancel(self) -> None:
        if self._job is not None and not self._cancelling:
            # never cancel twice: the backend may be awaiting subprocess cleanup.
            self._cancelling = True
            self._job.cancel()
            self._set_status("cancelling; waiting for subprocess cleanup…")
            self._refresh_controls()

    async def _stop_job(self) -> None:
        self._ripper_closing = True
        task = self._job
        if task is not None:
            # unmount may run after child widgets are gone; cleanup must not touch the UI.
            if not self._cancelling:
                self._cancelling = True
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def action_quit(self) -> None:
        self._request_cancel()
        await self._stop_job()
        self.exit()

    async def on_unmount(self) -> None:
        await self._stop_job()
