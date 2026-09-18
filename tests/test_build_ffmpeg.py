"""exercise local build orchestration without compiling or accessing the network."""

import hashlib
import importlib.util
import io
import signal
import subprocess
import tarfile
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_ffmpeg.py"
SPEC = importlib.util.spec_from_file_location("build_ffmpeg", SCRIPT)
assert SPEC and SPEC.loader
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def archive_bytes(entries=None):
    if entries is None:
        entries = [(f"ffmpeg-{builder.VERSION}/configure", tarfile.REGTYPE, b"configure fixture")]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as bundle:
        for name, kind, content in entries:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.mode = 0o6755
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info.linkname = "../../outside"
            info.size = len(content) if kind == tarfile.REGTYPE else 0
            bundle.addfile(info, io.BytesIO(content) if info.size else None)
    return buffer.getvalue()


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "project ; space"
    root.mkdir()
    monkeypatch.setattr(builder, "PROJECT_ROOT", root)
    monkeypatch.setattr(builder.platform, "system", lambda: "Linux")
    monkeypatch.setattr(builder.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(builder.shutil, "which", lambda name, path=None: f"/tools/{name}")
    popen = Mock(side_effect=AssertionError("unexpected subprocess"))
    opener = Mock(side_effect=AssertionError("unexpected network"))
    monkeypatch.setattr(builder.subprocess, "Popen", popen)
    monkeypatch.setattr(builder.urllib.request, "build_opener", opener)
    return SimpleNamespace(
        root=root,
        work=root / ".build" / f"ffmpeg-{builder.VERSION}",
        prefix=root / ".tools" / "ffmpeg",
        popen=popen,
        opener=opener,
    )


@pytest.fixture
def backend(sandbox, monkeypatch):
    data = archive_bytes()
    monkeypatch.setattr(builder, "SHA256", hashlib.sha256(data).hexdigest())
    opener = Mock()
    opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(data)
    sandbox.opener.side_effect = None
    sandbox.opener.return_value = opener
    tables = {
        ("ffmpeg", "-demuxers"): " D  dvdvideo dvd input\n",
        ("ffprobe", "-demuxers"): " D  dvdvideo dvd input\n",
        ("ffmpeg", "-encoders"): " V..... libx264 h264\n A..... aac audio\n",
        ("ffmpeg", "-filters"): " TS. bwdif deinterlace\n",
        ("ffmpeg", "-muxers"): " E mp4 mp4 output\n",
        ("ffmpeg", "demuxer=dvdvideo"): " -preindex <boolean> preindex title\n",
        ("ffprobe", "demuxer=dvdvideo"): " -preindex <boolean> preindex title\n",
    }

    def execute(command, **kwargs):
        kwargs["log"].write("mock build output\n")
        if Path(command[0]).name == "configure":
            (kwargs["cwd"] / "Makefile").write_text("mock makefile")
        elif command[0] == "make" and "install" in command:
            stage = Path(
                next(arg.removeprefix("DESTDIR=") for arg in command if arg.startswith("DESTDIR="))
            )
            installed = stage / sandbox.prefix.relative_to(sandbox.prefix.anchor) / "bin"
            installed.mkdir(parents=True)
            for tool in ("ffmpeg", "ffprobe"):
                (installed / tool).write_text("mock binary")
        return tables.get((Path(command[0]).name, command[-1]), "")

    runner = Mock(side_effect=execute)
    monkeypatch.setattr(builder, "run", runner)
    sandbox.runner, sandbox.tables, sandbox.download = runner, tables, opener.open
    return sandbox


def test_old_python_is_rejected_without_side_effects(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(builder.sys, "version_info", (3, 10, 0))
    assert builder.main([]) == 1
    assert "requires python >=3.11" in capsys.readouterr().err
    assert not list(sandbox.root.iterdir())


def test_official_pin():
    assert builder.VERSION == "9.0.1"
    assert builder.URL == "https://ffmpeg.org/releases/ffmpeg-9.0.1.tar.xz"
    assert builder.SHA256 == "cf38e0e28c7e5605942c4a77755349b0145804a397af37eb1fb4c77cb237f635"


@pytest.mark.parametrize("jobs", [[], ["--jobs", "3"]])
def test_dry_run_is_read_only_and_describes_local_plan(sandbox, capsys, jobs):
    assert builder.main(["--dry-run", *jobs]) == 0
    output = capsys.readouterr().out
    assert str(sandbox.prefix) in output
    assert str(sandbox.work) in output
    assert "make -j3" in output if jobs else "make -j1" in output
    assert builder.URL in output and builder.SHA256 in output
    assert "verify" in output and "preindex" in output
    assert "staging" in output and "dynamically linked" in output
    assert not list(sandbox.root.iterdir())
    sandbox.popen.assert_not_called()
    sandbox.opener.assert_not_called()


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "many"])
def test_invalid_jobs_have_no_side_effects(sandbox, value, capsys):
    with pytest.raises(SystemExit) as exc:
        builder.main(["--jobs", value])
    assert exc.value.code == 2
    assert "greater than zero" in capsys.readouterr().err
    assert not list(sandbox.root.iterdir())
    sandbox.popen.assert_not_called()


def test_help(sandbox, capsys):
    with pytest.raises(SystemExit) as exc:
        builder.main(["--help"])
    assert exc.value.code == 0
    assert "--dry-run" in capsys.readouterr().out
    assert not list(sandbox.root.iterdir())


def test_unsupported_system(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(builder.platform, "system", lambda: "Windows")
    assert builder.main([]) == 1
    assert "unsupported os" in capsys.readouterr().err
    assert not list(sandbox.root.iterdir())


def test_build_commands_paths_environment_and_verification(backend):
    assert builder.main(["--jobs", "2"]) == 0
    commands = [item.args[0] for item in backend.runner.call_args_list]
    configure = next(command for command in commands if Path(command[0]).name == "configure")
    assert configure == [
        str(backend.work / f"ffmpeg-{builder.VERSION}" / "configure"),
        f"--prefix={backend.prefix}",
        "--enable-gpl",
        "--enable-libdvdnav",
        "--enable-libdvdread",
        "--enable-libx264",
        "--enable-demuxer=dvdvideo",
        "--disable-ffplay",
        "--disable-doc",
        "--disable-debug",
        "--disable-autodetect",
        "--disable-shared",
        "--enable-static",
    ]
    assert commands[:1] == [["pkg-config", "--exists", "dvdnav", "dvdread", "x264"]]
    assert ["make", "-j2"] in commands
    install = next(command for command in commands if "install" in command)
    assert install[:3] == ["make", "-j2", "install"]
    assert Path(install[3].removeprefix("DESTDIR=")).is_relative_to(backend.work)
    assert len([command for command in commands if "-hide_banner" in command]) == 7
    for item in backend.runner.call_args_list:
        assert item.kwargs["cwd"] == backend.work / "build"
        env = item.kwargs["env"]
        assert env["TMPDIR"] == env["TMP"] == env["TEMP"] == str(backend.work / "tmp")
        assert env["MAKEFLAGS"] == env["MFLAGS"] == ""
        if item.args[0][0] == "make":
            assert item.kwargs["timeout"] >= 24 * 60 * 60
    assert (backend.prefix / "bin/ffmpeg").read_text() == "mock binary"
    assert (backend.prefix / "bin/ffprobe").exists()
    assert (backend.prefix / builder.MARKER).read_text() == builder.STAMP
    assert "mock build output" in (backend.work / "build.log").read_text()
    backend.download.assert_called_once_with(builder.URL, timeout=60)
    backend.popen.assert_not_called()


def test_rerun_reuses_source_configuration_and_download_and_retains_previous(backend):
    assert builder.main([]) == 0
    source = backend.work / f"ffmpeg-{builder.VERSION}" / "configure"
    modified = source.stat().st_mtime_ns
    (backend.prefix / "keep.txt").write_text("previous build")
    first_log = (backend.work / "build.log").stat().st_size
    assert builder.main([]) == 0
    commands = [item.args[0] for item in backend.runner.call_args_list]
    assert sum(Path(command[0]).name == "configure" for command in commands) == 1
    assert commands.count(["make", "-j1"]) == 2
    assert source.stat().st_mtime_ns == modified
    backend.download.assert_called_once()
    backups = list(backend.work.glob("install-*/previous-ffmpeg/keep.txt"))
    assert len(backups) == 1 and backups[0].read_text() == "previous build"
    assert (backend.work / "build.log").stat().st_size > first_log


def test_interrupted_make_resumes_without_reconfigure(backend, capsys):
    execute = backend.runner.side_effect

    def interrupt(command, **kwargs):
        if command == ["make", "-j1"]:
            raise KeyboardInterrupt
        return execute(command, **kwargs)

    backend.runner.side_effect = interrupt
    assert builder.main([]) == 130
    assert "files retained" in capsys.readouterr().err
    assert (backend.work / "build/.configured").exists()
    assert not backend.prefix.exists()
    backend.runner.side_effect = execute
    assert builder.main([]) == 0
    assert sum(Path(c.args[0][0]).name == "configure" for c in backend.runner.call_args_list) == 1


@pytest.mark.parametrize("stage", ["configure", "make", "install"])
def test_command_failure_retains_log_and_does_not_activate(backend, stage, capsys):
    execute = backend.runner.side_effect

    def fail(command, **kwargs):
        current = "install" if "install" in command else Path(command[0]).name
        if current == stage:
            raise builder.BuildError("simulated command failure")
        return execute(command, **kwargs)

    backend.runner.side_effect = fail
    assert builder.main([]) == 1
    error = capsys.readouterr().err
    assert "simulated command failure" in error
    assert str(backend.work / "build.log") in error
    assert "ffbuild/config.log" in error
    assert backend.work.exists() and not backend.prefix.exists()
    assert (backend.work / "build/.configured").exists() == (stage != "configure")


@pytest.mark.parametrize("failure", [OSError("rename failed"), KeyboardInterrupt()])
def test_activation_failure_restores_previous_prefix(backend, monkeypatch, failure):
    assert builder.main([]) == 0
    (backend.prefix / "keep").write_text("working installation")
    rename = Path.rename

    def fail_activation(path, target):
        if target == backend.prefix and path.name != "previous-ffmpeg":
            raise failure
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_activation)
    assert builder.main([]) == (130 if isinstance(failure, KeyboardInterrupt) else 1)
    assert (backend.prefix / "keep").read_text() == "working installation"
    assert len(list(backend.work.glob("install-*"))) == 2


def test_changed_configuration_is_not_reused(backend, capsys):
    assert builder.main([]) == 0
    (backend.work / "build/.configured").write_text("another configuration")
    backend.runner.reset_mock()
    assert builder.main([]) == 1
    assert "move" in capsys.readouterr().err
    assert all(item.args[0][0] != "make" for item in backend.runner.call_args_list)


@pytest.mark.parametrize("cached", [False, True])
def test_bad_checksum_stops_before_extraction(backend, monkeypatch, cached, capsys):
    if cached:
        builder.owned(backend.work, create=True)
        (backend.work / f"ffmpeg-{builder.VERSION}.tar.xz").write_bytes(b"corrupt cache")
    else:
        monkeypatch.setattr(builder, "SHA256", "0" * 64)
    assert builder.main([]) == 1
    assert "checksum mismatch" in capsys.readouterr().err
    assert not (backend.work / f"ffmpeg-{builder.VERSION}").exists()
    assert not backend.prefix.exists()
    assert all(Path(item.args[0][0]).name != "configure" for item in backend.runner.call_args_list)
    if cached:
        backend.download.assert_not_called()
    else:
        assert list(backend.work.glob("download-*.part"))


def test_cache_is_verified_even_when_source_already_exists(backend, capsys):
    assert builder.main([]) == 0
    (backend.work / f"ffmpeg-{builder.VERSION}.tar.xz").write_bytes(b"damaged")
    backend.runner.reset_mock()
    assert builder.main([]) == 1
    assert "checksum mismatch" in capsys.readouterr().err
    assert all(item.args[0][0] != "make" for item in backend.runner.call_args_list)


@pytest.mark.parametrize(
    "name,kind",
    [
        ("/absolute", tarfile.REGTYPE),
        ("../outside", tarfile.REGTYPE),
        ("ffmpeg-9.0.1/../../outside", tarfile.REGTYPE),
        ("other/configure", tarfile.REGTYPE),
        ("ffmpeg-9.0.1/dir/../file", tarfile.REGTYPE),
        ("ffmpeg-9.0.1/dir\\file", tarfile.REGTYPE),
        ("ffmpeg-9.0.1/link", tarfile.SYMTYPE),
        ("ffmpeg-9.0.1/link", tarfile.LNKTYPE),
        ("ffmpeg-9.0.1/device", tarfile.CHRTYPE),
        ("ffmpeg-9.0.1/device", tarfile.BLKTYPE),
        ("ffmpeg-9.0.1/pipe", tarfile.FIFOTYPE),
    ],
)
def test_tar_rejects_unsafe_members_before_writing(sandbox, monkeypatch, name, kind):
    builder.owned(sandbox.work, create=True)
    data = archive_bytes(
        [
            ("ffmpeg-9.0.1/configure", tarfile.REGTYPE, b"safe file"),
            (name, kind, b"bad file"),
        ]
    )
    archive = sandbox.work / "archive.tar.xz"
    archive.write_bytes(data)
    monkeypatch.setattr(builder, "SHA256", hashlib.sha256(data).hexdigest())
    with pytest.raises(builder.BuildError, match="unsafe archive member"):
        builder.source_tree(archive, sandbox.work)
    assert not (sandbox.work / f"ffmpeg-{builder.VERSION}").exists()


def test_safe_tar_extraction_preserves_executable_not_special_mode(backend):
    builder.owned(backend.work, create=True)
    archive = builder.download(backend.work)
    source = builder.source_tree(archive, backend.work)
    assert (source / "configure").read_bytes() == b"configure fixture"
    assert (source / "configure").stat().st_mode & 0o7777 == 0o755
    assert (source / builder.MARKER).read_text() == builder.STAMP


def test_https_required_for_initial_request_and_redirect(sandbox, monkeypatch):
    builder.owned(sandbox.work, create=True)
    monkeypatch.setattr(builder, "URL", "http://ffmpeg.org/releases/file.tar.xz")
    with pytest.raises(builder.BuildError, match="requires https"):
        builder.download(sandbox.work)
    sandbox.opener.assert_not_called()
    request = urllib.request.Request("https://ffmpeg.org/releases/file.tar.xz")
    for destination in ("http://example.com/file", "file:///etc/passwd", "ftp://example.com/file"):
        with pytest.raises(builder.BuildError, match="non-https"):
            builder.HTTPSOnly().redirect_request(request, None, 302, "redirect", {}, destination)
    redirect = builder.HTTPSOnly().redirect_request(
        request, None, 302, "redirect", {}, "https://example.com/file"
    )
    assert redirect.full_url == "https://example.com/file"


def test_download_failure_retains_partial_and_does_not_activate(backend, capsys):
    backend.download.side_effect = OSError("connection lost")
    assert builder.main([]) == 1
    assert "connection lost" in capsys.readouterr().err
    assert list(backend.work.glob("download-*.part"))
    assert not backend.prefix.exists()


@pytest.mark.parametrize("path", [".build", ".tools", ".tools/ffmpeg", ".build/ffmpeg-9.0.1"])
@pytest.mark.parametrize("dangling", [False, True])
def test_symlink_control_paths_are_refused(sandbox, tmp_path, path, dangling, capsys):
    outside = tmp_path / "outside"
    if not dangling:
        outside.mkdir()
    link = sandbox.root / path
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    assert builder.main([]) == 1
    assert "symlink" in capsys.readouterr().err
    assert not outside.exists() if dangling else not list(outside.iterdir())
    sandbox.popen.assert_not_called()
    sandbox.opener.assert_not_called()


@pytest.mark.parametrize(
    "relative", ["build", "build.log", "tmp", "ffmpeg-9.0.1.tar.xz", ".build-ffmpeg"]
)
def test_nested_symlinks_cannot_redirect_build_writes(sandbox, tmp_path, relative, capsys):
    builder.owned(sandbox.work, create=True)
    path = sandbox.work / relative
    if path.exists():
        path.unlink()
    path.symlink_to(tmp_path / "outside")
    assert builder.main([]) == 1
    assert "symlink" in capsys.readouterr().err
    sandbox.popen.assert_not_called()


@pytest.mark.parametrize("relative", [".tools/ffmpeg", ".build/ffmpeg-9.0.1"])
def test_unrecognized_directories_are_preserved(sandbox, relative, capsys):
    directory = sandbox.root / relative
    directory.mkdir(parents=True)
    (directory / "keep").write_text("user files")
    assert builder.main([]) == 1
    assert "unrecognized directory" in capsys.readouterr().err
    assert (directory / "keep").read_text() == "user files"
    sandbox.popen.assert_not_called()


def test_unrecognized_build_directory_is_preserved(backend, capsys):
    builder.owned(backend.work, create=True)
    directory = backend.work / "build"
    directory.mkdir()
    (directory / "keep").write_text("user files")
    assert builder.main([]) == 1
    assert "unrecognized directory" in capsys.readouterr().err
    assert (directory / "keep").read_text() == "user files"
    backend.runner.assert_not_called()


def test_local_rejects_parent_traversal(sandbox):
    with pytest.raises(builder.BuildError, match="outside the project"):
        builder.local(sandbox.root / ".." / "outside")


def test_partial_extraction_is_not_reused_or_deleted(backend, capsys):
    builder.owned(backend.work, create=True)
    source = backend.work / f"ffmpeg-{builder.VERSION}"
    source.mkdir()
    (source / "keep").write_text("partial extraction")
    assert builder.main([]) == 1
    assert "unrecognized directory" in capsys.readouterr().err
    assert (source / "keep").read_text() == "partial extraction"


@pytest.mark.parametrize("tool", ["cc", "make", "pkg-config", "nasm"])
def test_missing_preflight_tools(sandbox, monkeypatch, tool):
    monkeypatch.setattr(builder.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        builder.shutil, "which", lambda name, path=None: None if name == tool else name
    )
    execute = Mock()
    with pytest.raises(builder.BuildError, match=tool):
        builder.preflight("Linux", {"PATH": "/tools"}, execute)
    execute.assert_not_called()


@pytest.mark.parametrize("machine", ["aarch64", "arm64", "armv7l"])
def test_arm_does_not_require_nasm(sandbox, monkeypatch, machine):
    monkeypatch.setattr(builder.platform, "machine", lambda: machine)
    monkeypatch.setattr(
        builder.shutil, "which", lambda name, path=None: None if name == "nasm" else name
    )
    execute = Mock()
    builder.preflight("Linux", {"PATH": "/tools"}, execute)
    execute.assert_called_once_with(["pkg-config", "--exists", "dvdnav", "dvdread", "x264"])


def test_missing_development_libraries(sandbox):
    execute = Mock(side_effect=builder.BuildError("exit 1"))
    with pytest.raises(builder.BuildError, match="dvdnav/dvdread/x264 development libraries"):
        builder.preflight("Linux", {"PATH": "/tools"}, execute)


@pytest.mark.parametrize("homebrew", ["/usr/local", "/custom/homebrew"])
def test_macos_queries_formula_prefixes_and_preserves_environment(sandbox, monkeypatch, homebrew):
    env = {"PATH": "/user/bin", "PKG_CONFIG_PATH": "/user/pkgconfig"}
    calls = []

    def execute(command, **kwargs):
        calls.append(command)
        return f"{homebrew}/opt/{command[-1]}\n" if command[0] == "brew" else ""

    which = Mock(return_value="/tools/command")
    monkeypatch.setattr(builder.shutil, "which", which)
    builder.preflight("Darwin", env, execute)
    assert calls[:-1] == [
        ["brew", "--prefix", package] for package in ("pkgconf", "libdvdnav", "libdvdread", "x264")
    ]
    assert env["PATH"] == f"{homebrew}/opt/pkgconf/bin:/user/bin"
    assert env["PKG_CONFIG_PATH"].endswith(":/user/pkgconfig")
    for package in ("libdvdnav", "libdvdread", "x264"):
        assert f"{homebrew}/opt/{package}/lib/pkgconfig" in env["PKG_CONFIG_PATH"]
    assert env["HOMEBREW_NO_AUTO_UPDATE"] == "1"
    assert call("pkg-config", path=env["PATH"]) in which.call_args_list


@pytest.mark.parametrize("prefix", ["", "relative/path", "/prefix\nwarning"])
def test_macos_rejects_invalid_formula_prefix(sandbox, prefix):
    with pytest.raises(builder.BuildError, match="invalid homebrew prefix"):
        builder.preflight("Darwin", {}, Mock(return_value=prefix))


def test_macos_missing_brew_is_actionable(sandbox, monkeypatch):
    monkeypatch.setattr(builder.shutil, "which", lambda *args, **kwargs: None)
    with pytest.raises(builder.BuildError, match="missing brew"):
        builder.preflight("Darwin", {}, Mock())


@pytest.mark.parametrize(
    "key",
    [
        ("ffmpeg", "-demuxers"),
        ("ffprobe", "-demuxers"),
        ("ffmpeg", "-encoders"),
        ("ffmpeg", "-filters"),
        ("ffmpeg", "-muxers"),
        ("ffmpeg", "demuxer=dvdvideo"),
        ("ffprobe", "demuxer=dvdvideo"),
    ],
)
def test_missing_features_never_replace_previous_prefix(backend, key, capsys):
    assert builder.main([]) == 0
    (backend.prefix / "keep").write_text("working installation")
    backend.tables[key] = "unknown format 'dvdvideo'.\n"
    assert builder.main([]) == 1
    assert "missing" in capsys.readouterr().err
    assert (backend.prefix / "keep").read_text() == "working installation"
    assert len(list(backend.work.glob("install-*"))) == 2


@pytest.mark.parametrize(
    "listing",
    [
        " D dvdvideo_extra another demuxer\n D other dvdvideo compatibility\n",
        "unknown format 'dvdvideo'.\n",
        "",
    ],
)
def test_feature_names_must_match_exact_table_entries(sandbox, listing):
    with pytest.raises(builder.BuildError, match="missing dvdvideo"):
        builder.verify(sandbox.prefix, Mock(return_value=listing))


def test_each_encoder_is_required(backend):
    backend.tables[("ffmpeg", "-encoders")] = " V..... libx264 h264\n"
    assert builder.main([]) == 1
    assert not backend.prefix.exists()


@pytest.fixture
def process_mock(sandbox, monkeypatch):
    process = Mock(pid=12345, returncode=0)
    process.stdout = Mock()
    sandbox.popen.side_effect = None
    sandbox.popen.return_value = process
    selector = Mock()
    selector.get_map.side_effect = [True, True, False]
    selector.select.return_value = [(SimpleNamespace(fd=12, fileobj=process.stdout), 1)]
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=selector)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(builder.selectors, "DefaultSelector", factory)
    monkeypatch.setattr(builder.os, "read", Mock(side_effect=[b"build output\n", b""]))
    killpg = Mock()
    monkeypatch.setattr(builder.os, "killpg", killpg)
    return SimpleNamespace(process=process, selector=selector, killpg=killpg)


def test_runner_streams_logs_and_uses_no_shell(sandbox, process_mock, capsys):
    log = io.StringIO()
    env = {"PATH": "/tools"}
    command = ["make", "-j1", "literal ; argument"]
    assert (
        builder.run(command, cwd=sandbox.root, env=env, log=log, capture=True) == "build output\n"
    )
    assert "build output" in log.getvalue() and "build output" in capsys.readouterr().out
    sandbox.popen.assert_called_once_with(
        command,
        cwd=sandbox.root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=False,
        start_new_session=True,
    )
    process_mock.killpg.assert_not_called()
    process_mock.process.stdout.close.assert_called_once()


def test_runner_nonzero_exit_is_actionable(sandbox, process_mock):
    process_mock.process.returncode = 7
    with pytest.raises(builder.BuildError, match=r"command failed \(exit 7\): make"):
        builder.run(["make"], cwd=sandbox.root)


def test_runner_spawn_error(sandbox):
    sandbox.popen.side_effect = FileNotFoundError("missing compiler")
    with pytest.raises(builder.BuildError, match="could not start cc"):
        builder.run(["cc"], cwd=sandbox.root)


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), subprocess.TimeoutExpired(["make"], 1)])
def test_runner_cleans_process_group_on_interrupt_or_timeout(sandbox, process_mock, failure):
    process_mock.selector.select.side_effect = failure
    expected = KeyboardInterrupt if isinstance(failure, KeyboardInterrupt) else builder.BuildError
    with pytest.raises(expected):
        builder.run(["make"], cwd=sandbox.root, timeout=1)
    assert process_mock.killpg.call_args_list == [
        call(12345, signal.SIGTERM),
        call(12345, signal.SIGKILL),
    ]
    process_mock.process.stdout.close.assert_called_once()


def test_runner_deadline_covers_silent_process(sandbox, process_mock, monkeypatch):
    monkeypatch.setattr(builder.time, "monotonic", Mock(side_effect=[0, 2]))
    with pytest.raises(builder.BuildError, match="timed out after 1s"):
        builder.run(["make"], cwd=sandbox.root, timeout=1)
    process_mock.killpg.assert_any_call(12345, signal.SIGKILL)


def test_stop_escalates_when_process_ignores_termination(sandbox, process_mock):
    process_mock.process.wait.side_effect = [subprocess.TimeoutExpired(["make"], 5), 0]
    builder.stop(process_mock.process)
    assert process_mock.killpg.call_args_list == [
        call(12345, signal.SIGTERM),
        call(12345, signal.SIGKILL),
    ]
    assert process_mock.process.wait.call_count == 2


def test_stop_tolerates_already_exited_process_group(sandbox, process_mock):
    process_mock.killpg.side_effect = ProcessLookupError
    builder.stop(process_mock.process)
    assert process_mock.process.wait.call_count == 2
