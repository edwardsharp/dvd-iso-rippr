"""build a pinned, project-local ffmpeg; never install system packages."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSION = "9.0.1"
URL = f"https://ffmpeg.org/releases/ffmpeg-{VERSION}.tar.xz"
# release checksum also pinned by homebrew-core's ffmpeg formula.
SHA256 = "cf38e0e28c7e5605942c4a77755349b0145804a397af37eb1fb4c77cb237f635"
STAMP = f"dvd-iso-rippr build_ffmpeg {VERSION} {SHA256}\n"
MARKER = ".build-ffmpeg"
BUILD_TIMEOUT = 72 * 60 * 60
FLAGS = [
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
GUIDANCE = {
    "Linux": "install prerequisites yourself on debian/raspberry pi:\n"
    "  apt install build-essential pkg-config nasm libdvdnav-dev libdvdread-dev "
    "libx264-dev ca-certificates\nnasm is only needed on x86.",
    "Darwin": "install prerequisites yourself on macos:\n  xcode-select --install\n"
    "  brew install pkgconf nasm libdvdnav libdvdread x264",
}


class BuildError(Exception):
    """an actionable build failure."""


def local(path: Path) -> Path:
    """reject redirected control paths, including dangling symlinks."""
    if not path.is_relative_to(PROJECT_ROOT) or ".." in path.parts:
        raise BuildError(f"path is outside the project: {path}")
    for part in (path, *path.parents):
        if part == PROJECT_ROOT:
            break
        if part.is_symlink():
            raise BuildError(f"refusing symlink: {part}")
    return path


def owned(path: Path, *, create: bool = False) -> None:
    local(path)
    marker = local(path / MARKER)
    if path.exists():
        if not path.is_dir() or not marker.is_file() or marker.read_text() != STAMP:
            raise BuildError(f"unrecognized directory: {path}; inspect and move it aside to retry")
        # generated internal links are allowed, but must not redirect writes out of the project.
        for item in path.rglob("*"):
            if item.is_symlink() and not item.resolve().is_relative_to(PROJECT_ROOT):
                raise BuildError(f"symlink escapes the project: {item}")
    elif create:
        path.mkdir(parents=True)
        marker.write_text(STAMP)


def stop(process: subprocess.Popen) -> None:
    """terminate the whole session's process group, including make's children."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # the leader may have exited while a descendant ignored termination.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def run(command, *, cwd=PROJECT_ROOT, env=None, log=None, timeout=60, capture=False):
    """stream merged output to the terminal and log with a bounded process lifetime."""
    text = shlex.join(command)
    print(f"+ {text}", flush=True)
    if log:
        log.write(f"\n+ {text}\n")
        log.flush()
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            start_new_session=True,
        )
    except OSError as exc:
        raise BuildError(f"could not start {text}: {exc}") from exc
    assert process.stdout is not None
    output = []
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                for key, _ in selector.select(min(remaining, 0.2)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    decoded = chunk.decode(errors="replace")
                    sys.stdout.write(decoded)
                    sys.stdout.flush()
                    if log:
                        log.write(decoded)
                        log.flush()
                    if capture:
                        output.append(decoded)
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
    except BaseException as exc:
        stop(process)
        if isinstance(exc, subprocess.TimeoutExpired):
            raise BuildError(f"command timed out after {timeout}s: {text}") from exc
        raise
    finally:
        process.stdout.close()
    if process.returncode:
        raise BuildError(f"command failed (exit {process.returncode}): {text}")
    return "".join(output)


def preflight(system, env, execute):
    if system == "Darwin":
        env.update(HOMEBREW_NO_AUTO_UPDATE="1", HOMEBREW_NO_ANALYTICS="1")
        if not shutil.which("brew", path=env.get("PATH")):
            raise BuildError("missing brew; install homebrew and the listed prerequisites")
        prefixes = {}
        for package in ("pkgconf", "libdvdnav", "libdvdread", "x264"):
            value = execute(["brew", "--prefix", package], capture=True).strip()
            if not Path(value).is_absolute() or "\n" in value:
                raise BuildError(f"invalid homebrew prefix for {package}: {value!r}")
            prefixes[package] = Path(value)
        env["PATH"] = str(prefixes["pkgconf"] / "bin") + os.pathsep + env.get("PATH", "")
        paths = [str(p / sub / "pkgconfig") for p in prefixes.values() for sub in ("lib", "share")]
        env["PKG_CONFIG_PATH"] = os.pathsep.join(paths + [env.get("PKG_CONFIG_PATH", "")])
    tools = ["cc", "make", "pkg-config"]
    if platform.machine().lower() in {"x86_64", "amd64", "i386", "i486", "i586", "i686", "x86"}:
        tools.append("nasm")
    missing = [tool for tool in tools if not shutil.which(tool, path=env.get("PATH"))]
    if missing:
        raise BuildError("missing tools: " + ", ".join(missing))
    try:
        execute(["pkg-config", "--exists", "dvdnav", "dvdread", "x264"])
    except BuildError as exc:
        raise BuildError(
            "missing dvdnav/dvdread/x264 development libraries; "
            "check pkg-config search paths\n" + str(exc)
        ) from exc


class HTTPSOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise BuildError("refusing a non-https download redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def checksum(archive):
    with local(archive).open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != SHA256:
        raise BuildError(f"checksum mismatch: {archive}; inspect and move it aside before retrying")


def download(work):
    archive = local(work / f"ffmpeg-{VERSION}.tar.xz")
    if not archive.exists():
        if urllib.parse.urlsplit(URL).scheme != "https":
            raise BuildError("download requires https")
        opener = urllib.request.build_opener(HTTPSOnly())
        with tempfile.NamedTemporaryFile(
            dir=work, prefix="download-", suffix=".part", delete=False
        ) as out:
            with opener.open(URL, timeout=60) as response:
                deadline = time.monotonic() + 600
                while chunk := response.read(1024 * 1024):
                    if time.monotonic() > deadline:
                        raise BuildError("download timed out after 600s")
                    out.write(chunk)
            partial = Path(out.name)
        checksum(partial)
        partial.rename(archive)
    checksum(archive)
    return archive


def source_tree(archive, work):
    source = local(work / f"ffmpeg-{VERSION}")
    # even a cached archive must be checked before accepting an extracted source tree.
    checksum(archive)
    if source.exists():
        owned(source)
        return source
    with tarfile.open(archive, "r:xz") as bundle:
        members = bundle.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in member.name
                or not path.parts
                or path.parts[0] != source.name
                or not (member.isdir() or member.isfile())
            ):
                raise BuildError(f"unsafe archive member: {member.name!r}")
        source.mkdir()
        # manual regular-file extraction avoids version-dependent tarfile filters and link handling.
        for member in members:
            target = work.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                data = bundle.extractfile(member)
                assert data is not None
                with data, target.open("xb") as out:
                    shutil.copyfileobj(data, out)
                target.chmod(member.mode & 0o755)
    local(source / MARKER).write_text(STAMP)
    return source


def verify(prefix, execute):
    checks = [
        ("ffmpeg", "-demuxers", ("dvdvideo",)),
        ("ffprobe", "-demuxers", ("dvdvideo",)),
        ("ffmpeg", "-encoders", ("libx264", "aac")),
        ("ffmpeg", "-filters", ("bwdif",)),
        ("ffmpeg", "-muxers", ("mp4",)),
    ]
    for tool, option, features in checks:
        listing = execute([str(local(prefix / "bin" / tool)), "-hide_banner", option], capture=True)
        names = {fields[1] for line in listing.splitlines() if len(fields := line.split()) >= 2}
        for feature in features:
            if feature not in names:
                raise BuildError(f"{tool}: missing {feature} in {option}")
    for tool in ("ffmpeg", "ffprobe"):
        listing = execute(
            [str(prefix / "bin" / tool), "-hide_banner", "-h", "demuxer=dvdvideo"], capture=True
        )
        if not any(line.split()[:1] == ["-preindex"] for line in listing.splitlines()):
            raise BuildError(f"{tool}: missing dvdvideo preindex option")


def positive(value):
    try:
        jobs = int(value)
    except ValueError:
        jobs = 0
    if jobs <= 0:
        raise argparse.ArgumentTypeError("jobs must be an integer greater than zero")
    return jobs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--jobs", type=positive, default=1, metavar="n", help="make jobs (default: 1)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the plan without running or writing"
    )
    args = parser.parse_args(argv)
    work = PROJECT_ROOT / ".build" / f"ffmpeg-{VERSION}"
    prefix = PROJECT_ROOT / ".tools" / "ffmpeg"
    build = work / "build"
    logfile = work / "build.log"
    configure = [str(work / f"ffmpeg-{VERSION}" / "configure"), f"--prefix={prefix}", *FLAGS]
    system = platform.system()
    try:
        if sys.version_info < (3, 11):
            raise BuildError("this helper requires python >=3.11")
        if system not in GUIDANCE:
            raise BuildError("unsupported os; use debian/raspberry pi linux or macos")
        owned(work)
        owned(prefix)
        print(
            f"ffmpeg {VERSION}: {URL}\nsha256: {SHA256}\n{GUIDANCE[system]}\n"
            f"source/build/logs: {work}\ninstall prefix: {prefix}\n"
            f"configure: {shlex.join(configure)}\nmake -j{args.jobs}\n"
            "make install with a fresh project-local staging directory as DESTDIR; verify "
            "dvdvideo/preindex in both tools, libx264/aac/bwdif/mp4 before activation.\n"
            "old owned prefixes and failed attempts are retained under .build.\n"
            "rerun to resume make; for a clean rebuild, move the build workspace aside.\n"
            "external package-manager libraries may remain dynamically linked."
        )
        if args.dry_run:
            return 0
        owned(work, create=True)
        owned(build, create=True)
        temporary = local(work / "tmp")
        temporary.mkdir(exist_ok=True)
        env = os.environ.copy()
        env.update(
            TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary), MAKEFLAGS="", MFLAGS=""
        )
        with local(logfile).open("a", encoding="utf-8") as log:

            def execute(command, **kwargs):
                return run(command, cwd=build, env=env, log=log, **kwargs)

            preflight(system, env, execute)
            source_tree(download(work), work)
            configured = local(build / ".configured")
            if not configured.exists():
                execute(configure, timeout=600)
                configured.write_text(shlex.join(configure))
            elif (
                configured.read_text() != shlex.join(configure)
                or not local(build / "Makefile").is_file()
            ):
                raise BuildError(
                    f"configuration changed or incomplete; move {build} aside and rerun"
                )
            execute(["make", f"-j{args.jobs}"], timeout=BUILD_TIMEOUT)
            stage = Path(tempfile.mkdtemp(dir=work, prefix="install-"))
            execute(
                ["make", f"-j{args.jobs}", "install", f"DESTDIR={stage}"], timeout=BUILD_TIMEOUT
            )
            staged_prefix = stage / prefix.relative_to(prefix.anchor)
            verify(staged_prefix, execute)
            local(staged_prefix / MARKER).write_text(STAMP)
            owned(prefix)
            local(prefix.parent).mkdir(parents=True, exist_ok=True)
            previous = stage / "previous-ffmpeg"
            try:
                if prefix.exists():
                    prefix.rename(previous)
                staged_prefix.rename(prefix)
            except BaseException:
                if previous.exists() and not prefix.exists():
                    previous.rename(prefix)
                raise
        print(f"ready: {prefix / 'bin/ffmpeg'} and {prefix / 'bin/ffprobe'}\nlog: {logfile}")
        return 0
    except (BuildError, OSError, tarfile.TarError, ValueError) as exc:
        print(
            f"build error: {exc}\n{GUIDANCE.get(system, '')}\n"
            f"retained files: {work}\nlog: {logfile}\nconfigure diagnostics: {build / 'ffbuild/config.log'}",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print(
            f"build interrupted; subprocesses stopped. files retained: {work}\n"
            f"log: {logfile}\nconfigure diagnostics: {build / 'ffbuild/config.log'}",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    sys.exit(main())
