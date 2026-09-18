Yes — **Python + FFmpeg is a much better fit** for what you're describing than a giant Bash script.

 And there's one particularly useful development here: modern FFmpeg has a **`dvdvideo` demuxer** that can read DVD ISO files directly, using `libdvdnav`/`libdvdread`. It accepts ISO files and can select individual DVD titles with `-title N`.  FFmpeg+1

 So I would build this as a small Python TUI around FFmpeg rather than extracting all the VOBs first.

 ## The architecture I'd use

 Something roughly like:

```
dvd-ripper/
├── dvd_ripper.py
├── config.toml
└── requirements.txt
```

 Workflow:

```
                    ┌──────────────────┐
ISO(s) ────────────>│ inspect DVD      │
                    │ titles/tracks    │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │ interactive TUI  │
                    │                  │
                    │ ☑ Main feature   │
                    │ ☑ Trailer        │
                    │ ☑ Making of      │
                    │ ☐ Menu           │
                    │                  │
                    │ Audio: English   │
                    │ Subs: English    │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │ FFmpeg           │
                    │ H.264 + AAC      │
                    │ MP4 + faststart  │
                    └────────┬─────────┘
                             │
                             ▼
                    movie.mp4
                    movie.en.srt
```

 The important thing is that **title selection should happen before transcoding**. A DVD isn't really "one video"; it's a collection of titles/PGCs, and the title structure determines the playback sequence. FFmpeg's DVD demuxer specifically exists to handle that rather than simply concatenating VOB files.  FFmpeg

 ### Python wrapper: `ffmpeg-python`

 For the Python side, I'd probably use **`ffmpeg-python`**.

 It gives you both command construction and `ffprobe` parsing:

```
import ffmpeg

info = ffmpeg.probe("movie.iso")
```

 and:

```
(
    ffmpeg
    .input("input.mp4")
    .output("output.mp4", vcodec="libx264", crf=22)
    .run()
)
```

 It can also compile the command without executing it, which is very handy for a tool like this because you can show the user **exactly what will be run** before starting.  GitHub+1

 There's also a newer package called `ffmpeg-wrap` that provides typed `ffprobe` results and a fluent FFmpeg builder. It's interesting, but I'd personally start with `ffmpeg-python` because it's established and straightforward.  PyPI

 ## One complication: discovering DVD titles

 This is the part I'd spend the most effort on.

 Modern FFmpeg can do:

```
ffmpeg -f dvdvideo -title 1 -i movie.iso ...
ffmpeg -f dvdvideo -title 2 -i movie.iso ...
ffmpeg -f dvdvideo -title 3 -i movie.iso ...
```

 and so on. The DVD demuxer supports title selection, chapters, angles, PGCs, etc.  FFmpeg

 But **"title 1 is the movie and everything else is an extra" isn't reliable**.

 A typical DVD might look something like:

```
Title 1    01:42:13    16:9    English 5.1    ← probably movie
Title 2    00:02:31    4:3     English 2.0    ← trailer
Title 3    00:00:42    4:3     English 2.0    ← studio logo
Title 4    00:18:12    16:9    English 2.0    ← making-of
Title 5    00:00:15    4:3     English 2.0    ← menu-ish thing
```

 So the TUI could inspect every title and present something like:

```
┌─ DVD: Blade Runner ──────────────────────────────────────────┐
│                                                               │
│  #   Duration    Video       Audio       Description          │
│                                                               │
│  1   01:57:32    720x480     eng 5.1     ★ likely main       │
│  2   00:03:12    720x480     eng 2.0     trailer              │
│  3   00:07:48    720x480     eng 2.0     featurette           │
│  4   00:01:21    720x480     eng 2.0     deleted scene        │
│  5   00:00:34    720x480     eng 2.0     logo                 │
│                                                               │
│  [Space] select   [A] select all   [M] main only   [Enter] OK│
└───────────────────────────────────────────────────────────────┘
```

 I'd make the **longest title automatically selected as "main"**, but absolutely allow overriding it.

 That heuristic works surprisingly well for normal DVDs, while not pretending that the program can magically understand every bizarre DVD authoring scheme.

 ## Audio selection

 This is another place where Python makes things pleasant.

 After probing a title, you'd get streams along these lines:

```
Stream 0: video
Stream 1: audio, eng, AC3 5.1
Stream 2: audio, fra, AC3 5.1
Stream 3: audio, spa, AC3 2.0
Stream 4: subtitle, eng
Stream 5: subtitle, fra
```

 Then your selection logic can simply be:

```
english_audio = [
    s for s in streams
    if s["codec_type"] == "audio"
    and s.get("tags", {}).get("language") == "eng"
]
```

 `ffprobe` is specifically designed to expose this kind of machine-readable stream/container information, and `ffmpeg-python`'s `probe()` returns it as JSON-derived Python data.  FFmpeg+1

 I'd choose the English audio track with something like:

 1. `eng`
2. highest channel count
3. preferred codec (`ac3` over weird secondary tracks)
4. first remaining candidate

 That gets you the English 5.1 track in most cases.

 ## Subtitles: I'd make these external `.srt`

 I think your instinct here is good: **don't put the subtitles into the MP4 as your primary representation.**

 I'd produce:

```
Blade Runner.mp4
Blade Runner.en.srt
```

 rather than:

```
Blade Runner.mp4
    └── embedded DVD subtitle stream
```

 DVD subtitles are bitmap subtitles, not text. FFmpeg can decode them, but converting them to clean text subtitles requires OCR.

 So there are really two different goals:

 ### Easy/reliable

 Keep the original DVD subtitle stream and put it in the MP4.

 That's technically possible, but it isn't the nice browser/Raspberry Pi solution you're after.

 ### Nice final result

```
movie.mp4
movie.en.srt
```

 which means:

 - browser can display the subtitle
- VLC/mpv/etc. can load it
- you can edit it
- subtitle isn't permanently burned into the video
- no weird DVD subtitle codec required

 But you'd need an OCR step, probably involving something like `vobsub2srt`/OCRmyPDF-style tooling or a dedicated subtitle OCR library.

 **I'd actually make subtitle extraction a second stage of the project.**

 Get:

```
ISO → MP4
```

 working perfectly first.

 Then:

```
ISO → English DVD subtitle images → OCR → SRT
```

 can be added.

 ## H.264 settings for your Raspberry Pi 3A+

 For the target you described, I'd deliberately avoid modern codecs like HEVC/AV1.

 I'd use:

```
Video: H.264 / AVC
Audio: AAC-LC
Container: MP4
```

 Something approximately like:

```
-c:v libx264
-preset medium
-crf 20-23
-profile:v high
-level 4.0
-pix_fmt yuv420p
-c:a aac
-b:a 160k
-movflags +faststart
```

 The `+faststart` is particularly worthwhile for browser playback: FFmpeg moves the MP4 `moov` index toward the beginning of the file, which lets playback start before the whole file has been downloaded.  FFmpeg

 For DVD material specifically, you don't need an enormous bitrate. I'd probably start around **CRF 21–22** and see whether you're happy with the results.

 And I'd preserve the DVD's original resolution rather than scaling everything unnecessarily.

 So a 720×480 NTSC DVD remains roughly:

```
720×480
```

 rather than automatically becoming 1080p.

 ## Deinterlacing is worth thinking about

 This is probably the one encoding decision that matters more than CRF.

 A lot of DVD material is interlaced. If you simply encode it into H.264 without handling that appropriately, you'll get ugly combing on motion.

 I'd have the program detect whether the source appears interlaced and offer:

```
Video processing:

  ● Automatic
  ○ Preserve interlacing
  ○ Deinterlace
```

 For ordinary movie DVDs, I'd probably make **automatic deinterlacing** the default.

 But there's a wrinkle: some film DVDs use telecine rather than straightforward interlacing, so a sophisticated version could eventually detect that too. That's something I'd leave for v2.

 ## The TUI

 I'd use **Textual** for this.

 It would let you have a proper terminal UI without having to write a pile of `curses` code:

```
┌─────────────────────────────────────────────────────────────────┐
│ DVD Ripper                                           1/3        │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│ Source                                                          │
│   /media/dvds/Blade Runner.iso                                  │
│                                                                 │
│ Titles                                                          │
│   ● 1  01:57:32  720x480  ENG 5.1   Main feature              │
│   ○ 2  00:03:12  720x480  ENG 2.0   Trailer                  │
│   ○ 3  00:07:48  720x480  ENG 2.0   Featurette               │
│   ○ 4  00:01:21  720x480  ENG 2.0   Deleted scene             │
│                                                                 │
│ Audio:     English / AC3 5.1                                   │
│ Subtitles: English                                              │
│                                                                 │
│ Output:    /media/movies/Blade Runner/                          │
│                                                                 │
│                       [ Start conversion ]                      │
└─────────────────────────────────────────────────────────────────┘
```

 Then while encoding:

```
Blade Runner — Title 1

███████████████████████████████████████░░░░░░░░  78%

Video    720x480 H.264
Audio    AAC 5.1 → 384 kb/s
Subtitle English → Blade Runner.en.srt

Elapsed     00:41:32
Remaining   00:11:48
Speed       2.1x
```

 And when you've got a directory:

```
dvd-ripper ~/dvd-isos/
```

 it could show:

```
DVDs found:

  ☑ Blade Runner.iso
  ☑ Alien.iso
  ☑ The Thing.iso
  ☐ Weird Old DVD.iso

[Enter] Continue
```

 That would be much nicer than Bash prompts.

 ## One thing I'd _not_ do

 I wouldn't have Python try to become an FFmpeg implementation.

 The Python program should essentially be:

```
             Python
                │
       ┌────────┴─────────┐
       │                  │
   inspect             construct
    DVD                 command
       │                  │
       └────────┬─────────┘
                ▼
             FFmpeg
```

 Python handles:

 - finding ISOs
- TUI
- configuration
- title selection
- stream selection
- naming
- progress display
- error handling
- remembering choices

 FFmpeg handles:

 - DVD demuxing
- decoding
- deinterlacing
- H.264 encoding
- AAC encoding
- MP4 muxing

 That's a very clean division.

 ## One important caveat about FFmpeg itself

 Make sure your FFmpeg build actually has the DVD demuxer enabled.

 The current FFmpeg documentation says the `dvdvideo` demuxer requires **`libdvdnav` and `libdvdread`**, and the FFmpeg build needs to have those enabled. It also explicitly accepts ISO files.  FFmpeg

 You can check:

```
ffmpeg -demuxers | grep dvd
```

 and:

```
ffmpeg -h demuxer=dvdvideo
```

 You want to see `dvdvideo`.

 Also, this is for **unencrypted DVD material**. The FFmpeg DVD demuxer deliberately does not implement DVD decryption.  FFmpeg

 ## So I'd build it in stages

 I'd actually make **v1 quite small**:

 ### v1

```
ISO
 ↓
discover titles
 ↓
show title list
 ↓
user selects titles
 ↓
choose English audio
 ↓
choose English subtitle stream
 ↓
H.264/AAC MP4
```

 with:

```
ffmpeg-python
textual
```

 as the two main Python dependencies.

 Then v2:

```
DVD subtitles → OCR → .srt
```

 Then v3 could add:

 - automatic main-title detection
- automatic interlace detection
- parallel processing of multiple DVDs
- resume failed jobs
- persistent config
- filename metadata
- cover art
- chapter metadata
- dry-run / show commands
- automatic deletion/archiving of completed ISOs

 **I think this is a very good candidate for a \~300–500 line Python program rather than a sprawling shell script.** The underlying FFmpeg invocation can stay remarkably simple; the complexity is really in presenting the DVD structure nicely and making sensible decisions about which streams/titles to use.

 If you want, I can sketch out the actual Python project and give you a **working first version of the Textual TUI**, including ISO discovery, DVD title probing, English audio/subtitle selection, and the generated FFmpeg commands.

  sketch out the actual Python project and give you a working first version of the Textual TUI
