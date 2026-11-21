"""All tmux integration tests use private sockets, homes and state directories."""

from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tmux_clean_reload import cli


ROOT = Path(__file__).resolve().parents[1]
TMUX = shutil.which("tmux")


@unittest.skipUnless(TMUX, "tmux is required for integration tests")
class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tcr-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.socket = self.directory / "server"
        self.home = self.directory / "home"
        self.home.mkdir()
        self.config = self.home / ".tmux.conf"
        self.config.write_text("bind-key C-a last-window\n", encoding="utf-8")
        self.environment = dict(os.environ)
        self.environment.pop("TMUX", None)
        self.environment.pop("TMUX_PANE", None)
        self.environment.update({
            "HOME": str(self.home),
            "XDG_STATE_HOME": str(self.directory / "state"),
            "PYTHONPATH": str(ROOT / "src"),
        })
        self.addCleanup(self.stop_server)
        self.tmux(
            "new-session", "-d", "-s", "first", sys.executable,
            "-c", "import time; time.sleep(600)", start=True,
        )
        pid = self.tmux("display-message", "-p", "#{pid}").strip()
        pane = self.tmux("display-message", "-p", "-t", "first:", "#{pane_id}").strip()
        self.environment.update({"TMUX": f"{self.socket},{pid},0", "TMUX_PANE": pane})
        self.target = cli.Tmux(TMUX, str(self.socket))

    def stop_server(self):
        subprocess.run(
            [TMUX, "-N", "-S", str(self.socket), "kill-server"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )

    def tmux(self, *arguments, start=False):
        options = ["-f", "/dev/null"] if start else ["-N"]
        result = subprocess.run(
            [TMUX, "-S", str(self.socket), *options, *arguments],
            capture_output=True, env=self.environment, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result.stdout.decode("utf-8", errors="surrogateescape")

    def invoke(self, *arguments, success=True, environment=None):
        installed = os.environ.get("TCR_TEST_EXECUTABLE")
        command = [installed] if installed else [sys.executable, "-m", "tmux_clean_reload"]
        child_environment = dict(environment or self.environment)
        if installed:
            child_environment.pop("PYTHONPATH", None)
        result = subprocess.run(
            [*command, "--tmux", TMUX, *map(str, arguments)],
            capture_output=True, text=True, env=child_environment, check=False,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertNotIn("Traceback", result.stderr)
        return result

    def bindings(self):
        return cli.capture_bindings(self.target)

    def snapshots(self):
        return sorted((self.directory / "state").glob("tmux-clean-reload/*/snapshot-*.json"))

    def test_reset_restores_defaults_removes_stale_keys_and_reloads_for_all_sessions(self):
        baseline = {binding["key"]: binding for binding in self.bindings()}
        self.tmux("new-session", "-d", "-s", "second", sys.executable, "-c", "import time; time.sleep(600)")
        self.tmux("unbind-key", "\\;")
        self.tmux("bind-key", "-N", "old customization", "C-s", "display-message", "stale")
        self.tmux("bind-key", "C-o", "display-message", "overridden default")
        self.tmux("bind-key", "-n", "C-z", "display-message", "root binding survives")
        root_before = self.tmux("list-keys", "-T", "root")
        copy_before = self.tmux("list-keys", "-T", "copy-mode-vi")
        before = self.bindings()
        self.config.write_text("unbind-key ';'\nbind-key C-a last-window\nset -g status off\n", encoding="utf-8")

        result = self.invoke()

        after = {binding["key"]: binding for binding in self.bindings()}
        self.assertNotIn("C-s", after)
        self.assertNotIn(";", after)
        self.assertEqual(after["C-o"], baseline["C-o"])
        self.assertEqual(after["C-a"]["command"], "last-window")
        self.assertEqual(self.tmux("list-keys", "-T", "root"), root_before)
        self.assertEqual(self.tmux("list-keys", "-T", "copy-mode-vi"), copy_before)
        for session in ("first", "second"):
            self.assertEqual(self.tmux("show-options", "-Av", "-t", session, "status").strip(), "off")
        snapshot = self.snapshots()[0]
        self.assertIn(str(snapshot), result.stdout)
        self.assertEqual(json.loads(snapshot.read_text())["bindings"], before)
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
        self.assertEqual(snapshot.parent.stat().st_mode & 0o777, 0o700)

    def test_restores_missing_default_and_preserves_default_notes_and_repeat_flags(self):
        baseline = self.bindings()
        self.config.write_text("", encoding="utf-8")
        self.tmux("unbind-key", "\\;")
        self.invoke()
        self.assertEqual(self.bindings(), baseline)

    def test_restore_round_trips_awkward_notes_keys_and_commands(self):
        note = 'Quoted "text", $HOME, #format, \\ slash\nnext\rreturn\ttab café'
        for key in ("C-s", ";", '"', "'", "$"):
            self.tmux("bind-key", "-r", "-N", note, "\\;" if key == ";" else key,
                      "display-message", 'literal " ; $HOME #{} \\')
        before = self.bindings()
        self.invoke()
        self.assertNotEqual(self.bindings(), before)
        self.invoke("--restore", self.snapshots()[0])
        self.assertEqual(self.bindings(), before)
        self.assertEqual(len(self.snapshots()), 2)

    def test_empty_prefix_table_can_be_snapshotted_and_restored(self):
        self.tmux("unbind-key", "-a", "-T", "prefix")
        self.assertEqual(self.bindings(), [])
        self.invoke()
        snapshot = self.snapshots()[0]
        self.assertEqual(json.loads(snapshot.read_text())["bindings"], [])
        self.invoke("--restore", snapshot)
        self.assertEqual(self.bindings(), [])

    def test_syntax_error_does_not_change_bindings_or_create_snapshot(self):
        before = self.bindings()
        self.config.write_text('bind-key {\n', encoding="utf-8")
        self.invoke(success=False)
        self.assertEqual(self.bindings(), before)
        self.assertEqual(self.snapshots(), [])

    def test_runtime_error_rolls_back_bindings_but_not_other_config_side_effects(self):
        self.tmux("bind-key", "C-s", "display-message", "keep me")
        before = self.bindings()
        self.config.write_text(
            "set -g status off\nbind-key C-s last-window\nset -g nonexistent-option on\n",
            encoding="utf-8",
        )
        result = self.invoke(success=False)
        self.assertIn("previous prefix bindings restored", result.stderr)
        self.assertEqual(self.bindings(), before)
        self.assertEqual(self.tmux("show-options", "-gv", "status").strip(), "off")
        self.assertEqual(len(self.snapshots()), 1)

    def test_missing_nested_source_rolls_back_bindings(self):
        before = self.bindings()
        self.config.write_text(f"bind-key C-s last-window\nsource-file {self.home}/missing.conf\n", encoding="utf-8")
        result = self.invoke(success=False)
        self.assertIn("previous prefix bindings restored", result.stderr)
        self.assertEqual(self.bindings(), before)

    def test_missing_config_does_not_change_bindings(self):
        before = self.bindings()
        self.config.unlink()
        self.invoke(success=False)
        self.assertEqual(self.bindings(), before)
        self.assertEqual(self.snapshots(), [])

    def test_explicit_config_path_with_spaces_and_glob_characters(self):
        config = self.home / "config [literal]*?.conf"
        config.write_text("bind-key C-d last-pane\n", encoding="utf-8")
        self.invoke("--config", config)
        self.assertEqual(next(binding for binding in self.bindings() if binding["key"] == "C-d")["command"], "last-pane")

    def test_repeated_reload_is_idempotent(self):
        self.invoke()
        after = self.bindings()
        self.invoke()
        self.assertEqual(self.bindings(), after)
        self.assertEqual(len(self.snapshots()), 2)

    def test_outside_tmux_fails_without_touching_server(self):
        environment = dict(self.environment)
        environment.pop("TMUX")
        before = self.bindings()
        result = self.invoke(success=False, environment=environment)
        self.assertIn("inside tmux", result.stderr)
        self.assertEqual(self.bindings(), before)

    def test_malformed_tmux_environment_is_rejected(self):
        environment = dict(self.environment, TMUX="not,a,valid,value")
        result = self.invoke(success=False, environment=environment)
        self.assertIn("valid $TMUX", result.stderr)

    def test_snapshot_for_another_server_is_rejected(self):
        self.invoke()
        snapshot = self.snapshots()[0]
        data = json.loads(snapshot.read_text())
        data["socket"] = "/different/server"
        snapshot.write_text(json.dumps(data))
        before = self.bindings()
        self.invoke("--restore", snapshot, success=False)
        self.assertEqual(self.bindings(), before)
        self.assertEqual(len(self.snapshots()), 1)

    def test_invalid_snapshot_is_rejected_before_mutation(self):
        snapshot = self.directory / "invalid.json"
        snapshot.write_text("not json")
        before = self.bindings()
        self.invoke("--restore", snapshot, success=False)
        self.assertEqual(self.bindings(), before)

    def test_missing_executable_is_actionable(self):
        result = self.invoke("--tmux", "/no/such/tmux", success=False)
        self.assertIn("executable not found", result.stderr)

    def test_version_mismatch_does_not_change_bindings(self):
        wrapper = self.directory / "fake-tmux"
        wrapper.write_text(
            f"#!{sys.executable}\nimport os, sys\n"
            f"if sys.argv[1:] == ['-V']: print('tmux incompatible')\n"
            f"else: os.execv({TMUX!r}, [{TMUX!r}, *sys.argv[1:]])\n"
        )
        wrapper.chmod(0o700)
        before = self.bindings()
        result = self.invoke("--tmux", wrapper, success=False)
        self.assertIn("differs from executable version", result.stderr)
        self.assertEqual(self.bindings(), before)

    def test_overlapping_operation_is_rejected(self):
        with mock.patch.dict(os.environ, self.environment, clear=True):
            with cli.server_lock(str(self.socket)):
                result = self.invoke(success=False)
        self.assertIn("already running", result.stderr)
        self.assertEqual(self.snapshots(), [])

    def test_relative_state_directory_is_rejected(self):
        environment = dict(self.environment, XDG_STATE_HOME="relative/state")
        result = self.invoke(success=False, environment=environment)
        self.assertIn("absolute path", result.stderr)
        self.assertEqual(self.snapshots(), [])

    def test_unwritable_state_fails_before_mutation(self):
        state = self.directory / "state"
        state.write_text("not a directory")
        before = self.bindings()
        self.invoke(success=False)
        self.assertEqual(self.bindings(), before)

    def test_insecure_state_directory_is_rejected(self):
        root = self.directory / "state" / "tmux-clean-reload"
        root.mkdir(parents=True, mode=0o755)
        root.chmod(0o755)
        result = self.invoke(success=False)
        self.assertIn("0700", result.stderr)
        self.assertEqual(self.snapshots(), [])

    def test_source_uses_calling_pane_context(self):
        self.tmux("new-session", "-d", "-s", "second", sys.executable, "-c", "import time; time.sleep(600)")
        self.config.write_text("set-option @clean-reload-context first\n", encoding="utf-8")
        self.invoke()
        self.assertEqual(self.tmux("show-options", "-v", "-t", "first", "@clean-reload-context").strip(), "first")
        self.assertNotIn("@clean-reload-context", self.tmux("show-options", "-t", "second"))


class UnitTests(unittest.TestCase):
    def test_quoting_does_not_use_shell_single_quote_rules(self):
        self.assertEqual(cli.quote_tmux('a"b\\c$d\ne\rf\tg'), '"a\\"b\\\\c\\$d\\ne\\rf\\tg"')

    def test_old_server_version_is_rejected_before_export(self):
        with (
            mock.patch.dict(os.environ, {"TMUX": "/socket,123,0"}),
            mock.patch.object(cli.shutil, "which", return_value="/bin/tmux"),
            mock.patch.object(cli.Tmux, "identity", return_value="123 456 3.6a"),
        ):
            with self.assertRaisesRegex(cli.ReloadError, r"tmux 3\.7\+ is required"):
                cli.current_tmux("tmux")

    def test_server_without_any_key_tables_has_empty_prefix(self):
        tmux = mock.Mock()
        tmux.run.return_value = ""
        self.assertEqual(cli.capture_bindings(tmux), [])

    def test_incomplete_export_is_rejected(self):
        tmux = mock.Mock()
        tmux.run.return_value = "truncated output"
        with self.assertRaisesRegex(cli.ReloadError, "incomplete"):
            cli.capture_bindings(tmux)

    def test_bad_export_fields_are_rejected(self):
        tmux = mock.Mock()
        nonce = mock.Mock(hex="nonce")
        tmux.run.return_value = "bad:nonce:record:\n"
        with mock.patch.object(cli.uuid, "uuid4", return_value=nonce):
            with self.assertRaisesRegex(cli.ReloadError, "decode"):
                cli.capture_bindings(tmux)

    def test_restore_validation_rejects_bad_bindings_and_server_lifetimes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "snapshot.json"
            tmux = mock.Mock(socket="/socket")
            good = {"key": "C-a", "repeat": False, "note": "", "command": "last-window"}
            malformed = [
                None, {}, [None], [{**good, "repeat": 1}], [{**good, "command": ""}],
                [{**good, "note": "\0"}], [good, good], [{**good, "extra": "value"}],
            ]
            for bindings in malformed:
                with self.subTest(bindings=bindings):
                    path.write_text(json.dumps({"schema": 1, "socket": "/socket", "server": "1 3.8", "bindings": bindings}))
                    with self.assertRaises(cli.ReloadError):
                        cli.load_snapshot(path, tmux, "1 3.8")
            path.write_text(json.dumps({"schema": 1, "socket": "/socket", "server": "old", "bindings": []}))
            with self.assertRaisesRegex(cli.ReloadError, "server lifetime"):
                cli.load_snapshot(path, tmux, "new")

    @contextmanager
    def prepared_operation(self):
        arguments = mock.Mock(tmux="tmux", config=Path("~/.tmux.conf"), restore=None)
        tmux = mock.Mock(socket="/socket")
        tmux.identity.return_value = "identity"

        @contextmanager
        def lock(_socket):
            yield Path("/state")

        with ExitStack() as stack:
            patches = [
                mock.patch.object(cli, "current_tmux", return_value=(tmux, "identity")),
                mock.patch.object(cli, "server_lock", lock),
                mock.patch.object(Path, "resolve", return_value=Path("/config")),
                mock.patch.object(Path, "is_file", return_value=True),
                mock.patch.object(cli, "defaults", return_value=["defaults"]),
                mock.patch.object(cli, "capture_bindings", return_value=["before"]),
                mock.patch.object(cli, "save_snapshot", return_value=Path("/private/snapshot.json")),
                mock.patch("builtins.print"),
            ]
            for patch in patches:
                stack.enter_context(patch)
            apply = stack.enter_context(mock.patch.object(cli, "apply"))
            yield arguments, tmux, apply

    def test_server_replacement_during_preparation_never_applies(self):
        with self.prepared_operation() as (arguments, tmux, apply):
            tmux.identity.return_value = "different server"
            with self.assertRaisesRegex(cli.ReloadError, "changed during preparation"):
                cli.reload_or_restore(arguments)
            apply.assert_not_called()

    def test_rollback_failure_reports_retained_snapshot(self):
        with self.prepared_operation() as (arguments, _tmux, apply):
            apply.side_effect = [cli.ReloadError("reset failed"), cli.ReloadError("rollback failed")]
            with self.assertRaisesRegex(cli.ReloadError, "Recovery snapshot: /private/snapshot.json"):
                cli.reload_or_restore(arguments)
            self.assertEqual(apply.call_count, 2)

    def test_server_replacement_after_failure_prevents_rollback_into_new_server(self):
        with self.prepared_operation() as (arguments, tmux, apply):
            tmux.identity.side_effect = ["identity", "different server"]
            apply.side_effect = cli.ReloadError("reset failed")
            with self.assertRaisesRegex(cli.ReloadError, "refusing to restore into a different server"):
                cli.reload_or_restore(arguments)
            self.assertEqual(apply.call_count, 1)

    def test_keyboard_interrupt_attempts_binding_rollback(self):
        with self.prepared_operation() as (arguments, _tmux, apply):
            apply.side_effect = [KeyboardInterrupt(), None]
            with self.assertRaisesRegex(cli.ReloadError, "previous prefix bindings restored"):
                cli.reload_or_restore(arguments)
            self.assertEqual(apply.call_args_list[-1].args[1], ["before"])

    def test_snapshot_write_failure_never_applies(self):
        with self.prepared_operation() as (arguments, _tmux, apply):
            with mock.patch.object(cli, "save_snapshot", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    cli.reload_or_restore(arguments)
            apply.assert_not_called()

    def test_failed_preparation_never_applies(self):
        arguments = mock.Mock(tmux="tmux", config=Path("~/.tmux.conf"), restore=None)
        tmux = mock.Mock(socket="/socket")

        @contextmanager
        def lock(_socket):
            yield Path("/state")

        with (
            mock.patch.object(cli, "current_tmux", return_value=(tmux, "identity")),
            mock.patch.object(cli, "server_lock", lock),
            mock.patch.object(Path, "resolve", return_value=Path("/config")),
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(cli, "defaults", side_effect=cli.ReloadError("baseline failed")),
            mock.patch.object(cli, "apply") as apply,
        ):
            with self.assertRaisesRegex(cli.ReloadError, "baseline failed"):
                cli.reload_or_restore(arguments)
        apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
