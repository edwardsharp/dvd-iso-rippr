"""headless ui tests: scanning and encoding are always mocked."""

import asyncio
import shlex
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from rich.text import Text
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import Button, Checkbox, Input, Log, ProgressBar, Select, Static

import dvd_ripper.app as app_module
from dvd_ripper.app import SILENT, DVDRipperApp
from dvd_ripper.encoder import Progress, output_path
from dvd_ripper.ffmpeg import FFmpegError, FFmpegUnavailableError, build_encode_command
from dvd_ripper.models import DVD, EncodeSettings, Stream, Title

pytestmark = pytest.mark.asyncio


@pytest.fixture
def backend(monkeypatch, tmp_path):
    path = tmp_path / "[red]disc's movie.iso"
    video = Stream(0, "video", "mpeg2video", width=720, height=480)
    titles = (
        Title(
            1,
            120,
            (
                video,
                Stream(1, "audio", "ac3", "fra", channels=2),
                Stream(4, "audio", "ac3", "spa", channels=6),
            ),
        ),
        Title(
            2,
            7200,
            (
                video,
                Stream(4, "audio", "ac3", "fra", channels=8),
                Stream(1, "audio", "ac3", "eng", channels=2),
                Stream(
                    2,
                    "audio",
                    "ac3",
                    "eng",
                    title="[italic]director's track[/italic]",
                    channels=6,
                ),
                Stream(3, "subtitle", "dvd_subtitle", "eng"),
            ),
        ),
        Title(
            3,
            None,
            (
                video,
                Stream(1, "audio", "ac3", channels=2),
                Stream(4, "audio", "ac3", "deu", channels=8),
            ),
        ),
    )
    dvd = DVD(path, titles, ("some scan warning [bold]literal[/bold]",))

    async def publish(iso, title, output, **kwargs):
        return output

    scan = AsyncMock(return_value=dvd)
    encode = AsyncMock(side_effect=publish)
    build = Mock(wraps=app_module.build_encode_command)
    monkeypatch.setattr(app_module, "scan_dvd", scan)
    monkeypatch.setattr(app_module, "encode_title", encode)
    monkeypatch.setattr(app_module, "build_encode_command", build)
    return dvd, scan, encode, build


async def idle(app, pilot):
    async with asyncio.timeout(5):
        while app._job is not None:
            await asyncio.sleep(0.01)
        await pilot.pause()


async def click(app, pilot, selector):
    control = app.query_one(selector)
    control.scroll_visible(animate=False)
    await pilot.pause()
    # the active-effect timer can suppress repeated clicks even after pilot.pause().
    async with asyncio.timeout(2):
        while control.has_class("-active"):
            await asyncio.sleep(0.01)
    assert await pilot.click(selector)
    await pilot.pause()


async def confirm_with_enter(app, pilot):
    field = app.query_one("#output-directory", Input)
    field.scroll_visible(animate=False)
    field.focus()
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


def selected(app):
    return [
        title.number
        for title in app._dvd.titles
        if app.query_one(f"#title-{title.number}", Checkbox).value
    ]


def status(app):
    return str(app.query_one("#status", Static).render())


def log_text(app, selector="#log"):
    return "\n".join(app.query_one(selector, Log).lines)


def assert_logged(app, message):
    assert message in log_text(app)


def assert_error(app, message):
    panel = app.query_one("#error", Static)
    assert panel.display
    assert str(panel.render()) == message
    assert status(app) == message.splitlines()[0]
    assert_logged(app, message)


def assert_commands_cleared(app):
    preview = log_text(app, "#preview")
    assert "command display is optional" in preview
    assert "-map" not in preview
    assert not list(app.query("#copy-command, #copy-log"))


def assert_action(app, label="encode", *, disabled=False):
    buttons = list(app.query("#encode"))
    assert len(buttons) == 1
    assert not list(app.query("#cancel"))
    assert str(buttons[0].label) == label
    assert buttons[0].disabled is disabled


def assert_busy_controls(app):
    for control in app.query("Select, Checkbox, Input"):
        assert control.disabled, control.id
    for selector in ("#scan", "#use-output", "#main", "#all", "#preview-button"):
        assert app.query_one(selector, Button).disabled, selector


def command_maps(command):
    return [command[index + 1] for index, argument in enumerate(command) if argument == "-map"]


async def test_mount_defaults_main_english_stereo_and_literal_labels(backend):
    dvd, scan, encode, build = backend
    app = DVDRipperApp([dvd.path], output_dir=dvd.path.parent, ffprobe="custom-ffprobe")
    async with app.run_test(size=(100, 40)) as pilot:
        await idle(app, pilot)
        scan.assert_awaited_once()
        assert scan.call_args.args == (dvd.path,)
        assert scan.call_args.kwargs["ffprobe"] == "custom-ffprobe"
        assert selected(app) == [2]
        for number, expected in ((1, 1), (2, 2), (3, 1)):
            audio = app.query_one(f"#audio-{number}", Select)
            assert audio.value == expected
            assert audio._allow_blank is False
            assert Select.BLANK not in [value for _, value in audio._options]
            assert SILENT in [value for _, value in audio._options]
        assert app.query_one("#stereo", Checkbox).value is True
        assert app.query_one("#deinterlace", Select).value == "auto"
        assert "heuristic" in str(app.query_one("#title-2", Checkbox).label)
        assert_action(app)
        assert_commands_cleared(app)
        assert str(app.query_one("#preview-button", Button).label) == "show ffmpeg command"
        # rich text labels keep filenames and track names literal, not markup.
        disc_label = app.query_one("#disc", Select)._options[-1][0]
        assert isinstance(disc_label, Text)
        assert disc_label.plain == str(dvd.path)
        assert not disc_label.spans
        audio_label = next(
            label for label, value in app.query_one("#audio-2", Select)._options if value == 2
        )
        assert isinstance(audio_label, Text)
        assert "[italic]director's track[/italic]" in audio_label.plain
        assert not audio_label.spans
        assert_logged(app, "some scan warning [bold]literal[/bold]")
        build.assert_not_called()
        encode.assert_not_awaited()


async def test_all_main_and_no_selected_titles(backend):
    dvd, _, encode, build = backend
    app = DVDRipperApp([dvd.path], output_dir=dvd.path.parent)
    async with app.run_test(size=(100, 40)) as pilot:
        await idle(app, pilot)
        await click(app, pilot, "#all")
        assert selected(app) == [1, 2, 3]
        await click(app, pilot, "#main")
        assert selected(app) == [2]
        app.query_one("#title-2", Checkbox).value = False
        await pilot.pause()
        assert_action(app)
        message = "select at least one title, or click main/all above the titles."
        await click(app, pilot, "#preview-button")
        assert_error(app, message)
        await click(app, pilot, "#encode")
        assert_error(app, f"cannot start encoding: {message}")
        assert_action(app)
        build.assert_not_called()
        encode.assert_not_awaited()


@pytest.mark.parametrize("number", [1, 3])
async def test_non_english_and_unknown_audio_use_first_available_or_explicit_silent(
    backend, number
):
    dvd, _, encode, build = backend
    app = DVDRipperApp([dvd.path], output_dir=dvd.path.parent)
    async with app.run_test(size=(100, 40)) as pilot:
        await idle(app, pilot)
        app.query_one("#title-2", Checkbox).value = False
        app.query_one(f"#title-{number}", Checkbox).value = True
        await pilot.pause()
        assert app.query_one(f"#audio-{number}", Select).value == 1
        await click(app, pilot, "#encode")
        await idle(app, pilot)
        encode.assert_awaited_once()
        assert encode.call_args.args[1].number == number
        assert encode.call_args.kwargs["audio_index"] == 1
        assert build.call_args.kwargs["audio_index"] == 1
        app.query_one(f"#audio-{number}", Select).value = SILENT
        await pilot.pause()
        assert_action(app)
        assert_commands_cleared(app)
        await click(app, pilot, "#encode")
        await idle(app, pilot)
        assert encode.await_count == 2
        assert encode.call_args.kwargs["audio_index"] is None
        assert build.call_args.kwargs["audio_index"] is None
        assert "completed 1 title(s)" in status(app)


async def test_no_audio_encodes_silently_with_one_click_and_no_preview(backend):
    dvd, scan, encode, build = backend
    title = Title(8, 10, (dvd.titles[0].video,))
    scan.return_value = DVD(dvd.path, (title,))
    app = DVDRipperApp([dvd.path], output_dir=dvd.path.parent)
    async with app.run_test(size=(100, 40)) as pilot:
        await idle(app, pilot)
        audio = app.query_one("#audio-8", Select)
        assert audio.value == SILENT
        assert audio._allow_blank is False
        assert [value for _, value in audio._options] == [SILENT]
        assert selected(app) == [8]
        assert_action(app)
        build.assert_not_called()
        await click(app, pilot, "#encode")
        await idle(app, pilot)
        encode.assert_awaited_once()
        assert encode.call_args.args == (
            dvd.path,
            title,
            output_path(dvd.path, title, dvd.path.parent, unique=False),
        )
        assert encode.call_args.kwargs["audio_index"] is None
        assert encode.call_args.kwargs["settings"] == EncodeSettings()
        build.assert_called_once()
        command = build_encode_command(*build.call_args.args, **build.call_args.kwargs)
        assert "-an" in command
        assert "-sn" in command
        assert command_maps(command) == ["0:0"]
        assert "completed 1 title(s)" in status(app)
        assert_action(app)


async def test_preview_is_shell_quoted_literal_and_never_encodes(backend, tmp_path, monkeypatch):
    dvd, _, encode, build = backend
    output_dir = tmp_path / "output [blue] films"
    app = DVDRipperApp([dvd.path], output_dir=output_dir, ffmpeg="custom ffmpeg")
    async with app.run_test(size=(100, 40)) as pilot:
        await idle(app, pilot)
        preview = app.query_one("#preview", Log)
        write = Mock(wraps=preview.write_line)
        monkeypatch.setattr(preview, "write_line", write)
        await click(app, pilot, "#preview-button")
        encode.assert_not_awaited()
        assert_action(app)
        build.assert_called_once()
        title = dvd.titles[1]
        output = output_path(dvd.path, title, output_dir, unique=False)
        command = build_encode_command(
            dvd.path,
            title,
            output,
            audio_index=2,
            settings=EncodeSettings(),
            ffmpeg="custom ffmpeg",
        )
        rendered = write.call_args.args[0]
        assert isinstance(rendered, str)
        assert rendered == shlex.join(command)
        assert shlex.split(rendered) == command
        assert "[red]" in rendered
        assert build.call_args.args == (dvd.path, title, output)
        assert build.call_args.kwargs["ffmpeg"] == "custom ffmpeg"
        assert "-sn" in command and "-dn" in command
        assert "-c:s" not in command
        assert command_maps(command) == ["0:0", "0:2"]
        assert not output_dir.exists()


@pytest.mark.parametrize("selector", ["#preview", "#log"])
async def test_scrolled_log_selection_exposes_partial_text(backend, selector):
    dvd, _, _, _ = backend
    app = DVDRipperApp([dvd.path], output_dir=dvd.path.parent)
    async with app.run_test(size=(100, 40)) as pilot:
        await idle(app, pilot)
        log = app.query_one(selector, Log)
        lines = [
            f"line {index}: [bold]café's quoted text[/bold] " + "x" * 200 for index in range(15)
        ]
        log.clear().write_lines(lines)
        log.scroll_visible(animate=False)
        log.focus()
        await pilot.pause()
        log.scroll_to(x=8, y=5, animate=False, immediate=True)
        await pilot.pause()
        assert log.scroll_offset == Offset(8, 5)
        strip = log.render_line(1)
        assert next(iter(strip)).style.meta["offset"] == (8, 6)
        assert strip.text.startswith(lines[6][8:20])
        selection = Selection.from_offsets(Offset(12, 6), Offset(27, 7))
        expected = lines[6][12:] + "\n" + lines[7][:27]
        assert log.get_selection(selection) == (expected, "\n")
        app.screen.selections = {log: selection}
        await pilot.pause()
        assert app.screen.get_selected_text() == expected
        assert app.is_running
        assert not app._ripper_closing
        assert_action(app)


@pytest.mark.parametrize("quit_key", ["ctrl+c", "q"])
@pytest.mark.parametrize("focused", ["#disc", "#audio-2", "#log", "#encode"])
async def test_quit_shortcuts_take_priority_over_focused_controls(backend, quit_key, focused):
    dvd, _, _, _ = backend
    app = DVDRipperApp([dvd.path], output_dir=dvd.path.parent)
    async with app.run_test(size=(100, 40)) as pilot:
        await idle(app, pilot)
        app.query_one(focused).focus()
        await pilot.pause()
        assert app.focused is app.query_one(focused)
        await pilot.press(quit_key)
        assert app._ripper_closing
        assert app._job is None


@pytest.mark.parametrize("quit_key", ["ctrl+c", "q"])
async def test_quit_shortcuts_work_with_open_dropdown(backend, quit_key):
    dvd, _, _, _ = backend
    app = DVDRipperApp([dvd.path], output_dir=dvd.path.parent)
    async with app.run_test(size=(100, 40)) as pilot:
        await idle(app, pilot)
        select = app.query_one("#audio-2", Select)
        select.scroll_visible(animate=False)
        select.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert select.expanded
        await pilot.press(quit_key)
        assert app._ripper_closing
        assert app._job is None


async def test_q_remains_typeable_in_output_path_but_quits_after_leaving_field(backend):
    dvd, _, _, _ = backend
    app = DVDRipperApp([dvd.path], output_dir=dvd.path.parent)
    async with app.run_test(size=(100, 40)) as pilot:
        await idle(app, pilot)
        field = app.query_one("#output-directory", Input)
        field.focus()
        await pilot.press("end")
        before = field.value
        assert app.check_action("quit_shortcut", ()) is False
        await pilot.press("q")
        assert field.value == before + "q"
        assert app.is_running and not app._ripper_closing
        app.query_one("#use-output", Button).focus()
        await pilot.pause()
        assert app.check_action("quit_shortcut", ()) is True
        await pilot.press("q")
        assert app._ripper_closing


async def test_ctrl_c_quits_from_automatically_focused_output_prompt(backend):
    dvd, _, _, _ = backend
    app = DVDRipperApp([dvd.path])
    async with app.run_test(size=(100, 40)) as pilot:
        await idle(app, pilot)
        assert app.focused is app.query_one("#output-directory", Input)
        await pilot.press("ctrl+c")
        assert app._ripper_closing
        assert app._job is None


async def test_ctrl_c_quits_with_input_selection_instead_of_copying(backend, monkeypatch):
    dvd, _, _, _ = backend
    app = DVDRipperApp([dvd.path], output_dir=dvd.path.parent)
    copy = Mock(wraps=app.copy_to_clipboard)
    monkeypatch.setattr(app, "copy_to_clipboard", copy)
    async with app.run_test(size=(100, 40)) as pilot:
        await idle(app, pilot)
        field = app.query_one("#output-directory", Input)
        field.scroll_visible(animate=False)
        field.focus()
        await pilot.pause()
        await pilot.press("home", "shift+end", "ctrl+c")
        copy.assert_not_called()
        assert app._ripper_closing
        assert app._job is None


async def test_command_selection_is_complete_literal_multiline_and_invalidates(backend):
    dvd, _, encode, build = backend
    app = DVDRipperApp(
        [dvd.path], output_dir=dvd.path.parent, ffmpeg="café's [red]tools/" + "x" * 160
    )
    async with app.run_test() as pilot:
        await idle(app, pilot)
        await click(app, pilot, "#all")
        await click(app, pilot, "#preview-button")
        commands = [
            build_encode_command(*call.args, **call.kwargs) for call in build.call_args_list
        ]
        expected = "\n".join(shlex.join(command) for command in commands)
        assert len(commands) == 3
        preview = app.query_one("#preview", Log)
        assert preview.lines[1:] == expected.splitlines()
        assert all(len(line) > preview.size.width for line in preview.lines[1:])
        preview.focus()
        app.screen.selections = {
            preview: Selection.from_offsets(
                Offset(0, 1), Offset(len(preview.lines[-1]), len(preview.lines) - 1)
            )
        }
        assert app.screen.get_selected_text() == expected
        assert "filenames shown are estimates" not in expected
        assert "café" in expected and "[red]" in expected
        assert [shlex.split(line) for line in expected.splitlines()] == commands
        app.screen.selections = {}
        app.query_one("#output-directory", Input).value += " changed"
        await pilot.pause()
        assert_commands_cleared(app)
        assert not list(app.query("#copy-command, #copy-log"))
        encode.assert_not_awaited()


async def test_log_selection_preserves_full_bounded_lines(backend):
    dvd, _, _, _ = backend
    app = DVDRipperApp([dvd.path], output_dir=dvd.path.parent)
    async with app.run_test() as pilot:
        await idle(app, pilot)
        log = app.query_one("#log", Log).clear()
        lines = [f"line {index}: café's [bold]literal[/bold] " + "x" * 180 for index in range(505)]
        app._write_log("\n".join(lines))
        await pilot.pause()
        assert log.max_lines == 500
        assert log.lines == lines[-500:]
        log.focus()
        app.screen.selections = {
            log: Selection.from_offsets(
                Offset(0, 0), Offset(len(log.lines[-1]), len(log.lines) - 1)
            )
        }
        expected = "\n".join(lines[-500:])
        assert app.screen.get_selected_text() == expected
        assert log.lines == lines[-500:]


async def test_encode_uses_live_titles_audio_and_settings_without_reshowing_commands(backend):
    dvd, _, encode, build = backend
    app = DVDRipperApp([dvd.path], output_dir=dvd.path.parent)
    async with app.run_test(size=(100, 40)) as pilot:
        await idle(app, pilot)
        await click(app, pilot, "#preview-button")
        assert build.call_args.args[1] == dvd.titles[1]
        assert build.call_args.kwargs["audio_index"] == 2
        assert build.call_args.kwargs["settings"] == EncodeSettings()
        build.reset_mock()
        app.query_one("#title-2", Checkbox).value = False
        app.query_one("#title-1", Checkbox).value = True
        app.query_one("#title-3", Checkbox).value = True
        app.query_one("#audio-1", Select).value = SILENT
        app.query_one("#audio-3", Select).value = 4
        app.query_one("#stereo", Checkbox).value = False
        app.query_one("#deinterlace", Select).value = "on"
        await pilot.pause()
        assert_commands_cleared(app)
        assert_action(app)
        await click(app, pilot, "#encode")
        await idle(app, pilot)
        assert [call.args[1].number for call in encode.call_args_list] == [1, 3]
        assert [call.kwargs["audio_index"] for call in encode.call_args_list] == [None, 4]
        assert [call.args[1].number for call in build.call_args_list] == [1, 3]
        for call in encode.call_args_list + build.call_args_list:
            assert call.kwargs["settings"] == EncodeSettings(stereo=False, deinterlace="on")
        assert "completed 2 title(s)" in status(app)
        assert_action(app)


@pytest.mark.parametrize("confirmation", ["button", "enter"])
@pytest.mark.parametrize("new_directory", [False, True])
async def test_output_prompt_blocks_encode_until_default_or_new_directory_confirmed(
    backend, tmp_path, monkeypatch, confirmation, new_directory
):
    dvd, _, encode, build = backend
    monkeypatch.chdir(tmp_path)
    app = DVDRipperApp([Path(dvd.path.name)])
    assert app.output_dir is None
    async with app.run_test(size=(100, 40)) as pilot:
        await idle(app, pilot)
        field = app.query_one("#output-directory", Input)
        assert field.value == str(dvd.path.resolve().parent)
        assert app.output_dir is None
        assert_action(app)
        target = dvd.path.parent
        if new_directory:
            target = tmp_path / "new [blue] output"
            field.value = str(tmp_path / "unused" / ".." / target.name)
            await pilot.pause()
        await click(app, pilot, "#encode")
        assert_error(
            app,
            "cannot start encoding: confirm your output directory with use directory "
            "(or press enter) before encoding.",
        )
        assert app.output_dir is None
        assert app._job is None
        encode.assert_not_awaited()
        build.assert_not_called()
        if confirmation == "enter":
            await confirm_with_enter(app, pilot)
        else:
            await click(app, pilot, "#use-output")
        assert app.output_dir == target.resolve()
        assert field.value == str(target.resolve())
        assert target.is_dir()
        assert str(app.query_one("#error", Static).render()) == ""
        assert "output confirmed" in str(app.query_one("#output-hint", Static).render())
        await click(app, pilot, "#encode")
        await idle(app, pilot)
        encode.assert_awaited_once()
        assert encode.call_args.args[2].parent == target.resolve() / dvd.path.stem
        assert "completed 1 title(s)" in status(app)


@pytest.mark.parametrize("supplied_output", [False, True])
async def test_changing_output_invalidates_confirmation_and_current_commands(
    backend, tmp_path, supplied_output
):
    dvd, _, encode, build = backend
    app = DVDRipperApp([dvd.path], output_dir=dvd.path.parent if supplied_output else None)
    async with app.run_test(size=(100, 40)) as pilot:
        await idle(app, pilot)
        if not supplied_output:
            await click(app, pilot, "#use-output")
        await click(app, pilot, "#preview-button")
        assert "-map" in log_text(app, "#preview")
        build.reset_mock()
        target = tmp_path / "changed output"
        app.query_one("#output-directory", Input).value = str(target)
        await pilot.pause()
        assert app.output_dir is None
        assert_commands_cleared(app)
        await click(app, pilot, "#encode")
        assert "confirm your output directory" in status(app)
        encode.assert_not_awaited()
        build.assert_not_called()
        await confirm_with_enter(app, pilot)
        assert app.output_dir == target.resolve()
        await click(app, pilot, "#encode")
        await idle(app, pilot)
        encode.assert_awaited_once()
        assert encode.call_args.args[2].parent == target / dvd.path.stem


@pytest.mark.parametrize("invalid", ["blank", "file"])
async def test_invalid_output_directory_is_visible_and_never_encodes(backend, tmp_path, invalid):
    dvd, _, encode, build = backend
    existing = tmp_path / "[red]not a directory"
    existing.write_text("keep me")
    value = "   " if invalid == "blank" else str(existing)
    detail = (
        "enter an output directory, then click use directory."
        if invalid == "blank"
        else f"output is not a directory: {existing}"
    )
    app = DVDRipperApp([dvd.path])
    async with app.run_test(size=(100, 40)) as pilot:
        await idle(app, pilot)
        app.query_one("#output-directory", Input).value = value
        await pilot.pause()
        await click(app, pilot, "#use-output")
        assert_error(app, f"cannot use output directory: {detail}")
        assert app.output_dir is None
        await click(app, pilot, "#encode")
        assert_error(app, f"cannot start encoding: {detail}")
        assert app._job is None
        assert_action(app)
        encode.assert_not_awaited()
        build.assert_not_called()
        assert existing.read_text() == "keep me"


@pytest.mark.parametrize("not_directory", [False, True])
async def test_supplied_output_resolves_skips_confirmation_and_validates_on_encode(
    backend, tmp_path, monkeypatch, not_directory
):
    dvd, _, encode, build = backend
    monkeypatch.chdir(tmp_path)
    supplied = Path("unused") / ".." / "chosen output"
    resolved = supplied.resolve()
    if not_directory:
        resolved.write_text("keep me")
    app = DVDRipperApp([dvd.path], output_dir=supplied)
    assert app.output_dir == resolved
    async with app.run_test(size=(100, 40)) as pilot:
        await idle(app, pilot)
        assert app.output_dir == resolved
        assert app.query_one("#output-directory", Input).value == str(resolved)
        if not not_directory:
            assert not resolved.exists()
        await click(app, pilot, "#encode")
        await idle(app, pilot)
        if not_directory:
            assert_error(app, f"cannot start encoding: output is not a directory: {resolved}")
            encode.assert_not_awaited()
            build.assert_not_called()
            assert resolved.read_text() == "keep me"
        else:
            encode.assert_awaited_once()
            assert resolved.is_dir()
            assert encode.call_args.args[2].parent == resolved / dvd.path.stem
            assert "completed 1 title(s)" in status(app)


@pytest.mark.parametrize("failure", ["create", "permission"])
@pytest.mark.parametrize("supplied_output", [False, True])
async def test_output_directory_create_and_write_failures_are_visible_and_block_encode(
    backend, tmp_path, monkeypatch, failure, supplied_output
):
    dvd, _, encode, _ = backend
    target = tmp_path / "unwritable output"
    detail = (
        f"{failure} failed [red]literal[/red]\ncheck destination permissions [bold]details[/bold]"
    )
    error = OSError(detail) if failure == "create" else PermissionError(detail)
    fail = Mock(side_effect=error)
    if failure == "create":
        mkdir = Path.mkdir

        def guarded_mkdir(path, *args, **kwargs):
            if path == target:
                return fail(path, *args, **kwargs)
            return mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", guarded_mkdir)
    else:
        temporary_file = app_module.tempfile.TemporaryFile

        def guarded_temporary_file(*args, **kwargs):
            if kwargs.get("dir") == target:
                return fail(*args, **kwargs)
            return temporary_file(*args, **kwargs)

        monkeypatch.setattr(app_module.tempfile, "TemporaryFile", guarded_temporary_file)
    app = DVDRipperApp([dvd.path], output_dir=target if supplied_output else None)
    async with app.run_test(size=(100, 40)) as pilot:
        await idle(app, pilot)
        fail.assert_not_called()
        if supplied_output:
            assert app.output_dir == target
            await click(app, pilot, "#encode")
            assert_error(app, f"cannot start encoding: {detail}")
        else:
            app.query_one("#output-directory", Input).value = str(target)
            await pilot.pause()
            await click(app, pilot, "#use-output")
            assert_error(app, f"cannot use output directory: {detail}")
            assert app.output_dir is None
            await click(app, pilot, "#encode")
            assert "confirm your output directory" in status(app)
        fail.assert_called_once()
        encode.assert_not_awaited()
        assert app._job is None
        assert_action(app)


@pytest.mark.parametrize("supplied_output", [False, True])
async def test_switch_discs_rescans_clears_commands_and_handles_output_confirmation(
    backend, tmp_path, supplied_output
):
    dvd, scan, encode, build = backend
    second = tmp_path / "other disc" / "second.iso"
    second.parent.mkdir()
    scan.side_effect = [dvd, DVD(second, (dvd.titles[0],))]
    chosen = tmp_path / "chosen output"
    app = DVDRipperApp([dvd.path, second], output_dir=chosen if supplied_output else None)
    async with app.run_test(size=(100, 40)) as pilot:
        await idle(app, pilot)
        if not supplied_output:
            app.query_one("#output-directory", Input).value = str(chosen)
            await pilot.pause()
            await click(app, pilot, "#use-output")
        await click(app, pilot, "#preview-button")
        assert app.output_dir == chosen
        build.reset_mock()
        app.query_one("#disc", Select).value = 1
        await pilot.pause()
        await idle(app, pilot)
        assert scan.await_count == 2
        assert scan.call_args.args == (second,)
        assert app._dvd.path == second
        assert selected(app) == [1]
        assert not list(app.query("#title-2"))
        assert_commands_cleared(app)
        assert_action(app)
        field = app.query_one("#output-directory", Input)
        if supplied_output:
            assert app.output_dir == chosen
            assert field.value == str(chosen)
            expected_root = chosen
        else:
            assert app.output_dir is None
            assert field.value == str(second.resolve().parent)
            await click(app, pilot, "#encode")
            assert "confirm your output directory" in status(app)
            encode.assert_not_awaited()
            build.assert_not_called()
            await confirm_with_enter(app, pilot)
            expected_root = second.parent
        await click(app, pilot, "#encode")
        await idle(app, pilot)
        encode.assert_awaited_once()
        assert encode.call_args.args == (
            second,
            dvd.titles[0],
            output_path(second, dvd.titles[0], expected_root, unique=False),
        )


async def test_collision_preview_is_estimate_backend_receives_base_and_actual_path_is_logged(
    backend, tmp_path, monkeypatch
):
    dvd, _, encode, build = backend
    title = dvd.titles[1]
    base = output_path(dvd.path, title, tmp_path, unique=False)
    estimate = base.with_stem(f"{base.stem} (1)")
    published = base.with_stem(f"{base.stem} (2)")
    base.parent.mkdir()
    base.write_bytes(b"existing output")
    paths = Mock(wraps=app_module.output_path)
    available = Mock(wraps=app_module.available_output_path)
    monkeypatch.setattr(app_module, "output_path", paths)
    monkeypatch.setattr(app_module, "available_output_path", available)

    async def racing_encode(iso, selected_title, output, **kwargs):
        assert output == base
        # another writer takes the estimate after command display, before publication.
        estimate.write_bytes(b"another writer")
        return published

    encode.side_effect = racing_encode
    app = DVDRipperApp([dvd.path], output_dir=tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await idle(app, pilot)
        preview = app.query_one("#preview", Log)
        write = Mock(wraps=preview.write_line)
        monkeypatch.setattr(preview, "write_line", write)
        await click(app, pilot, "#preview-button")
        assert build.call_args.args[2] == estimate
        assert shlex.split(write.call_args.args[0])[-1] == str(estimate)
        assert "filenames shown are estimates" in log_text(app, "#preview")
        available.assert_called_with(base)
        assert not estimate.exists()
        encode.assert_not_awaited()
        log = app.query_one("#log", Log)
        log_write = Mock(wraps=log.write_line)
        monkeypatch.setattr(log, "write_line", log_write)
        await click(app, pilot, "#encode")
        await idle(app, pilot)
        encode.assert_awaited_once()
        assert encode.call_args.args == (dvd.path, title, base)
        assert all(call.args[2] == estimate for call in build.call_args_list)
        assert paths.call_count == 2
        for call in paths.call_args_list:
            assert call.args == (dvd.path, title, tmp_path)
            assert call.kwargs == {"unique": False}
        assert_logged(app, f"completed: {published}")
        messages = [call.args[0] for call in log_write.call_args_list]
        assert f"completed: {published}" in messages
        assert f"completed: {estimate}" not in messages
        assert base.read_bytes() == b"existing output"
        assert estimate.read_bytes() == b"another writer"
        assert "completed 1 title(s)" in status(app)


async def test_partial_scan_reports_warning_once_and_encodes_only_readable_titles(backend):
    dvd, scan, encode, _ = backend
    readable = Title(4, 600, dvd.titles[1].streams)
    warning = "skipped title 2: ffprobe failed\n[red]unreadable café's title[/red]\nlast detail"
    result = DVD(dvd.path, (dvd.titles[0], readable), (warning,))
    reported = asyncio.Event()
    release = asyncio.Event()

    async def partial_scan(path, **kwargs):
        kwargs["on_progress"](warning)
        reported.set()
        await release.wait()
        return result

    scan.side_effect = partial_scan
    app = DVDRipperApp([dvd.path], output_dir=dvd.path.parent)
    async with app.run_test(size=(100, 40)) as pilot:
        try:
            await asyncio.wait_for(reported.wait(), 3)
            await pilot.pause()
            assert status(app) == warning.splitlines()[0]
            assert_logged(app, warning)
            assert not app.query_one("#error", Static).display
        finally:
            release.set()
        await idle(app, pilot)
        assert app._dvd is result
        assert not list(app.query("#title-2"))
        assert selected(app) == [4]
        assert app.query_one("#audio-4", Select).value == 2
        assert "found 2 title(s); skipped 1 title(s)." in status(app)
        assert_logged(app, "scan complete: 2 title(s); skipped 1 title(s).")
        assert log_text(app).count(warning) == 1
        assert not app.query_one("#error", Static).display
        assert_action(app)
        await click(app, pilot, "#all")
        await click(app, pilot, "#encode")
        await idle(app, pilot)
        assert [call.args[1].number for call in encode.call_args_list] == [1, 4]
        assert [call.kwargs["audio_index"] for call in encode.call_args_list] == [1, 2]


@pytest.mark.parametrize("error_type", [FFmpegError, FFmpegUnavailableError])
async def test_scan_failure_is_full_literal_error_then_button_retry(backend, error_type):
    dvd, scan, encode, _ = backend
    detail = "cannot read [red]disc[/red]\nffprobe details: [bold]unreadable title[/bold]"
    scan.side_effect = [error_type(detail), dvd]
    app = DVDRipperApp([dvd.path], output_dir=dvd.path.parent)
    async with app.run_test(size=(100, 40)) as pilot:
        await idle(app, pilot)
        assert_error(app, f"scan failed: {detail}")
        assert_action(app, disabled=True)
        assert app.query_one("#preview-button", Button).disabled
        assert not app.query_one("#scan", Button).disabled
        await click(app, pilot, "#scan")
        await idle(app, pilot)
        assert selected(app) == [2]
        assert scan.await_count == 2
        assert str(app.query_one("#error", Static).render()) == ""
        assert_action(app)
        encode.assert_not_awaited()


async def test_empty_inputs_and_empty_scan(backend):
    dvd, scan, encode, _ = backend
    app = DVDRipperApp([], output_dir=dvd.path.parent)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        scan.assert_not_awaited()
        assert "no discs provided" in status(app)
        assert app.query_one("#scan", Button).disabled
        assert app.query_one("#disc", Select).disabled
        assert_action(app, disabled=True)
    scan.return_value = DVD(dvd.path, ())
    app = DVDRipperApp([dvd.path], output_dir=dvd.path.parent)
    async with app.run_test(size=(100, 40)) as pilot:
        await idle(app, pilot)
        assert "0 title(s)" in status(app)
        for selector in ("#preview-button", "#main", "#all"):
            assert app.query_one(selector, Button).disabled
        assert_action(app, disabled=True)
        encode.assert_not_awaited()


async def test_scan_responsive_cancel_cleanup_and_late_callback(backend):
    dvd, scan, _, _ = backend
    started = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    callbacks = []

    async def slow_scan(path, **kwargs):
        callbacks.append(kwargs["on_progress"])
        kwargs["on_progress"]("preindexing title 1")
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    scan.side_effect = slow_scan
    app = DVDRipperApp([dvd.path], output_dir=dvd.path.parent)
    async with app.run_test(size=(100, 40)) as pilot:
        try:
            await asyncio.wait_for(started.wait(), 3)
            await pilot.pause()
            assert "preindexing" in status(app)
            assert_busy_controls(app)
            assert_action(app, "cancel scan")
            await click(app, pilot, "#scan")
            assert scan.await_count == 1
            task = app._job
            await click(app, pilot, "#encode")
            await asyncio.wait_for(cleaning.wait(), 3)
            assert app._job is task
            assert_action(app, "cancelling…", disabled=True)
            assert_busy_controls(app)
            await click(app, pilot, "#encode")
            app._request_cancel()
            assert task.cancelling() == 1
            before = status(app), log_text(app)
            callbacks[0]("stale scan status")
            assert (status(app), log_text(app)) == before
        finally:
            release.set()
        await idle(app, pilot)
        assert "cancelled" in status(app)
        assert not app.query_one("#scan", Button).disabled
        assert app._dvd is None
        assert_action(app, disabled=True)
        scan.side_effect = None
        await click(app, pilot, "#scan")
        await idle(app, pilot)
        before = status(app), log_text(app)
        callbacks[0]("stale scan status")
        assert (status(app), log_text(app)) == before
        assert selected(app) == [2]
        assert_action(app)


async def test_encode_batch_progress_and_busy_controls(backend, tmp_path):
    dvd, _, encode, _ = backend
    started = asyncio.Event()
    release = asyncio.Event()
    callbacks = []

    async def slow_encode(iso, title, output, **kwargs):
        callbacks.append(kwargs["on_progress"])
        kwargs["on_progress"](Progress(seconds=60, speed="2.0x"))
        started.set()
        await release.wait()
        return output

    encode.side_effect = slow_encode
    app = DVDRipperApp([dvd.path], output_dir=tmp_path, ffmpeg="chosen-ffmpeg")
    async with app.run_test(size=(100, 40)) as pilot:
        try:
            await idle(app, pilot)
            await click(app, pilot, "#all")
            app.query_one("#audio-3", Select).value = SILENT
            app.query_one("#stereo", Checkbox).value = False
            app.query_one("#deinterlace", Select).value = "off"
            await pilot.pause()
            await click(app, pilot, "#encode")
            await asyncio.wait_for(started.wait(), 3)
            assert "60.0s" in status(app)
            assert "2.0x" in status(app)
            progress = app.query_one("#progress", ProgressBar)
            assert progress.total == 120
            assert progress.progress == 60
            assert_busy_controls(app)
            assert_action(app, "cancel encoding")
            await click(app, pilot, "#all")
            await click(app, pilot, "#use-output")
            assert encode.await_count == 1
            task = app._job
            before = status(app), progress.progress, log_text(app), log_text(app, "#preview")
            commands = "\n".join(app.query_one("#preview", Log).lines[1:])
            assert commands.count("chosen-ffmpeg") == 3
            assert not app.screen.selections
            for selector in ("#preview", "#log"):
                log = app.query_one(selector, Log)
                log.focus()
                first = 1 if selector == "#preview" else 0
                expected = "\n".join(log.lines[first:])
                app.screen.selections = {
                    log: Selection.from_offsets(
                        Offset(0, first), Offset(len(log.lines[-1]), len(log.lines) - 1)
                    )
                }
                assert app.screen.get_selected_text() == expected
                assert app._job is task
                assert task.cancelling() == 0 and not task.done()
                assert not app._cancelling
                assert_action(app, "cancel encoding")
                assert_busy_controls(app)
                assert before == (
                    status(app),
                    progress.progress,
                    log_text(app),
                    log_text(app, "#preview"),
                )
            assert encode.await_count == 1
        finally:
            release.set()
        await idle(app, pilot)
        assert [call.args[1].number for call in encode.call_args_list] == [1, 2, 3]
        assert [call.kwargs["audio_index"] for call in encode.call_args_list] == [1, 2, None]
        for call in encode.call_args_list:
            assert call.args[0] == dvd.path
            assert call.args[2] == output_path(dvd.path, call.args[1], tmp_path, unique=False)
            assert call.kwargs["settings"] == EncodeSettings(stereo=False, deinterlace="off")
            assert call.kwargs["ffmpeg"] == "chosen-ffmpeg"
            assert_logged(app, f"completed: {call.args[2]}")
        assert "completed 3 title(s)" in status(app)
        assert_action(app)
        assert progress.total == 1 and progress.progress == 1
        before = status(app), progress.progress
        for callback in callbacks:
            callback(Progress(seconds=999, speed="stale"))
        assert (status(app), progress.progress) == before
        for control in app.query("Select, Checkbox, Input"):
            assert not control.disabled, control.id
        assert not app.query_one("#use-output", Button).disabled


async def test_main_encode_action_cancels_once_stops_queue_and_keeps_completed_log(backend):
    dvd, _, encode, _ = backend
    started = asyncio.Event()
    cleaning = asyncio.Event()
    cleaned = asyncio.Event()
    release = asyncio.Event()
    callbacks = []
    completed = output_path(dvd.path, dvd.titles[0], dvd.path.parent, unique=False)

    async def slow_batch(iso, title, output, **kwargs):
        if title.number == 1:
            return completed
        callbacks.append(kwargs["on_progress"])
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    encode.side_effect = slow_batch
    app = DVDRipperApp([dvd.path], output_dir=dvd.path.parent)
    async with app.run_test(size=(100, 40)) as pilot:
        try:
            await idle(app, pilot)
            assert_action(app)
            await click(app, pilot, "#all")
            await click(app, pilot, "#encode")
            await asyncio.wait_for(started.wait(), 3)
            assert_action(app, "cancel encoding")
            assert_logged(app, f"completed: {completed}")
            task = app._job
            await click(app, pilot, "#encode")
            await asyncio.wait_for(cleaning.wait(), 3)
            assert_action(app, "cancelling…", disabled=True)
            assert_busy_controls(app)
            await click(app, pilot, "#encode")
            app._request_cancel()
            assert task.cancelling() == 1
            assert app._job is task
            assert not cleaned.is_set()
            assert encode.await_count == 2
            before = status(app), app.query_one("#progress", ProgressBar).progress
            callbacks[0](Progress(seconds=999, speed="stale"))
            assert (status(app), app.query_one("#progress", ProgressBar).progress) == before
        finally:
            release.set()
        await idle(app, pilot)
        assert cleaned.is_set()
        assert [call.args[1].number for call in encode.call_args_list] == [1, 2]
        assert "cancelled; completed files are kept" in status(app)
        assert_logged(app, f"completed: {completed}")
        assert_logged(app, "encode cancelled; completed files are kept.")
        assert_action(app)
        assert not app.query_one("#scan", Button).disabled
        before = status(app), app.query_one("#progress", ProgressBar).progress
        callbacks[0](Progress(seconds=999, speed="stale"))
        assert (status(app), app.query_one("#progress", ProgressBar).progress) == before


@pytest.mark.parametrize("quit_key", ["q", "ctrl+c", "ctrl+q"])
@pytest.mark.parametrize("job_name", ["scan", "encode"])
async def test_quit_waits_for_backend_cleanup_and_ignores_late_callbacks(
    backend, job_name, quit_key
):
    dvd, scan, encode, _ = backend
    started = asyncio.Event()
    cleaning = asyncio.Event()
    cleaned = asyncio.Event()
    release = asyncio.Event()
    callbacks = []

    async def slow_job(*args, **kwargs):
        callbacks.append(kwargs["on_progress"])
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    if job_name == "scan":
        scan.side_effect = slow_job
    else:
        encode.side_effect = slow_job
    app = DVDRipperApp([dvd.path], output_dir=dvd.path.parent)
    async with app.run_test(size=(100, 40)) as pilot:
        try:
            if job_name == "encode":
                await idle(app, pilot)
                await click(app, pilot, "#encode")
            await asyncio.wait_for(started.wait(), 3)
            task = app._job
            log = app.query_one("#log", Log)
            app._write_log("selected text must not prevent quitting")
            log.focus()
            app.screen.selections = {log: Selection.from_offsets(Offset(0, 0), Offset(5, 0))}
            await pilot.pause()
            assert app.screen.get_selected_text()
            # do not await the key dispatch until the deliberately held cleanup can finish.
            quitting = asyncio.create_task(pilot.press(quit_key))
            await asyncio.wait_for(cleaning.wait(), 3)
            assert not cleaned.is_set()
            assert app._job is task
            assert task.cancelling() == 1
            assert_action(app, "cancelling…", disabled=True)
            before = status(app), log_text(app)
            callbacks[0]("stale scan" if job_name == "scan" else Progress(999, "stale"))
            assert (status(app), log_text(app)) == before
        finally:
            release.set()
        await asyncio.wait_for(quitting, 3)
        await asyncio.wait_for(cleaned.wait(), 3)
        await idle(app, pilot)
        assert app._job is None


@pytest.mark.parametrize("job_name", ["scan", "encode"])
async def test_unmount_also_cancels_active_job(backend, job_name):
    dvd, scan, encode, _ = backend
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def slow_job(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.02)
            cleaned.set()

    if job_name == "scan":
        scan.side_effect = slow_job
    else:
        encode.side_effect = slow_job
    app = DVDRipperApp([dvd.path], output_dir=dvd.path.parent)
    async with app.run_test(size=(100, 40)) as pilot:
        if job_name == "encode":
            await idle(app, pilot)
            await click(app, pilot, "#encode")
        await asyncio.wait_for(started.wait(), 3)
    assert cleaned.is_set()
    assert app._job is None


async def test_encode_error_is_full_literal_text_and_stops_batch(backend):
    dvd, _, encode, _ = backend
    detail = "ffmpeg failed [red](exit 1)[/red]\n[bold]cannot write frame[/bold]\nlast stderr line"
    encode.side_effect = FFmpegError(detail)
    app = DVDRipperApp([dvd.path], output_dir=dvd.path.parent)
    async with app.run_test(size=(100, 40)) as pilot:
        await idle(app, pilot)
        await click(app, pilot, "#all")
        await click(app, pilot, "#encode")
        await idle(app, pilot)
        encode.assert_awaited_once()
        assert_error(app, f"encode failed: {detail}")
        assert not app.query_one("#scan", Button).disabled
        assert_action(app)
        assert app.query_one("#progress", ProgressBar).progress == 0


async def test_default_80_by_24_layout_keeps_actions_separate_and_reachable(backend):
    dvd, _, encode, _ = backend
    app = DVDRipperApp([dvd.path], output_dir=dvd.path.parent)
    async with app.run_test() as pilot:
        await idle(app, pilot)
        assert (app.size.width, app.size.height) == (80, 24)
        action = app.query_one("#encode", Button)
        content = app.query_one("#content")
        assert_action(app)
        assert action.region.y >= content.region.bottom
        assert app.screen.region.contains_region(action.region)
        for selector in ("#status", "#progress", "Footer"):
            assert not action.region.overlaps(app.query_one(selector).region)
        main = app.query_one("#main", Button)
        all_titles = app.query_one("#all", Button)
        command = app.query_one("#preview-button", Button)
        assert main.parent.id == all_titles.parent.id == "title-actions"
        assert command.parent.id == "command-actions"
        assert str(command.label) == "show ffmpeg command"
        await click(app, pilot, "#all")
        assert selected(app) == [1, 2, 3]
        for button in (main, all_titles):
            assert app.screen.region.contains_region(button.region)
            assert not button.region.overlaps(action.region)
            assert button.region.width <= 10
        assert main.region.y == all_titles.region.y
        assert not main.region.overlaps(all_titles.region)
        await click(app, pilot, "#main")
        assert selected(app) == [2]
        await click(app, pilot, "#preview-button")
        assert app.screen.region.contains_region(command.region)
        assert not command.region.overlaps(action.region)
        assert not list(app.query("#copy-command, #copy-log"))
        field = app.query_one("#output-directory", Input)
        use_output = app.query_one("#use-output", Button)
        field.scroll_visible(animate=False)
        await pilot.pause()
        assert field.parent is use_output.parent
        assert field.region.y == use_output.region.y
        assert not field.region.overlaps(use_output.region)
        for control in (field, use_output):
            assert app.screen.region.contains_region(control.region)
            assert not control.region.overlaps(action.region)
        encode.assert_not_awaited()
