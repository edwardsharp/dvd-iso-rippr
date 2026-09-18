"""bootstrap tests: no subprocess, package download, or media tool is actually run."""

import ast
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup.py"
SPEC = importlib.util.spec_from_file_location("dvd_ripper_bootstrap", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


class SetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dvd setup ; ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "pyproject.toml").write_text(
            "[project]\nname = 'dvd-ripper'\n", encoding="utf-8"
        )
        self.venv = self.root / ".venv"
        self.responses = {}
        self.stages = []
        self.validation_stdout = None
        self.venv_info = {
            "version": [3, 14, 7],
            "prefix": str(self.venv),
            "base_prefix": str(self.root / "system-python"),
        }
        self.patch(setup, "PROJECT_ROOT", self.root)
        self.patch(setup.sys, "version_info", (3, 14, 7))
        self.runner = self.patch(setup.subprocess, "run", side_effect=self.fake_run)

    def patch(self, target, name, *args, **kwargs):
        patcher = mock.patch.object(target, name, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def make_venv(self):
        python = setup.venv_python(self.venv)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("mock interpreter; never executed", encoding="utf-8")
        (self.venv / "pyvenv.cfg").write_text("home = system-python\n", encoding="utf-8")
        return python

    def fake_run(self, command, **kwargs):
        if command[1:3] == ["-m", "venv"]:
            stage = "create"
        elif command[1:3] == ["-m", "pip"]:
            stage = "install"
        elif command[1:3] == ["-m", "dvd_ripper"]:
            stage = "check"
        elif command[1] == "-c" and "json.dumps" in command[2]:
            stage = "validate"
        elif command[1] == "-c" and "import dvd_ripper.cli" in command[2]:
            stage = "imports"
        else:
            self.fail(f"unexpected subprocess: {command!r}")
        self.stages.append(stage)
        if stage in self.responses:
            response = self.responses[stage]
            if isinstance(response, BaseException):
                raise response
            return subprocess.CompletedProcess(command, response, "simulated diagnostic", "")
        if stage == "create":
            self.make_venv()
        stdout = ""
        if stage == "validate":
            stdout = self.validation_stdout
            if stdout is None:
                stdout = json.dumps(self.venv_info)
        return subprocess.CompletedProcess(command, 0, stdout, "")

    def run_setup(self, *args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = setup.main(list(args))
        return status, stdout.getvalue(), stderr.getvalue()

    def test_script_parses_with_python_38_grammar(self):
        ast.parse(SCRIPT.read_text(encoding="utf-8"), feature_version=(3, 8))

    def test_old_python_reports_full_install_guidance_without_subprocesses(self):
        for version in [(3, 8, 20), (3, 10, 16)]:
            with (
                self.subTest(version=version),
                mock.patch.object(setup.sys, "version_info", version),
            ):
                status, _, error = self.run_setup()
                self.assertEqual(status, 1)
                self.assertIn("requires python >=3.11", error)
                self.assertIn("brew install python@3.13", error)
                self.assertIn("python3-venv", error)
                self.assertIn("python3-pip", error)
                self.assertIn("py -3.13", error)
        self.runner.assert_not_called()
        self.assertFalse(self.venv.exists())

    def test_help_is_available_on_old_python_without_touching_environment(self):
        with (
            mock.patch.object(setup.sys, "version_info", (3, 8, 20)),
            self.assertRaises(SystemExit) as raised,
        ):
            self.run_setup("--help")
        self.assertEqual(raised.exception.code, 0)
        self.runner.assert_not_called()
        self.assertFalse(self.venv.exists())

    def test_python_311_is_sufficient(self):
        self.venv_info["version"] = [3, 11, 0]
        with mock.patch.object(setup.sys, "version_info", (3, 11, 0)):
            status, _, error = self.run_setup()
        self.assertEqual(status, 0, error)

    def test_missing_project_metadata_is_actionable(self):
        (self.root / "pyproject.toml").unlink()
        status, _, error = self.run_setup()
        self.assertEqual(status, 1)
        self.assertIn("complete project checkout", error)
        self.runner.assert_not_called()

    def test_fresh_dev_install_uses_current_python_and_venv_commands(self):
        status, output, error = self.run_setup("--dev")
        self.assertEqual(status, 0, error)
        self.assertEqual(self.stages, ["create", "validate", "install", "imports", "check"])
        calls = self.runner.call_args_list
        self.assertEqual(calls[0].args[0], [sys.executable, "-m", "venv", str(self.venv)])
        python = str(setup.venv_python(self.venv))
        for call in calls[1:]:
            self.assertEqual(call.args[0][0], python)
        self.assertEqual(
            calls[2].args[0],
            [
                python,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "-e",
                str(self.root) + "[dev]",
            ],
        )
        self.assertEqual(calls[-1].args[0], [python, "-m", "dvd_ripper", "--check"])
        for call in calls:
            self.assertIsInstance(call.args[0], list)
            self.assertIs(call.kwargs["shell"], False)
            self.assertEqual(call.kwargs["cwd"], str(self.root))
            self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
            self.assertGreater(call.kwargs["timeout"], 0)
            self.assertLessEqual(call.kwargs["timeout"], 600)
            self.assertNotIn("--upgrade", call.args[0])
        self.assertIn("runtime capability check passed", output)
        self.assertIn("run without activation", output)
        self.assertEqual(error, "")

    def test_default_install_does_not_request_dev_dependencies(self):
        status, _, error = self.run_setup()
        self.assertEqual(status, 0, error)
        self.assertEqual(self.runner.call_args_list[2].args[0][-1], str(self.root))

    def test_rerun_preserves_existing_environment_and_user_files(self):
        self.assertEqual(self.run_setup()[0], 0)
        marker = self.venv / "keep-me.txt"
        marker.write_text("user data", encoding="utf-8")
        status, output, error = self.run_setup("--dev")
        self.assertEqual(status, 0, error)
        self.assertEqual(self.stages.count("create"), 1)
        self.assertEqual(self.stages.count("install"), 2)
        self.assertEqual(marker.read_text(encoding="utf-8"), "user data")
        self.assertIn("reusing", output)
        self.assertIn("no python upgrade", output)

    def test_existing_compatible_python_is_not_upgraded_to_invoking_version(self):
        self.make_venv()
        self.venv_info["version"] = [3, 11, 12]
        status, _, error = self.run_setup()
        self.assertEqual(status, 0, error)
        self.assertNotIn("create", self.stages)

    def test_refuses_non_venv_directory_without_modifying_contents(self):
        self.venv.mkdir()
        marker = self.venv / "important.txt"
        marker.write_text("keep this", encoding="utf-8")
        status, _, error = self.run_setup()
        self.assertEqual(status, 1)
        self.assertIn("refusing to modify", error)
        self.assertEqual(list(self.venv.iterdir()), [marker])
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep this")
        self.runner.assert_not_called()

    def test_refuses_even_an_empty_existing_directory(self):
        self.venv.mkdir()
        status, _, error = self.run_setup()
        self.assertEqual(status, 1)
        self.assertIn("move it aside", error)
        self.assertEqual(list(self.venv.iterdir()), [])
        self.runner.assert_not_called()

    def test_refuses_existing_file(self):
        self.venv.write_text("not a venv", encoding="utf-8")
        status, _, error = self.run_setup()
        self.assertEqual(status, 1)
        self.assertIn("refusing to modify", error)
        self.assertEqual(self.venv.read_text(encoding="utf-8"), "not a venv")
        self.runner.assert_not_called()

    def test_refuses_incomplete_environment(self):
        self.venv.mkdir()
        config = self.venv / "pyvenv.cfg"
        config.write_text("home = some-python\n", encoding="utf-8")
        status, _, error = self.run_setup()
        self.assertEqual(status, 1)
        self.assertIn("not a usable local virtual environment", error)
        self.assertTrue(config.is_file())
        self.runner.assert_not_called()

    def make_symlink(self, link, target, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

    def test_refuses_symlinked_venv(self):
        target = self.root / "other-environment"
        target.mkdir()
        marker = target / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        self.make_symlink(self.venv, target, directory=True)
        status, _, error = self.run_setup()
        self.assertEqual(status, 1)
        self.assertIn("symlink", error)
        self.assertTrue(self.venv.is_symlink())
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.runner.assert_not_called()

    def test_refuses_dangling_symlink(self):
        target = self.root / "missing-target"
        self.make_symlink(self.venv, target, directory=True)
        status, _, error = self.run_setup()
        self.assertEqual(status, 1)
        self.assertIn("symlink", error)
        self.assertTrue(self.venv.is_symlink())
        self.assertFalse(target.exists())
        self.runner.assert_not_called()

    def test_refuses_symlinked_venv_config(self):
        self.make_venv()
        config = self.venv / "pyvenv.cfg"
        config.unlink()
        target = self.root / "other.cfg"
        target.write_text("keep", encoding="utf-8")
        self.make_symlink(config, target)
        status, _, error = self.run_setup()
        self.assertEqual(status, 1)
        self.assertIn("refusing to modify", error)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep")
        self.runner.assert_not_called()

    def test_old_existing_venv_is_not_replaced(self):
        self.make_venv()
        self.venv_info["version"] = [3, 10, 16]
        status, _, error = self.run_setup()
        self.assertEqual(status, 1)
        self.assertIn("existing .venv uses python 3.10.16", error)
        self.assertIn("no automatic replacement", error)
        self.assertEqual(self.stages, ["validate"])
        self.assertTrue(setup.venv_python(self.venv).exists())

    def test_rejects_interpreter_outside_project_environment(self):
        self.make_venv()
        self.venv_info["prefix"] = str(self.root / "another-venv")
        status, _, error = self.run_setup()
        self.assertEqual(status, 1)
        self.assertIn("not running inside", error)
        self.assertEqual(self.stages, ["validate"])

    def test_rejects_interpreter_without_venv_isolation(self):
        self.make_venv()
        self.venv_info["base_prefix"] = str(self.venv)
        status, _, error = self.run_setup()
        self.assertEqual(status, 1)
        self.assertIn("not running inside", error)
        self.assertEqual(self.stages, ["validate"])

    def test_malformed_interpreter_response_is_actionable(self):
        self.make_venv()
        self.validation_stdout = "not JSON"
        status, _, error = self.run_setup()
        self.assertEqual(status, 1)
        self.assertIn("cannot use", error)
        self.assertIn("move the existing .venv aside", error)
        self.assertEqual(self.stages, ["validate"])

    def test_creation_failure_explains_venv_and_ensurepip(self):
        self.responses["create"] = 1
        status, _, error = self.run_setup()
        self.assertEqual(status, 1)
        self.assertIn("virtual environment creation failed", error)
        self.assertIn("venv and ensurepip support", error)
        self.assertIn("python3-venv", error)
        self.assertEqual(self.stages, ["create"])

    def test_failed_creation_does_not_delete_partial_environment(self):
        def fail_after_creation(command, **kwargs):
            self.make_venv()
            return subprocess.CompletedProcess(command, 1)

        self.runner.side_effect = fail_after_creation
        status, _, error = self.run_setup()
        self.assertEqual(status, 1)
        self.assertIn("partial .venv", error)
        self.assertTrue((self.venv / "pyvenv.cfg").is_file())
        self.runner.assert_called_once()

    def test_creation_timeout_has_bounded_actionable_failure(self):
        self.responses["create"] = subprocess.TimeoutExpired("mock venv", setup.CREATE_TIMEOUT)
        status, _, error = self.run_setup()
        self.assertEqual(status, 1)
        self.assertIn("timed out after 120 seconds", error)
        self.assertIn("partial .venv", error)
        self.assertEqual(self.stages, ["create"])

    def test_broken_venv_executable_is_actionable(self):
        self.make_venv()
        self.responses["validate"] = FileNotFoundError("interpreter no longer exists")
        status, _, error = self.run_setup()
        self.assertEqual(status, 1)
        self.assertIn("could not run virtual environment validation", error)
        self.assertIn("broken or moved", error)
        self.assertEqual(self.stages, ["validate"])

    def test_pip_failure_has_network_and_missing_pip_guidance(self):
        self.responses["install"] = 1
        status, output, error = self.run_setup()
        self.assertEqual(status, 1)
        self.assertIn("package installation failed", error)
        self.assertIn("network/proxy/index", error)
        self.assertIn("ensurepip --upgrade", error)
        self.assertIn("do not use sudo pip", error)
        self.assertNotIn("installation succeeded", output)
        self.assertEqual(self.stages, ["create", "validate", "install"])
        self.assertTrue(self.venv.is_dir())

    def test_install_timeout_does_not_run_runtime_check(self):
        self.responses["install"] = subprocess.TimeoutExpired("mock pip", setup.INSTALL_TIMEOUT)
        status, _, error = self.run_setup()
        self.assertEqual(status, 1)
        self.assertIn("timed out after 600 seconds", error)
        self.assertIn("command:", error)
        self.assertIn("network/proxy/index", error)
        self.assertNotIn("check", self.stages)

    def test_permission_error_is_actionable(self):
        self.responses["create"] = PermissionError("permission denied")
        status, _, error = self.run_setup()
        self.assertEqual(status, 1)
        self.assertIn("could not run virtual environment creation", error)
        self.assertIn("project is writable", error)

    def test_import_failure_is_not_misreported_as_missing_ffmpeg(self):
        self.responses["imports"] = 1
        status, output, error = self.run_setup()
        self.assertEqual(status, 1)
        self.assertIn("python package import check failed", error)
        self.assertIn("traceback", error)
        self.assertNotIn("next steps for ffmpeg", output + error)
        self.assertNotIn("check", self.stages)

    def test_missing_runtime_capabilities_warn_but_do_not_fail_python_setup(self):
        self.responses["check"] = 1
        status, output, error = self.run_setup()
        self.assertEqual(status, 0, error)
        self.assertIn("python environment and package installation succeeded", output)
        self.assertIn("runtime check did not pass (exit 1)", output)
        self.assertIn("conversion is not ready", output)
        self.assertIn("--ffmpeg PATH --ffprobe PATH", output)
        self.assertIn("Unknown format dvdvideo", output)
        self.assertIn("even when the command exits 0", output)
        self.assertIn("reinstalling python will not add dvd support", output)
        self.assertIn("build help: python -m dvd_ripper --setup-ffmpeg", output)
        self.assertIn("local build: python scripts/build_ffmpeg.py", output)
        self.assertNotIn("runtime capability check passed", output)
        self.assertEqual(error, "")

    def test_other_nonzero_runtime_status_is_visible_not_claimed_ready(self):
        self.responses["check"] = 2
        status, output, error = self.run_setup()
        self.assertEqual(status, 0, error)
        self.assertIn("runtime check did not pass (exit 2)", output)
        self.assertIn("review its output", output)
        self.assertNotIn("runtime capability check passed", output)

    def test_runtime_timeout_distinguishes_completed_python_install(self):
        self.responses["check"] = subprocess.TimeoutExpired("mock check", setup.CHECK_TIMEOUT)
        status, output, error = self.run_setup()
        self.assertEqual(status, 1)
        self.assertIn("installation succeeded", output)
        self.assertIn("runtime capability check timed out", error)
        self.assertIn("python installation completed", error)
        self.assertIn("-m dvd_ripper --check", error)

    def test_interrupt_does_not_delete_environment(self):
        self.responses["install"] = KeyboardInterrupt()
        status, _, error = self.run_setup()
        self.assertEqual(status, 130)
        self.assertIn("setup interrupted", error)
        self.assertTrue((self.venv / "pyvenv.cfg").exists())

    def test_windows_uses_scripts_python_exe(self):
        with mock.patch.object(setup, "IS_WINDOWS", True):
            status, _, error = self.run_setup()
        self.assertEqual(status, 0, error)
        python = str(self.venv / "Scripts" / "python.exe")
        for call in self.runner.call_args_list[1:]:
            self.assertEqual(call.args[0][0], python)
        self.assertFalse((self.venv / "bin").exists())


if __name__ == "__main__":
    unittest.main()
