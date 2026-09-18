"""scanner tests use only the standard library and never invoke media binaries."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from dvd_ripper.ffmpeg import FFmpegError
from dvd_ripper.models import DVD, Stream, Title
from dvd_ripper.scanner import find_isos, probe_dvd_title, scan_dvd


def video_payload() -> dict:
    return {
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "mpeg2video",
                "width": 720,
                "height": 480,
                "field_order": "tt",
            }
        ],
        "format": {"duration": "123.5"},
    }


def end_of_disc(number: int) -> FFmpegError:
    # ffmpeg dvdvideo_ifo_open(): opt_title > tt_srpt->nr_of_srpts.
    return FFmpegError(
        "ffprobe exited with status 1:\n"
        f"[dvdvideo @ 0x1234abcd] Title {number} not found\n"
        "disc.iso: Stream not found\n"
    )


class FindIsosTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def make_file(self, name: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def test_case_insensitive_suffixes_and_stable_sort(self) -> None:
        expected = [
            self.make_file("alpha.ISO"),
            self.make_file("Beta.iMg"),
            self.make_file("zeta.iso"),
        ]
        self.make_file("ignored.mp4")
        self.make_file("also-ignored.iso.bak")
        (self.root / "directory.iso").mkdir()
        self.assertEqual(find_isos([self.root]), expected)
        self.assertEqual(find_isos(reversed(expected)), expected)

    def test_recursive_is_opt_in(self) -> None:
        top = self.make_file("top.iso")
        nested = self.make_file("nested/disc.IMG")
        deeper = self.make_file("nested/deeper/disc.iso")
        self.assertEqual(find_isos([self.root]), [top])
        self.assertEqual(find_isos([self.root], recursive=True), [deeper, nested, top])

    def test_resolved_deduplication_across_files_directories_and_symlinks(self) -> None:
        image = self.make_file("disc.iso")
        alias = self.root / "alias.img"
        alias.symlink_to(image)
        inputs = (path for path in [alias, self.root, image, self.root / "." / image.name])
        self.assertEqual(find_isos(inputs), [image])

    def test_recursive_does_not_follow_directory_symlink_cycles(self) -> None:
        image = self.make_file("nested/disc.iso")
        (self.root / "nested" / "loop").symlink_to(self.root, target_is_directory=True)
        self.assertEqual(find_isos([self.root], recursive=True), [image])
        self.assertEqual(find_isos([self.root / "nested" / "loop"], recursive=True), [image])

    def test_empty_inputs_and_directories(self) -> None:
        self.assertEqual(find_isos([]), [])
        self.assertEqual(find_isos([self.root]), [])

    def test_missing_explicit_path_is_an_actionable_error(self) -> None:
        missing = self.root / "missing.iso"
        with self.assertRaisesRegex(ValueError, "missing.iso.*") as caught:
            find_isos([missing])
        self.assertIn("exists", str(caught.exception))

    def test_unresolvable_home_directory_is_an_actionable_error(self) -> None:
        with (
            patch.object(Path, "expanduser", side_effect=RuntimeError("Unknown home directory")),
            self.assertRaisesRegex(ValueError, "cannot discover.*Unknown home directory"),
        ):
            find_isos([Path("~/disc.iso")])

    def test_invalid_explicit_path_does_not_return_partial_discovery(self) -> None:
        image = self.make_file("good.iso")
        unsupported = self.make_file("wrong.mp4")
        with self.assertRaisesRegex(ValueError, r"wrong.mp4.*\.iso or \.img"):
            find_isos([image, unsupported])

    def test_broken_symlink_is_an_error(self) -> None:
        broken = self.root / "broken.iso"
        broken.symlink_to(self.root / "missing.iso")
        for inputs in ([broken], [self.root]):
            with self.subTest(inputs=inputs), self.assertRaisesRegex(ValueError, "cannot discover"):
                find_isos(inputs)

    def test_unreadable_image_is_an_error(self) -> None:
        image = self.make_file("unreadable.iso")
        with (
            patch.object(Path, "open", side_effect=PermissionError("Permission denied")),
            self.assertRaisesRegex(ValueError, "permission to read"),
        ):
            find_isos([image])

    def test_walk_errors_are_not_silently_ignored(self) -> None:
        def denied_walk(path, *, onerror):
            onerror(PermissionError(13, "Permission denied", str(path / "private")))
            return iter(())

        with (
            patch("dvd_ripper.scanner.os.walk", side_effect=denied_walk),
            self.assertRaisesRegex(ValueError, "private.*permission to read"),
        ):
            find_isos([self.root], recursive=True)

    def test_nonregular_explicit_path_is_an_error(self) -> None:
        fifo = self.root / "pipe.iso"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, "not a regular disc image file or directory"):
            find_isos([fifo])


class ProbeTitleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.path = Path("Disc name 'quoted'.iso")
        patcher = patch("dvd_ripper.scanner.run_capture", new_callable=AsyncMock)
        self.capture = patcher.start()
        self.addCleanup(patcher.stop)
        self.capture.return_value = json.dumps(video_payload())

    async def test_dvdvideo_command_uses_preindex_json_and_long_bounded_timeout(self) -> None:
        title = await probe_dvd_title(self.path, 7, ffprobe="/custom/bin/ffprobe")
        self.capture.assert_awaited_once_with(
            [
                "/custom/bin/ffprobe",
                "-v",
                "error",
                "-f",
                "dvdvideo",
                "-title",
                "7",
                "-preindex",
                "1",
                "-show_streams",
                "-show_format",
                "-print_format",
                "json",
                "-i",
                str(self.path.resolve()),
            ],
            timeout=180.0,
        )
        self.assertEqual(
            title,
            Title(
                number=7,
                duration=123.5,
                streams=(
                    Stream(
                        index=0,
                        codec_type="video",
                        codec_name="mpeg2video",
                        width=720,
                        height=480,
                        field_order="tt",
                    ),
                ),
            ),
        )

    async def test_all_stream_metadata_and_optional_fields(self) -> None:
        payload = video_payload()
        payload["streams"].extend(
            [
                {
                    "index": 3,
                    "codec_type": "audio",
                    "codec_name": "ac3",
                    "tags": {"language": "eng", "title": "Commentary"},
                    "channels": 6,
                    "channel_layout": "5.1(side)",
                },
                {"index": 4, "codec_type": "subtitle", "codec_name": "dvd_subtitle"},
                {"index": 5, "codec_type": "data"},
            ]
        )
        self.capture.return_value = json.dumps(payload)
        title = await probe_dvd_title(self.path, 1)
        self.assertIsInstance(title.streams, tuple)
        self.assertEqual(
            title.streams[1],
            Stream(
                index=3,
                codec_type="audio",
                codec_name="ac3",
                language="eng",
                title="Commentary",
                channels=6,
                channel_layout="5.1(side)",
            ),
        )
        self.assertEqual(
            title.streams[2],
            Stream(
                index=4,
                codec_type="subtitle",
                codec_name="dvd_subtitle",
            ),
        )
        self.assertEqual(title.streams[3], Stream(index=5, codec_type="data"))

    async def test_missing_null_and_na_durations_remain_unknown(self) -> None:
        for format_data in ({}, {"duration": None}, {"duration": "N/A"}):
            with self.subTest(format_data=format_data):
                payload = video_payload()
                payload["format"] = format_data
                self.capture.return_value = json.dumps(payload)
                self.assertIsNone((await probe_dvd_title(self.path, 1)).duration)

    async def test_missing_format_uses_stream_duration(self) -> None:
        payload = video_payload()
        del payload["format"]
        payload["streams"][0]["duration"] = "9.25"
        self.capture.return_value = json.dumps(payload)
        self.assertEqual((await probe_dvd_title(self.path, 1)).duration, 9.25)

    async def test_fallback_uses_longest_known_stream_duration(self) -> None:
        for format_data in ({}, {"duration": None}, {"duration": "N/A"}):
            with self.subTest(format_data=format_data):
                payload = video_payload()
                payload["format"] = format_data
                payload["streams"][0]["duration"] = "11.5"
                payload["streams"].extend(
                    [
                        {"index": 1, "codec_type": "audio", "duration": "12.75"},
                        {"index": 2, "codec_type": "subtitle", "duration": "N/A"},
                        {"index": 3, "codec_type": "data", "duration": None},
                    ]
                )
                self.capture.return_value = json.dumps(payload)
                self.assertEqual((await probe_dvd_title(self.path, 1)).duration, 12.75)

    async def test_valid_format_duration_takes_precedence_including_zero(self) -> None:
        for duration in (0, "0.000", 1.25, "2.5"):
            with self.subTest(duration=duration):
                payload = video_payload()
                payload["format"]["duration"] = duration
                payload["streams"][0]["duration"] = "999"
                self.capture.return_value = json.dumps(payload)
                self.assertEqual((await probe_dvd_title(self.path, 1)).duration, float(duration))

    async def test_zero_stream_duration_is_valid_fallback(self) -> None:
        payload = video_payload()
        payload["format"] = {}
        payload["streams"][0]["duration"] = "0"
        self.capture.return_value = json.dumps(payload)
        self.assertEqual((await probe_dvd_title(self.path, 1)).duration, 0.0)

    async def test_malformed_durations_raise_with_title_context(self) -> None:
        invalid = (
            "",
            "unknown",
            "NaN",
            "Infinity",
            "-inf",
            "1e999",
            -1,
            "-0.1",
            True,
            [],
            {},
            float("nan"),
            float("inf"),
            10**400,
        )
        for location in ("format", "stream"):
            for value in invalid:
                with self.subTest(location=location, value=value):
                    payload = video_payload()
                    target = payload["format"] if location == "format" else payload["streams"][0]
                    target["duration"] = value
                    self.capture.return_value = json.dumps(payload)
                    with self.assertRaisesRegex(FFmpegError, r"title 6.*duration"):
                        await probe_dvd_title(self.path, 6)

    async def test_malformed_json_and_shapes_raise_with_title_context(self) -> None:
        malformed = [
            "",
            "not json",
            "{",
            "[]",
            "null",
            "42",
            "{}",
            '{"streams": null}',
            '{"streams": {}}',
            '{"streams": [null]}',
            '{"streams": [], "format": null}',
            '{"streams": [], "error": {"string": "Read error"}}',
        ]
        for output in malformed:
            with self.subTest(output=output):
                self.capture.return_value = output
                with self.assertRaisesRegex(FFmpegError, "malformed ffprobe data for title 8"):
                    await probe_dvd_title(self.path, 8)

    async def test_malformed_stream_metadata_is_rejected(self) -> None:
        invalid_fields = (
            ("index", None),
            ("index", "0"),
            ("index", True),
            ("index", -1),
            ("codec_type", None),
            ("codec_type", ""),
            ("codec_type", []),
            ("codec_name", 123),
            ("channels", "6"),
            ("channels", False),
            ("width", -720),
            ("height", 480.5),
            ("field_order", []),
            ("channel_layout", {}),
            ("tags", None),
            ("tags", []),
            ("tags", {"language": 123}),
            ("tags", {"title": []}),
        )
        for field, value in invalid_fields:
            with self.subTest(field=field, value=value):
                payload = video_payload()
                payload["streams"][0][field] = value
                self.capture.return_value = json.dumps(payload)
                with self.assertRaisesRegex(FFmpegError, "title 5"):
                    await probe_dvd_title(self.path, 5)

    async def test_duplicate_stream_indices_are_rejected(self) -> None:
        payload = video_payload()
        payload["streams"].append({"index": 0, "codec_type": "audio"})
        self.capture.return_value = json.dumps(payload)
        with self.assertRaisesRegex(FFmpegError, "duplicate stream index 0"):
            await probe_dvd_title(self.path, 1)

    async def test_valid_json_without_streams_is_a_title_not_eof(self) -> None:
        self.capture.return_value = '{"streams": [], "format": {}}'
        self.assertEqual(await probe_dvd_title(self.path, 2), Title(2, None, ()))

    async def test_invalid_title_ids_are_rejected_before_running_ffprobe(self) -> None:
        for number in (0, -1, 100, True, 1.5, "1"):
            with self.subTest(number=number), self.assertRaises(ValueError):
                await probe_dvd_title(self.path, number)  # type: ignore[arg-type]
        self.capture.assert_not_awaited()

    async def test_probe_failure_preserves_stderr_and_context(self) -> None:
        failure = FFmpegError("Unable to open the VMG (VIDEO_TS.IFO)")
        self.capture.side_effect = failure
        with self.assertRaises(FFmpegError) as caught:
            await probe_dvd_title(self.path, 4)
        self.assertIn("title 4", str(caught.exception))
        self.assertIn(str(self.path.resolve()), str(caught.exception))
        self.assertIn(str(failure), str(caught.exception))
        self.assertIs(caught.exception.__cause__, failure)


class ScanDvdTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.path = Path("disc.iso")
        patcher = patch("dvd_ripper.scanner.run_capture", new_callable=AsyncMock)
        self.capture = patcher.start()
        self.addCleanup(patcher.stop)
        self.payload = json.dumps(video_payload())

    async def test_scan_stops_only_at_confirmed_boundary(self) -> None:
        self.capture.side_effect = [self.payload, self.payload, end_of_disc(3)]
        progress: list[str] = []
        dvd = await scan_dvd(self.path, ffprobe="custom-ffprobe", on_progress=progress.append)
        self.assertIsInstance(dvd, DVD)
        self.assertEqual(dvd.path, self.path.resolve())
        self.assertEqual([title.number for title in dvd.titles], [1, 2])
        self.assertIsInstance(dvd.titles, tuple)
        self.assertEqual(dvd.warnings, ())
        self.assertEqual(self.capture.await_count, 3)
        for number, call in enumerate(self.capture.await_args_list, 1):
            args = call.args[0]
            self.assertEqual(args[0], "custom-ffprobe")
            self.assertEqual(args[args.index("-title") + 1], str(number))
            self.assertEqual(call.kwargs, {"timeout": 180.0})
        self.assertTrue(any("title 1" in message for message in progress))
        self.assertTrue(any("title 3" in message for message in progress))

    async def test_boundary_allows_dvdvideo_prefix_or_bare_diagnostic(self) -> None:
        for diagnostic in (
            "Title 2 not found",
            "[dvdvideo @ 0xabc] Title 2 not found\n",
            "[dvdvideo @ 0xabc] [error] Title 2 not found\r\n",
        ):
            with self.subTest(diagnostic=diagnostic):
                self.capture.side_effect = [self.payload, FFmpegError(diagnostic)]
                self.assertEqual(len((await scan_dvd(self.path)).titles), 1)

    async def test_all_other_errors_abort_including_after_successful_titles(self) -> None:
        diagnostics = (
            "Unknown input format: dvdvideo",
            "Could not find executable: ffprobe",
            "ffprobe timed out after 180 seconds",
            "Permission denied",
            "Unable to open the DVD-Video structure",
            "Unable to open the VMG (VIDEO_TS.IFO)",
            "Unable to read next block of PGC",
            "Title 2 has invalid headers (no PTTs found)",
            "Title 2 has invalid headers in VTS",
            "Title 2, PGC 1 looks empty (may consist of padding cells)",
            "Unable to add video stream",
            "Angle 1 not found",
            "Chapter (PTT) range [1, 2] is invalid",
            "Stream not found",
            "End of file",
            "Invalid argument",
            "Title number is out of range",
        )
        for successful_count in (0, 1):
            for diagnostic in diagnostics:
                with self.subTest(successful_count=successful_count, diagnostic=diagnostic):
                    self.capture.reset_mock()
                    self.capture.side_effect = [self.payload] * successful_count + [
                        FFmpegError(diagnostic)
                    ]
                    with self.assertRaises(FFmpegError) as caught:
                        await scan_dvd(self.path)
                    self.assertIn(diagnostic, str(caught.exception))
                    self.assertIn(f"title {successful_count + 1}", str(caught.exception))
                    self.assertIn("does not confirm end-of-disc", str(caught.exception))
                    self.assertEqual(self.capture.await_count, successful_count + 1)

    async def test_similar_diagnostics_wrong_title_and_filenames_are_not_boundaries(self) -> None:
        diagnostics = (
            "[dvdvideo @ 0xabc] Title 20 not found",
            "[dvdvideo @ 0xabc] Title 1 not found",
            "[dvdvideo @ 0xabc] Title 2 not found in damaged VTS",
            "[other @ 0xabc] Title 2 not found",
            "/images/Title 2 not found.iso: Permission denied",
            "Command failed: ffprobe -i 'Title 2 not found'",
        )
        for diagnostic in diagnostics:
            with self.subTest(diagnostic=diagnostic):
                self.capture.side_effect = [self.payload, FFmpegError(diagnostic)]
                with self.assertRaisesRegex(FFmpegError, "does not confirm end-of-disc"):
                    await scan_dvd(self.path)

    async def test_boundary_at_first_title_is_not_an_empty_success(self) -> None:
        self.capture.side_effect = end_of_disc(1)
        with self.assertRaisesRegex(FFmpegError, "no dvd titles found"):
            await scan_dvd(self.path)
        self.capture.assert_awaited_once()

    async def test_no_video_titles_are_skipped_with_visible_warnings(self) -> None:
        audio_only = {"streams": [{"index": 0, "codec_type": "audio"}]}
        unknown_duration = video_payload()
        unknown_duration["format"] = {"duration": "N/A"}
        self.capture.side_effect = [
            json.dumps(audio_only),
            '{"streams": []}',
            json.dumps(unknown_duration),
            end_of_disc(4),
        ]
        progress: list[str] = []
        dvd = await scan_dvd(self.path, on_progress=progress.append)
        self.assertEqual([title.number for title in dvd.titles], [3])
        self.assertIsNone(dvd.titles[0].duration)
        self.assertEqual(len(dvd.warnings), 2)
        self.assertIsInstance(dvd.warnings, tuple)
        for number, warning in enumerate(dvd.warnings, 1):
            self.assertIn(f"title {number}", warning)
            self.assertIn("no video", warning)
            self.assertIn(warning, progress)
        self.assertEqual(self.capture.await_count, 4)

    async def test_zero_duration_video_title_is_retained(self) -> None:
        payload = video_payload()
        payload["format"]["duration"] = "0"
        self.capture.side_effect = [json.dumps(payload), end_of_disc(2)]
        dvd = await scan_dvd(self.path)
        self.assertEqual(len(dvd.titles), 1)
        self.assertEqual(dvd.titles[0].duration, 0.0)

    async def test_disc_with_no_video_reports_failure_and_skip_details(self) -> None:
        self.capture.side_effect = ['{"streams": []}', end_of_disc(2)]
        with self.assertRaisesRegex(FFmpegError, "no video titles found") as caught:
            await scan_dvd(self.path)
        self.assertIn("skipped title 1", str(caught.exception))

    async def test_malformed_success_output_does_not_end_scan(self) -> None:
        for output in ("{}", "not json", '{"error": "Title 2 not found"}'):
            with self.subTest(output=output):
                self.capture.side_effect = [self.payload, output]
                with self.assertRaisesRegex(FFmpegError, "dvd scan failed at title 2"):
                    await scan_dvd(self.path)

    async def test_scan_is_bounded_at_99_and_never_probes_100(self) -> None:
        self.capture.return_value = self.payload
        dvd = await scan_dvd(self.path)
        self.assertEqual([title.number for title in dvd.titles], list(range(1, 100)))
        self.assertEqual(self.capture.await_count, 99)
        args = self.capture.await_args_list[-1].args[0]
        self.assertEqual(args[args.index("-title") + 1], "99")

    async def test_cancellation_propagates_without_probing_further(self) -> None:
        self.capture.side_effect = [self.payload, asyncio.CancelledError()]
        with self.assertRaises(asyncio.CancelledError):
            await scan_dvd(self.path)
        self.assertEqual(self.capture.await_count, 2)

    async def test_cancelling_scan_cancels_the_inflight_capture(self) -> None:
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def pending_capture(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        self.capture.side_effect = pending_capture
        task = asyncio.create_task(scan_dvd(self.path))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(stopped.is_set())
            self.capture.assert_awaited_once()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
