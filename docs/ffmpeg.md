# local ffmpeg build

ffmpeg uses `configure`, `make`, and `make install`. the helper wraps them with a project-local prefix, a pinned download, sha256 verification, and feature checks.

## debian / raspberry pi os

use a current release with python 3.11+ (bookworm or newer):

```sh
sudo apt update
sudo apt install python3 python3-venv python3-pip \
  build-essential pkg-config nasm ca-certificates \
  libdvdnav-dev libdvdread-dev libx264-dev
```

## macos

with [homebrew](https://brew.sh/) installed:

```sh
# only if command line tools are missing
xcode-select --install

brew install pkgconf nasm libdvdnav libdvdread x264

# only if python 3.11+ is missing
brew install python@3.13
```

## build

from the checkout, on either platform:

```sh
python3 scripts/setup.py --dev
python3 scripts/build_ffmpeg.py --dry-run
python3 scripts/build_ffmpeg.py --jobs 1
.venv/bin/python -m dvd_ripper --check
```

`--dry-run` changes nothing. the build downloads ffmpeg **9.0.1** from ffmpeg.org; it never runs `sudo`, `apt`, or `brew install`.

- binaries: `.tools/ffmpeg/bin/ffmpeg` and `.tools/ffmpeg/bin/ffprobe`
- sources, objects, old builds: `.build/ffmpeg-9.0.1/`
- log: `.build/ffmpeg-9.0.1/build.log`
- configure errors: `.build/ffmpeg-9.0.1/build/ffbuild/config.log`

one build job is the default for low-memory pis; use `--jobs 4` on a machine with enough RAM. allow several GB of disk space and potentially hours on a pi. software x264 encoding is also slow on a pi 3A+; this does not enable hardware encoding.

the app selects the local pair before PATH. no shell configuration or system ffmpeg replacement is needed. dvdnav/dvdread/x264 still come from your package manager: keep those libraries installed. build separately on each machine; a mac binary won't run on a pi.

## verify / troubleshoot

```sh
.venv/bin/python -m dvd_ripper --check
.venv/bin/python -m dvd_ripper --setup-ffmpeg

.tools/ffmpeg/bin/ffmpeg -hide_banner -h demuxer=dvdvideo
.tools/ffmpeg/bin/ffprobe -hide_banner -h demuxer=dvdvideo

# select another pair explicitly
.venv/bin/python -m dvd_ripper --check \
  --ffmpeg /path/to/ffmpeg --ffprobe /path/to/ffprobe
```

checks cover `dvdvideo` + `preindex` in both tools, plus `libx264`, `aac`, `bwdif`, and the `mp4` muxer. a help command reporting an unknown format is a failure even if its exit code is zero.

- **missing libraries:** install the prerequisites above; retry. on macos, the script discovers homebrew library paths.
- **interrupted build:** rerun the same command to resume. files/logs are retained; the previous local install stays active until verification succeeds.
- **clean rebuild:** move `.build/ffmpeg-9.0.1` aside, then rerun. move `.tools/ffmpeg` aside too when changing the pinned version. unrecognized directories and redirected paths are refused rather than deleted.
- **checksum mismatch:** inspect and move the cached archive aside, then retry; don't skip verification.
- **system ffmpeg still selected:** read the paths printed by `--check`; both local binaries must exist. supplying either tool override bypasses local discovery, so supply both.
- **macos compiler cache permission errors in an editor sandbox:** rerun the build from your regular terminal, or grant the compiler's reported cache directory write access. no global FFmpeg install is needed.

unencrypted dvd images only; no decryption or subtitle OCR. this build enables GPL components; check upstream license terms before redistributing.

[upstream build guide](https://ffmpeg.org/platform.html) · [releases](https://ffmpeg.org/download.html)
