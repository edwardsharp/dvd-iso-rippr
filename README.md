# dvd-iso-rippr

interactive dvd `.iso` / `.img` → H.264/AAC `.mp4`.

requires python 3.11+ and ffmpeg with `dvdvideo`. targets debian / raspberry pi os and macos.

## setup

from the checkout; `--dev` includes test tools:

```sh
python3 --version
python3 scripts/setup.py --dev
. .venv/bin/activate

dvd-ripper --check
```

missing python, build tools, or `dvdvideo`? see [ffmpeg setup](docs/ffmpeg.md).

```sh
dvd-ripper --setup-ffmpeg
python3 scripts/build_ffmpeg.py --jobs 1
dvd-ripper --check
```

the build stays in `.tools/ffmpeg`; the app finds it automatically. system ffmpeg is unchanged.

## run

```sh
dvd-ripper "movie.iso" -o ./movies
dvd-ripper ./dvd-images --recursive -o ./movies
dvd-ripper "movie one.iso" "movie two.img"

# without activation
.venv/bin/python -m dvd_ripper "movie.iso" -o ./movies
```

choose a disc → confirm output → select titles/audio → **encode**. without `-o`, confirm the suggested ISO directory with **use directory** or enter, or type another path.

**main/all** select titles. **show ffmpeg command** is optional. the bottom **encode** button becomes **cancel encoding** (or **cancel scan**) while working. cancelling stops the batch, removes unfinished output, and keeps completed files. **q** quits.

the longest title is selected by default; review it. audio defaults to english, then the first available track, or silent if none exist. stereo AAC is the default. subtitles/OCR and decryption are not implemented.

outputs: `movies/movie/movie - Title 1.mp4`, then `movie - Title 1 (1).mp4`, `(2)`, etc. existing paths are never replaced, even if another job creates a file during encoding. the log shows the final filename; errors appear in a separate panel.

publication needs hard links (ext4/APFS/NTFS, not exFAT/FAT); failed publication keeps the finished file at the recovery path shown in the log.

## manual python setup

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
dvd-ripper --check
```

setup reuses `.venv`; it won't replace a broken or unrelated directory. inspect and move it aside before retrying. a successful python setup can still report missing ffmpeg features.

## tests

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
```

default tests use mocks and generated clips. opt into a real dvd scan and five-second sample (the ISO is unchanged):

```sh
.venv/bin/python -m pytest tests/test_dvd_smoke.py --dvd-iso test-isoz/fffms.ISO -s
```

this needs the local ffmpeg build. sample output goes into pytest's temporary directory.
