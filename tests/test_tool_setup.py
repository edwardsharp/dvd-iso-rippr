"""read-only tool selection and platform guidance without builds or downloads."""

import asyncio
import os
import shlex
import socket
import subprocess
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from dvd_ripper import tool_setup


@pytest.fixture(autouse=True)
def project_root(tmp_path, monkeypatch):
    root = tmp_path / "checkout with spaces ; quote's"
    root.mkdir()
    monkeypatch.setattr(tool_setup, "PROJECT_ROOT", root)
    return root


@pytest.fixture(autouse=True)
def no_external_actions(monkeypatch):
    blocked = []
    for module, name in (
        (subprocess, "Popen"),
        (subprocess, "run"),
        (os, "system"),
        (urllib.request, "urlopen"),
        (urllib.request, "urlretrieve"),
        (urllib.request, "build_opener"),
        (socket, "create_connection"),
    ):
        action = Mock(
            side_effect=AssertionError("tool setup must not run commands or download files")
        )
        monkeypatch.setattr(module, name, action)
        blocked.append(action)
    for name in ("create_subprocess_exec", "create_subprocess_shell"):
        action = AsyncMock(side_effect=AssertionError("tool setup must not launch subprocesses"))
        monkeypatch.setattr(asyncio, name, action)
        blocked.append(action)
    yield
    for action in blocked:
        action.assert_not_called()


@pytest.fixture
def local_bin(project_root):
    directory = project_root / ".tools" / "ffmpeg" / "bin"
    directory.mkdir(parents=True)
    return directory


@pytest.mark.parametrize("layout", ["absent", "empty", "unrelated-files"])
def test_select_tools_falls_back_to_path_without_local_pair(layout, project_root):
    directory = project_root / ".tools" / "ffmpeg" / "bin"
    if layout != "absent":
        directory.mkdir(parents=True)
    if layout == "unrelated-files":
        (directory / "build.log").touch()

    before = set(project_root.rglob("*"))
    assert tool_setup.select_tools() == ("ffmpeg", "ffprobe")
    assert set(project_root.rglob("*")) == before


def test_select_tools_prefers_complete_local_pair_independent_of_cwd(
    local_bin, tmp_path, monkeypatch
):
    pair = local_bin / "ffmpeg", local_bin / "ffprobe"
    for path in pair:
        path.touch()
    other = tmp_path / "another checkout"
    decoy = other / ".tools" / "ffmpeg" / "bin"
    decoy.mkdir(parents=True)
    (decoy / "ffmpeg").touch()
    monkeypatch.chdir(other)

    selected = tool_setup.select_tools()

    assert selected == tuple(str(path) for path in pair)
    assert all(Path(path).is_absolute() for path in selected)


def test_select_tools_ignores_pair_in_current_working_directory(
    project_root, tmp_path, monkeypatch
):
    other = tmp_path / "another checkout"
    decoy = other / ".tools" / "ffmpeg" / "bin"
    decoy.mkdir(parents=True)
    for name in ("ffmpeg", "ffprobe"):
        (decoy / name).touch()
    monkeypatch.chdir(other)

    assert tool_setup.select_tools() == ("ffmpeg", "ffprobe")
    assert not list(project_root.iterdir())


@pytest.mark.parametrize("tool", ["ffmpeg", "ffprobe"])
@pytest.mark.parametrize("problem", ["missing", "dangling", "directory"])
def test_select_tools_rejects_incomplete_local_pair(tool, problem, local_bin):
    for name in ("ffmpeg", "ffprobe"):
        path = local_bin / name
        if name != tool:
            path.touch()
        elif problem == "dangling":
            path.symlink_to(local_bin / "missing-target")
        elif problem == "directory":
            path.mkdir()

    with pytest.raises(ValueError) as error:
        tool_setup.select_tools()

    message = str(error.value)
    assert "incomplete local ffmpeg build" in message
    assert str(local_bin) in message
    assert "both ffmpeg and ffprobe are needed" in message
    assert "--setup-ffmpeg" in message
    assert "specify both tool paths" in message


@pytest.mark.parametrize("tools", [("ffmpeg",), ("ffprobe",), ("ffmpeg", "ffprobe")])
def test_select_tools_does_not_treat_dangling_symlinks_as_absent(tools, local_bin):
    for name in tools:
        (local_bin / name).symlink_to(local_bin / f"missing-{name}")

    with pytest.raises(ValueError, match="incomplete local ffmpeg build"):
        tool_setup.select_tools()


def test_select_tools_accepts_complete_symlink_pair(local_bin, tmp_path):
    for name in ("ffmpeg", "ffprobe"):
        target = tmp_path / f"installed-{name}"
        target.touch()
        (local_bin / name).symlink_to(target)

    assert tool_setup.select_tools() == (str(local_bin / "ffmpeg"), str(local_bin / "ffprobe"))


@pytest.mark.parametrize("layout", ["absent", "complete", "partial", "dangling"])
@pytest.mark.parametrize("overrides", ["both", "ffmpeg", "ffprobe"])
def test_explicit_overrides_bypass_local_discovery(layout, overrides, local_bin, tmp_path):
    if layout in {"complete", "partial"}:
        (local_bin / "ffmpeg").touch()
    if layout == "complete":
        (local_bin / "ffprobe").touch()
    if layout == "dangling":
        (local_bin / "ffprobe").symlink_to(local_bin / "missing-ffprobe")
    ffmpeg = str(tmp_path / "custom tools" / "ffmpeg") if overrides != "ffprobe" else None
    ffprobe = str(tmp_path / "custom tools" / "ffprobe") if overrides != "ffmpeg" else None

    assert tool_setup.select_tools(ffmpeg, ffprobe) == (ffmpeg or "ffmpeg", ffprobe or "ffprobe")


def test_explicit_tool_names_and_relative_paths_are_preserved():
    assert tool_setup.select_tools("ffmpeg-dvd", "./tools/ffprobe") == (
        "ffmpeg-dvd",
        "./tools/ffprobe",
    )


@pytest.fixture
def guidance(monkeypatch, project_root):
    system = Mock(return_value="Linux")
    release = Mock(return_value={})
    select = Mock(side_effect=AssertionError("guidance must not inspect installed tools"))
    monkeypatch.setattr(tool_setup.platform, "system", system)
    monkeypatch.setattr(tool_setup.platform, "freedesktop_os_release", release)
    monkeypatch.setattr(tool_setup, "select_tools", select)
    yield SimpleNamespace(system=system, release=release)
    select.assert_not_called()
    assert not list(project_root.iterdir())


def assert_build_guidance(text, root):
    assert f"from the checkout ({root}):" in text
    assert "python3 scripts/build_ffmpeg.py --dry-run" in text
    assert "python3 scripts/build_ffmpeg.py --jobs 1" in text
    assert ".venv/bin/python -m dvd_ripper --check" in text
    assert ".tools/ffmpeg/bin" in text
    assert "system ffmpeg is unchanged" in text
    assert "downloads pinned source" in text
    assert "checks its sha256" in text
    assert "configure + make" in text
    assert "no packages are installed automatically" in text
    assert "pi build can take hours" in text
    assert "software x264 encoding can also be slow" in text
    assert "docs/ffmpeg.md" in text


def test_macos_guidance_lists_manual_prerequisites(guidance, project_root):
    guidance.system.return_value = "Darwin"

    text = tool_setup.setup_instructions()

    assert "macos prerequisites (run yourself):" in text
    assert "xcode-select --install" in text
    assert "brew install pkgconf nasm libdvdnav libdvdread x264" in text
    assert "sudo apt" not in text
    assert_build_guidance(text, project_root)
    guidance.system.assert_called_once_with()
    guidance.release.assert_not_called()


@pytest.mark.parametrize(
    "release",
    [
        {"ID": "debian"},
        {"ID": "raspbian"},
        {"ID": "ubuntu"},
        {"ID": "derivative", "ID_LIKE": "debian"},
        {"ID": "derivative", "ID_LIKE": "raspbian"},
        {"ID": "derivative", "ID_LIKE": "ubuntu debian"},
        {"ID": "derivative", "ID_LIKE": "other\tdebian\nmore"},
        {"ID_LIKE": "ubuntu"},
    ],
)
def test_debian_family_guidance_uses_os_release_id_or_id_like(release, guidance, project_root):
    guidance.release.return_value = release

    text = tool_setup.setup_instructions()

    assert "debian / raspberry pi os prerequisites (run yourself):" in text
    assert "sudo apt update" in text
    assert (
        "sudo apt install build-essential pkg-config nasm libdvdnav-dev "
        "libdvdread-dev libx264-dev ca-certificates"
    ) in text
    assert "brew install" not in text
    assert_build_guidance(text, project_root)
    guidance.system.assert_called_once_with()
    guidance.release.assert_called_once_with()


@pytest.mark.parametrize(
    "release",
    [{}, {"ID": "fedora", "ID_LIKE": "rhel"}, {"ID": "notdebian", "ID_LIKE": "ubuntuish"}],
)
def test_unknown_linux_guidance_does_not_assume_apt(release, guidance, project_root):
    guidance.release.return_value = release

    text = tool_setup.setup_instructions()

    assert "linux: use your package manager" in text
    for prerequisite in ("compiler", "make", "pkg-config", "dvdnav/dvdread/x264", "nasm"):
        assert prerequisite in text
    assert "sudo apt" not in text
    assert "brew install" not in text
    assert_build_guidance(text, project_root)
    guidance.release.assert_called_once_with()


def test_unreadable_os_release_falls_back_to_generic_linux_guidance(guidance, project_root):
    guidance.release.side_effect = OSError("os release is unavailable")

    text = tool_setup.setup_instructions()

    assert "linux: use your package manager" in text
    assert "sudo apt" not in text
    assert_build_guidance(text, project_root)
    guidance.release.assert_called_once_with()


@pytest.mark.parametrize("system", ["Windows", "FreeBSD", "unknown"])
def test_unsupported_platform_explains_external_tool_requirements(system, guidance):
    guidance.system.return_value = system

    text = tool_setup.setup_instructions()

    assert "the build helper supports debian / raspberry pi os and macos" in text
    assert "on other systems, supply a suitable build using --ffmpeg and --ffprobe" in text
    assert "both tools need dvdvideo with preindex" in text
    for capability in ("libx264", "aac", "bwdif", "mp4 muxer"):
        assert capability in text
    assert "docs/ffmpeg.md" in text
    for command in ("sudo apt", "brew install", "xcode-select", "python3 scripts/build_ffmpeg.py"):
        assert command not in text
    guidance.system.assert_called_once_with()
    guidance.release.assert_not_called()


def test_guidance_quotes_absolute_helper_path_for_another_directory(
    guidance, project_root, tmp_path, monkeypatch
):
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)

    text = tool_setup.setup_instructions()

    prefix = "from another directory: "
    command = next(
        line.removeprefix(prefix) for line in text.splitlines() if line.startswith(prefix)
    )
    expected_script = project_root / "scripts" / "build_ffmpeg.py"
    assert shlex.split(command) == ["python3", str(expected_script)]
    assert Path(shlex.split(command)[1]).is_absolute()
    assert not list(other.iterdir())
    assert_build_guidance(text, project_root)
