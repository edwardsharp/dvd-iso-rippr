"""cli and dependency diagnostics without textual, media tools, or a real ui."""

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from dvd_ripper import cli, ffmpeg, tool_setup


@pytest.fixture(autouse=True)
def project_root(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    monkeypatch.setattr(tool_setup, "PROJECT_ROOT", root)
    return root


@pytest.fixture(autouse=True)
def no_tool_processes(monkeypatch):
    spawn = AsyncMock(side_effect=AssertionError("tests must not launch media tools"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return spawn


@pytest.fixture
def cli_backend(monkeypatch, tmp_path):
    check = AsyncMock(return_value=[])
    textual = Mock(return_value=object())
    app = Mock(name="DVDRipperApp")
    module = ModuleType("dvd_ripper.app")
    module.DVDRipperApp = app
    monkeypatch.setattr(cli, "dependency_issues", check)
    monkeypatch.setattr(cli.importlib.util, "find_spec", textual)
    monkeypatch.setitem(sys.modules, "dvd_ripper.app", module)
    tool_paths = {name: str(tmp_path / "path tools" / name) for name in ("ffmpeg", "ffprobe")}
    which = Mock(side_effect=tool_paths.get)
    monkeypatch.setattr(cli.shutil, "which", which)
    return SimpleNamespace(
        check=check, textual=textual, app=app, which=which, tool_paths=tool_paths
    )


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "Movie with spaces.ISO"
    path.touch()
    return path.resolve()


def assert_setup_guidance(text):
    assert "python3 scripts/setup.py" in text
    assert "py -3 scripts/setup.py" in text
    assert ".venv/bin/python" in text
    assert ".venv\\Scripts\\python.exe" in text
    assert "python -m pip install -e ." in text
    assert "README.md" in text


def assert_selected_tools(text, tools):
    for name, path in tools.items():
        assert f"{name}: {path}" in text.splitlines()


def assert_no_startup(backend):
    backend.check.assert_not_awaited()
    backend.textual.assert_not_called()
    backend.app.assert_not_called()
    backend.app.return_value.run.assert_not_called()


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_works_without_python_or_tool_dependencies(flag, cli_backend, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "textual", None)
    monkeypatch.setitem(sys.modules, "dvd_ripper.app", None)
    cli_backend.textual.return_value = None
    cli_backend.check.side_effect = AssertionError("help must not check media tools")

    with pytest.raises(SystemExit) as exc:
        cli.main([flag])

    assert exc.value.code == 0
    out, err = capsys.readouterr()
    assert "usage:" in out
    for option in (
        "PATH",
        "--check",
        "--setup-ffmpeg",
        "--recursive",
        "--output-dir",
        "--ffmpeg",
        "--ffprobe",
    ):
        assert option in out
    assert err == ""
    assert_no_startup(cli_backend)


def test_no_input_prints_help_and_setup_guidance_without_dependencies(
    cli_backend, monkeypatch, capsys
):
    monkeypatch.setitem(sys.modules, "textual", None)
    monkeypatch.setitem(sys.modules, "dvd_ripper.app", None)
    cli_backend.textual.return_value = None
    cli_backend.check.side_effect = AssertionError("no input must not check media tools")

    assert cli.main([]) == 2

    out, err = capsys.readouterr()
    assert "usage:" in out
    assert "first time here?" in out
    assert_setup_guidance(out)
    assert err == ""
    assert_no_startup(cli_backend)


def test_check_ready_does_not_discover_paths_or_launch_ui(
    cli_backend, monkeypatch, tmp_path, capsys
):
    discover = Mock(side_effect=AssertionError("--check must not scan inputs"))
    monkeypatch.setattr(cli, "find_isos", discover)

    assert cli.main(["--check", str(tmp_path / "missing.iso")]) == 0

    out, err = capsys.readouterr()
    assert f"Python {sys.version.split()[0]} ({sys.executable})" in out
    assert "ready:" in out
    assert_selected_tools(out, cli_backend.tool_paths)
    for capability in (
        "Textual",
        "ffmpeg/ffprobe",
        "dvdvideo",
        "preindex",
        "libx264",
        "AAC",
        "bwdif",
        "mp4",
    ):
        assert capability in out
    assert err == ""
    cli_backend.textual.assert_called_once_with("textual")
    cli_backend.check.assert_awaited_once_with(ffmpeg="ffmpeg", ffprobe="ffprobe")
    discover.assert_not_called()
    cli_backend.app.assert_not_called()


def test_check_tool_failures_are_actionable_and_do_not_launch_ui(cli_backend, capsys):
    issues = ["ffmpeg: missing dvdvideo demuxer", "ffprobe: missing dvdvideo demuxer"]
    cli_backend.check.return_value = issues

    assert cli.main(["--check"]) == 1

    out, err = capsys.readouterr()
    assert sys.executable in out
    assert "ready:" not in out
    assert_selected_tools(out, cli_backend.tool_paths)
    assert "not ready to convert:" in err
    for issue in issues:
        assert issue in err
    assert "README.md" in err
    assert "docs/ffmpeg.md" in err
    assert "--setup-ffmpeg" in err
    assert "--ffmpeg /path/to/ffmpeg --ffprobe /path/to/ffprobe" in err
    cli_backend.check.assert_awaited_once()
    cli_backend.app.assert_not_called()


@pytest.mark.parametrize("tool_issues", [[], ["ffprobe: missing dvdvideo demuxer"]])
def test_check_missing_textual_reports_setup_and_still_checks_tools(
    tool_issues, cli_backend, capsys
):
    cli_backend.textual.return_value = None
    cli_backend.check.return_value = tool_issues

    assert cli.main(["--check"]) == 1

    out, err = capsys.readouterr()
    assert "ready:" not in out
    assert_selected_tools(out, cli_backend.tool_paths)
    assert "missing Python dependency: Textual" in err
    assert_setup_guidance(err)
    for issue in tool_issues:
        assert issue in err
    cli_backend.check.assert_awaited_once_with(ffmpeg="ffmpeg", ffprobe="ffprobe")
    cli_backend.app.assert_not_called()


@pytest.mark.parametrize("missing", ["textual", "tools"])
def test_conversion_does_not_launch_ui_when_dependencies_are_missing(
    missing, image, cli_backend, capsys
):
    if missing == "textual":
        cli_backend.textual.return_value = None
    else:
        cli_backend.check.return_value = ["ffmpeg: missing libx264 encoder"]

    assert cli.main([str(image)]) == 1

    assert "not ready to convert:" in capsys.readouterr().err
    cli_backend.check.assert_awaited_once()
    cli_backend.app.assert_not_called()


@pytest.mark.parametrize("argv", [["--not-an-option"], ["--ffmpeg"], ["--ffprobe"], ["-o"]])
def test_malformed_arguments_fail_before_dependency_checks(argv, cli_backend, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)

    assert exc.value.code == 2
    assert "error:" in capsys.readouterr().err
    assert_no_startup(cli_backend)


@pytest.mark.parametrize("kind", ["missing", "unsupported", "nul"])
def test_bad_input_paths_fail_before_dependency_checks(kind, tmp_path, cli_backend, capsys):
    if kind == "missing":
        path = str(tmp_path / "missing.iso")
        message = "missing.iso"
    elif kind == "unsupported":
        unsupported = tmp_path / "movie.mp4"
        unsupported.touch()
        path = str(unsupported)
        message = ".iso or .img"
    else:
        path = str(tmp_path / "bad\x00name.iso")
        message = "null"

    assert cli.main([path]) == 2

    out, err = capsys.readouterr()
    assert message in err.lower()
    assert "Traceback" not in err
    assert out == ""
    assert_no_startup(cli_backend)


def test_invalid_path_does_not_launch_ui_for_other_valid_inputs(
    image, tmp_path, cli_backend, capsys
):
    assert cli.main([str(image), str(tmp_path / "missing.iso")]) == 2

    assert "missing.iso" in capsys.readouterr().err
    assert_no_startup(cli_backend)


@pytest.mark.parametrize("nested_only", [False, True])
def test_empty_discovery_reports_recursive_guidance(nested_only, tmp_path, cli_backend, capsys):
    if nested_only:
        nested = tmp_path / "nested"
        nested.mkdir()
        (nested / "disc.iso").touch()

    assert cli.main([str(tmp_path)]) == 2

    out, err = capsys.readouterr()
    assert "no .iso or .img files found" in err
    assert "--recursive" in err
    assert out == ""
    assert_no_startup(cli_backend)


@pytest.mark.parametrize("recursive", [False, True])
def test_directory_and_repeated_file_arguments_forward_deduplicated_images(
    recursive, tmp_path, cli_backend
):
    first = tmp_path / "a.ISO"
    second = tmp_path / "b.img"
    first.touch()
    second.touch()
    (tmp_path / "ignored.mp4").touch()
    nested = tmp_path / "nested"
    nested.mkdir()
    nested_image = nested / "extra.iso"
    nested_image.touch()
    argv = [str(second), str(tmp_path), str(first), str(second)]
    if recursive:
        argv.append("--recursive")
    expected = [first.resolve(), second.resolve()]
    if recursive:
        expected.append(nested_image.resolve())

    assert cli.main(argv) == 0

    cli_backend.app.assert_called_once_with(
        expected, output_dir=None, ffmpeg="ffmpeg", ffprobe="ffprobe"
    )
    cli_backend.app.return_value.run.assert_called_once_with()
    cli_backend.check.assert_awaited_once_with(ffmpeg="ffmpeg", ffprobe="ffprobe")


@pytest.mark.parametrize("output_option", ["-o", "--output-dir"])
def test_custom_tools_and_output_directory_propagate_to_app(
    output_option, image, tmp_path, cli_backend
):
    output = tmp_path / "new output directory"
    tools = {
        "ffmpeg": str(tmp_path / "custom tools" / "ffmpeg"),
        "ffprobe": str(tmp_path / "custom tools" / "ffprobe"),
    }

    assert (
        cli.main(
            [
                str(image),
                output_option,
                str(output),
                "--ffmpeg",
                tools["ffmpeg"],
                "--ffprobe",
                tools["ffprobe"],
            ]
        )
        == 0
    )

    cli_backend.check.assert_awaited_once_with(**tools)
    cli_backend.app.assert_called_once_with([image], output_dir=output, **tools)
    cli_backend.app.return_value.run.assert_called_once_with()
    assert not output.exists()


@pytest.mark.parametrize("issues", [[], ["custom ffprobe: missing dvdvideo demuxer"]])
def test_check_uses_custom_tools(issues, cli_backend, tmp_path, capsys):
    tools = {"ffmpeg": str(tmp_path / "ffmpeg-dvd"), "ffprobe": str(tmp_path / "ffprobe-dvd")}

    cli_backend.check.return_value = issues

    status = cli.main(["--check", "--ffmpeg", tools["ffmpeg"], "--ffprobe", tools["ffprobe"]])

    assert status == (1 if issues else 0)
    cli_backend.check.assert_awaited_once_with(**tools)
    cli_backend.app.assert_not_called()
    out, err = capsys.readouterr()
    assert_selected_tools(out, tools)
    assert ("ready:" in out) is (not issues)
    for issue in issues:
        assert issue in err


def test_ui_import_error_is_actionable(image, cli_backend, monkeypatch, capsys):
    def broken_import(name):
        if name == "DVDRipperApp":
            raise ImportError("cannot import name 'App' from 'textual.app'")
        raise AttributeError(name)

    module = ModuleType("dvd_ripper.app")
    module.__getattr__ = broken_import
    monkeypatch.setitem(sys.modules, "dvd_ripper.app", module)

    assert cli.main([str(image)]) == 1

    out, err = capsys.readouterr()
    assert "could not load the terminal UI:" in err
    assert "cannot import name 'App' from 'textual.app'" in err
    assert_setup_guidance(err)
    assert out == ""
    cli_backend.check.assert_awaited_once()
    cli_backend.app.assert_not_called()


@pytest.mark.parametrize("mode", ["setup", "check", "missing-input", "custom-tools"])
def test_setup_ffmpeg_prints_instructions_without_checking_dependencies_or_files(
    mode, cli_backend, monkeypatch, tmp_path, capsys
):
    instructions = Mock(return_value="platform-specific build instructions\nrun these yourself.")
    discover = Mock(side_effect=AssertionError("setup must not scan files"))
    select = Mock(side_effect=AssertionError("setup must not discover tool files"))
    monkeypatch.setattr(cli, "setup_instructions", instructions)
    monkeypatch.setattr(cli, "find_isos", discover)
    monkeypatch.setattr(cli, "select_tools", select)
    monkeypatch.setitem(sys.modules, "textual", None)
    monkeypatch.setitem(sys.modules, "dvd_ripper.app", None)
    cli_backend.textual.return_value = None
    cli_backend.check.side_effect = AssertionError("setup must not check tools")
    argv = ["--setup-ffmpeg"]
    if mode == "check":
        argv.append("--check")
    elif mode == "missing-input":
        argv.append(str(tmp_path / "missing.iso"))
    elif mode == "custom-tools":
        argv += ["--ffmpeg", str(tmp_path / "missing ffmpeg"), "--ffprobe", "missing-ffprobe"]

    assert cli.main(argv) == 0

    out, err = capsys.readouterr()
    assert out == instructions.return_value + "\n"
    assert err == ""
    instructions.assert_called_once_with()
    discover.assert_not_called()
    select.assert_not_called()
    cli_backend.which.assert_not_called()
    assert_no_startup(cli_backend)


@pytest.fixture
def local_tools(project_root):
    directory = project_root / ".tools" / "ffmpeg" / "bin"
    directory.mkdir(parents=True)
    tools = {}
    for name in ("ffmpeg", "ffprobe"):
        path = directory / name
        path.touch()
        tools[name] = str(path)
    return tools


@pytest.mark.parametrize("issues", [[], ["local ffprobe: missing dvdvideo preindex option"]])
def test_check_prefers_local_pair_and_prints_paths_even_on_failure(
    issues, local_tools, cli_backend, capsys
):
    cli_backend.check.return_value = issues

    assert cli.main(["--check"]) == (1 if issues else 0)

    out, err = capsys.readouterr()
    assert_selected_tools(out, local_tools)
    assert ("ready:" in out) is (not issues)
    for issue in issues:
        assert issue in err
    cli_backend.check.assert_awaited_once_with(**local_tools)
    cli_backend.app.assert_not_called()


@pytest.mark.parametrize("issues", [[], ["could not run 'ffmpeg': missing executable"]])
def test_check_prints_selected_names_when_which_cannot_find_tools(issues, cli_backend, capsys):
    cli_backend.which.side_effect = None
    cli_backend.which.return_value = None
    cli_backend.check.return_value = issues

    assert cli.main(["--check"]) == (1 if issues else 0)

    assert_selected_tools(capsys.readouterr().out, {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"})
    cli_backend.check.assert_awaited_once_with(ffmpeg="ffmpeg", ffprobe="ffprobe")
    cli_backend.app.assert_not_called()


def test_conversion_passes_local_pair_to_dependency_check_and_app(image, local_tools, cli_backend):
    assert cli.main([str(image)]) == 0

    cli_backend.check.assert_awaited_once_with(**local_tools)
    cli_backend.app.assert_called_once_with([image], output_dir=None, **local_tools)
    cli_backend.app.return_value.run.assert_called_once_with()


@pytest.mark.parametrize("tool", ["ffmpeg", "ffprobe"])
def test_check_single_override_bypasses_local_pair(
    tool, local_tools, tmp_path, cli_backend, capsys
):
    custom = str(tmp_path / "custom tools" / tool)
    selected = {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe", tool: custom}

    assert cli.main(["--check", f"--{tool}", custom]) == 0

    cli_backend.check.assert_awaited_once_with(**selected)
    assert_selected_tools(capsys.readouterr().out, {**cli_backend.tool_paths, tool: custom})
    cli_backend.app.assert_not_called()


@pytest.mark.parametrize("check", [False, True])
def test_incomplete_local_pair_reports_repair_guidance_before_dependency_checks(
    check, image, project_root, cli_backend, capsys
):
    directory = project_root / ".tools" / "ffmpeg" / "bin"
    directory.mkdir(parents=True)
    (directory / "ffmpeg").touch()

    assert cli.main(["--check"] if check else [str(image)]) == 2

    out, err = capsys.readouterr()
    assert "incomplete local ffmpeg build" in err
    assert str(directory) in err
    assert "both ffmpeg and ffprobe are needed" in err
    assert "--setup-ffmpeg" in err
    assert "specify both tool paths" in err
    assert out == ""
    assert_no_startup(cli_backend)


DEMUXERS = """Demuxers:
 D. = Demuxing supported
 --
 D  dvdvideo        DVD-Video
 D  mov,mp4,m4a     QuickTime / MOV
"""
ENCODERS = """Encoders:
 V..... = Video
 A..... = Audio
 ------
 V....D libx264      libx264 H.264 / AVC (codec h264)
 A..... aac          AAC (Advanced Audio Coding)
"""
FILTERS = """Filters:
 T.. = Timeline support
 .S. = Slice threading
 ..C = Command support
 ---
 TS. bwdif            V->V       Deinterlace the input image.
"""


DVDVIDEO_HELP = """Demuxer dvdvideo [DVD-Video]:
dvdvideo AVOptions:
  -title             <int>        .D......... title number (from 0 to 99) (default 0)
  -preindex          <boolean>    .D......... enable title pre-indexing (default false)
"""
MUXERS = """Muxers:
 E. = Muxing supported
 --
 E  mp4              MP4 (MPEG-4 Part 14)
"""
FEATURES = {
    "libx264": ("-encoders", "encoder"),
    "aac": ("-encoders", "encoder"),
    "bwdif": ("-filters", "filter"),
    "mp4": ("-muxers", "muxer"),
}
CAPABILITY_CALLS = [
    call(["ffmpeg", "-hide_banner", "-demuxers"]),
    call(["ffmpeg", "-hide_banner", "-h", "demuxer=dvdvideo"]),
    call(["ffprobe", "-hide_banner", "-demuxers"]),
    call(["ffprobe", "-hide_banner", "-h", "demuxer=dvdvideo"]),
    call(["ffmpeg", "-hide_banner", "-encoders"]),
    call(["ffmpeg", "-hide_banner", "-filters"]),
    call(["ffmpeg", "-hide_banner", "-muxers"]),
]


@pytest.fixture
def capability_tables(monkeypatch):
    tables = {
        ("ffmpeg", "-demuxers"): DEMUXERS,
        ("ffmpeg", "demuxer=dvdvideo"): DVDVIDEO_HELP,
        ("ffprobe", "-demuxers"): DEMUXERS,
        ("ffprobe", "demuxer=dvdvideo"): DVDVIDEO_HELP,
        ("ffmpeg", "-encoders"): ENCODERS,
        ("ffmpeg", "-filters"): FILTERS,
        ("ffmpeg", "-muxers"): MUXERS,
    }

    async def capture(args):
        tool, banner, *options = args
        assert banner == "-hide_banner"
        if options[-1] == "demuxer=dvdvideo":
            assert options == ["-h", "demuxer=dvdvideo"]
        else:
            assert len(options) == 1
        result = tables[(tool, options[-1])]
        if isinstance(result, Exception):
            raise result
        return result

    mock = AsyncMock(side_effect=capture)
    monkeypatch.setattr(ffmpeg, "run_capture", mock)
    return tables, mock


def test_dependency_issues_checks_both_demuxers_preindex_encoders_filter_and_muxer(
    capability_tables,
):
    _, capture = capability_tables

    assert asyncio.run(ffmpeg.dependency_issues()) == []

    assert capture.await_args_list == CAPABILITY_CALLS


@pytest.mark.parametrize("tool", ["ffmpeg", "ffprobe"])
@pytest.mark.parametrize(
    "listing",
    [
        "Demuxers:\n D  mpeg MPEG-PS (MPEG-2 Program Stream)\n",
        "Unknown format 'dvdvideo'.\n",
        " D  dvdvideo_extra not the dvdvideo demuxer\n D  other dvdvideo compatibility\n",
    ],
)
def test_dependency_issues_rejects_successful_output_without_dvdvideo(
    tool, listing, capability_tables
):
    tables, capture = capability_tables
    # a returned string represents a successful exit, not proof of dvd support.
    tables[(tool, "-demuxers")] = listing

    issues = asyncio.run(ffmpeg.dependency_issues())

    assert len(issues) == 1
    assert f"{tool}: missing dvdvideo demuxer" in issues[0]
    assert "FFmpeg 7+" in issues[0]
    assert "--enable-libdvdnav --enable-libdvdread" in issues[0]
    assert "both ffmpeg and ffprobe" in issues[0]
    assert "reinstalling Python dependencies cannot fix this" in issues[0]
    skipped = call([tool, "-hide_banner", "-h", "demuxer=dvdvideo"])
    assert capture.await_args_list == [
        invocation for invocation in CAPABILITY_CALLS if invocation != skipped
    ]


@pytest.mark.parametrize(
    "missing",
    [
        ("libx264",),
        ("aac",),
        ("bwdif",),
        ("mp4",),
        ("libx264", "aac", "bwdif"),
        ("libx264", "aac", "bwdif", "mp4"),
    ],
)
def test_dependency_issues_reports_missing_encoders_filter_and_muxer(missing, capability_tables):
    tables, capture = capability_tables
    for feature in missing:
        option, _ = FEATURES[feature]
        # similar names and descriptions must not count as a matching table entry.
        tables[("ffmpeg", option)] = tables[("ffmpeg", option)].replace(feature, f"{feature}_other")
        tables[("ffmpeg", option)] += f" ... other description mentioning {feature}\n"

    issues = asyncio.run(ffmpeg.dependency_issues())

    assert len(issues) == len(missing)
    for feature in missing:
        _, kind = FEATURES[feature]
        assert any(f"ffmpeg: missing {feature} {kind}" in issue for issue in issues)
    assert capture.await_args_list == CAPABILITY_CALLS


@pytest.mark.parametrize("tool", ["ffmpeg", "ffprobe"])
@pytest.mark.parametrize(
    "help_text",
    [
        "",
        "Unknown format 'dvdvideo'.\n",
        "  -preindex_extra <boolean> extra option\n",
        "  --preindex <boolean> wrong spelling\n",
        "  -preindex=true <boolean> not a standalone token\n",
        "  -other <boolean> description mentioning -preindex\n",
        "preindex is mentioned here, but is not an option\n",
    ],
)
def test_dependency_issues_requires_exact_preindex_line_token(tool, help_text, capability_tables):
    tables, capture = capability_tables
    tables[(tool, "demuxer=dvdvideo")] = help_text

    issues = asyncio.run(ffmpeg.dependency_issues())

    assert issues == [f"{tool}: missing dvdvideo preindex option; use a complete dvdvideo build."]
    assert capture.await_args_list == CAPABILITY_CALLS


@pytest.mark.parametrize("help_text", ["-preindex <boolean> option\n", "\t-preindex\t<boolean>\n"])
def test_dependency_issues_accepts_preindex_with_different_indentation(
    help_text, capability_tables
):
    tables, capture = capability_tables
    for tool in ("ffmpeg", "ffprobe"):
        tables[(tool, "demuxer=dvdvideo")] = help_text

    assert asyncio.run(ffmpeg.dependency_issues()) == []

    assert capture.await_args_list == CAPABILITY_CALLS


@pytest.mark.parametrize("tool", ["ffmpeg", "ffprobe"])
@pytest.mark.parametrize("status", [0, 7])
def test_dependency_issues_rejects_unknown_format_help_even_with_status_zero(
    tool, status, no_tool_processes
):
    tables = {
        "-demuxers": DEMUXERS,
        "demuxer=dvdvideo": DVDVIDEO_HELP,
        "-encoders": ENCODERS,
        "-filters": FILTERS,
        "-muxers": MUXERS,
    }
    external_error = b"Unknown format 'dvdvideo'.\n"

    async def spawn(binary, *args, **kwargs):
        failing_help = binary == tool and args[-1] == "demuxer=dvdvideo"
        if failing_help:
            stdout, stderr = (external_error, b"") if status == 0 else (b"", external_error)
        else:
            stdout, stderr = tables[args[-1]].encode(), b""
        return SimpleNamespace(
            returncode=status if failing_help else 0,
            communicate=AsyncMock(return_value=(stdout, stderr)),
        )

    no_tool_processes.side_effect = spawn

    issues = asyncio.run(ffmpeg.dependency_issues())

    assert len(issues) == 1
    if status == 0:
        assert f"{tool}: missing dvdvideo preindex option" in issues[0]
    else:
        assert (
            f"command failed (exit {status}): {tool} -hide_banner -h demuxer=dvdvideo" in issues[0]
        )
        assert external_error.decode().strip() in issues[0]
    assert [
        call(list(invocation.args)) for invocation in no_tool_processes.await_args_list
    ] == CAPABILITY_CALLS


@pytest.mark.parametrize("demuxers_missing", [False, True])
def test_dependency_issues_collects_all_independent_missing_features(
    demuxers_missing, capability_tables
):
    tables, capture = capability_tables
    for tool in ("ffmpeg", "ffprobe"):
        tables[(tool, "-demuxers" if demuxers_missing else "demuxer=dvdvideo")] = ""
    for option in ("-encoders", "-filters", "-muxers"):
        tables[("ffmpeg", option)] = ""

    issues = asyncio.run(ffmpeg.dependency_issues())

    assert len(issues) == 6
    missing = "dvdvideo demuxer" if demuxers_missing else "dvdvideo preindex option"
    for tool in ("ffmpeg", "ffprobe"):
        assert any(f"{tool}: missing {missing}" in issue for issue in issues)
    for feature, (_, kind) in FEATURES.items():
        assert any(f"ffmpeg: missing {feature} {kind}" in issue for issue in issues)
    assert capture.await_args_list == [
        invocation
        for invocation in CAPABILITY_CALLS
        if not demuxers_missing or "-h" not in invocation.args[0]
    ]


@pytest.mark.parametrize(
    ("tool", "option"),
    [
        ("ffmpeg", "-demuxers"),
        ("ffprobe", "-demuxers"),
        ("ffmpeg", "demuxer=dvdvideo"),
        ("ffprobe", "demuxer=dvdvideo"),
        ("ffmpeg", "-encoders"),
        ("ffmpeg", "-filters"),
        ("ffmpeg", "-muxers"),
    ],
)
def test_dependency_issues_continues_after_each_failed_capability_query(
    tool, option, capability_tables
):
    tables, capture = capability_tables
    message = f"command failed: {tool} {option}"
    tables[(tool, option)] = ffmpeg.FFmpegError(message)

    assert asyncio.run(ffmpeg.dependency_issues()) == [message]

    skipped = (
        call([tool, "-hide_banner", "-h", "demuxer=dvdvideo"]) if option == "-demuxers" else None
    )
    assert capture.await_args_list == [
        invocation for invocation in CAPABILITY_CALLS if invocation != skipped
    ]


def test_dependency_issues_deduplicates_repeated_errors_without_losing_other_issues(
    capability_tables,
):
    tables, capture = capability_tables
    message = "could not run 'ffmpeg': installation is broken"
    for option in ("-demuxers", "-encoders", "-filters", "-muxers"):
        tables[("ffmpeg", option)] = ffmpeg.FFmpegError(message)
    tables[("ffprobe", "demuxer=dvdvideo")] = ""

    assert asyncio.run(ffmpeg.dependency_issues()) == [
        message,
        "ffprobe: missing dvdvideo preindex option; use a complete dvdvideo build.",
    ]

    skipped = call(["ffmpeg", "-hide_banner", "-h", "demuxer=dvdvideo"])
    assert capture.await_args_list == [
        invocation for invocation in CAPABILITY_CALLS if invocation != skipped
    ]


@pytest.mark.parametrize("missing", [("ffmpeg",), ("ffprobe",), ("ffmpeg", "ffprobe")])
def test_dependency_issues_missing_tools_are_actionable_and_both_are_checked(
    missing, no_tool_processes
):
    tables = {
        "-demuxers": DEMUXERS,
        "demuxer=dvdvideo": DVDVIDEO_HELP,
        "-encoders": ENCODERS,
        "-filters": FILTERS,
        "-muxers": MUXERS,
    }

    async def spawn(tool, *args, **kwargs):
        if tool in missing:
            raise FileNotFoundError(2, "No such file or directory", tool)
        return SimpleNamespace(
            returncode=0,
            communicate=AsyncMock(return_value=(tables[args[-1]].encode(), b"")),
        )

    no_tool_processes.side_effect = spawn

    issues = asyncio.run(ffmpeg.dependency_issues())

    assert len(issues) == len(missing)
    for tool in missing:
        assert any(
            f"could not run '{tool}'" in issue and "installation and PATH" in issue
            for issue in issues
        )
    demuxer_tools = {
        invocation.args[0]
        for invocation in no_tool_processes.await_args_list
        if invocation.args[-1] == "-demuxers"
    }
    assert demuxer_tools == {"ffmpeg", "ffprobe"}


def test_dependency_issues_uses_both_custom_executable_paths(monkeypatch, tmp_path):
    tools = {
        "ffmpeg": str(tmp_path / "custom build" / "ffmpeg"),
        "ffprobe": str(tmp_path / "custom build" / "ffprobe"),
    }
    capture = AsyncMock(
        side_effect=[DEMUXERS, DVDVIDEO_HELP, DEMUXERS, DVDVIDEO_HELP, ENCODERS, FILTERS, MUXERS]
    )
    monkeypatch.setattr(ffmpeg, "run_capture", capture)

    assert asyncio.run(ffmpeg.dependency_issues(**tools)) == []

    assert capture.await_args_list == [
        call([tools["ffmpeg"], "-hide_banner", "-demuxers"]),
        call([tools["ffmpeg"], "-hide_banner", "-h", "demuxer=dvdvideo"]),
        call([tools["ffprobe"], "-hide_banner", "-demuxers"]),
        call([tools["ffprobe"], "-hide_banner", "-h", "demuxer=dvdvideo"]),
        call([tools["ffmpeg"], "-hide_banner", "-encoders"]),
        call([tools["ffmpeg"], "-hide_banner", "-filters"]),
        call([tools["ffmpeg"], "-hide_banner", "-muxers"]),
    ]
