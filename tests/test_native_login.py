"""Native login must preserve existing loaders and clean its own partial setup."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('native_login', ROOT / 'scripts/uu-native-login.py')
login = importlib.util.module_from_spec(spec)
spec.loader.exec_module(login)


class NativeLoginTests(unittest.TestCase):
    def test_guest_configuration_does_not_count_as_logged_in(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory)
            settings = prefix / 'drive_c/users/zyk/AppData/Local/GameViewer'
            settings.mkdir(parents=True)
            (settings / 'setting_guest_1.ini').touch()
            self.assertFalse(login.logged_in(prefix))
            (settings / 'setting_account.ini').touch()
            self.assertTrue(login.logged_in(prefix))

    def test_tray_quit_is_a_server_missing_past_the_restart_grace(self):
        watcher = login.QuitWatcher(0)
        grace = login.SERVER_GONE_SECONDS
        self.assertFalse(watcher.quit(False, 0), 'a server that has not started yet is not a quit')
        self.assertFalse(watcher.quit(False, grace + 1))
        self.assertFalse(watcher.quit(True, 100))
        self.assertFalse(watcher.quit(False, 101))
        self.assertFalse(watcher.quit(True, 101 + grace - 1), 'Healthd restarted it within the grace')
        self.assertFalse(watcher.quit(False, 200))
        self.assertFalse(watcher.quit(False, 200 + grace - 1))
        self.assertTrue(watcher.quit(False, 200 + grace))

    def test_a_server_that_never_starts_ends_the_wait(self):
        watcher = login.QuitWatcher(1000)
        self.assertFalse(watcher.quit(False, 1000 + login.SERVER_START_SECONDS - 1))
        self.assertTrue(watcher.quit(False, 1000 + login.SERVER_START_SECONDS))
        self.assertFalse(watcher.seen)

    def until_quit_session(self, root, server_alive):
        """Run --until-quit against a bootstrap that never exits, on a simulated clock."""
        prefix = root / 'prefix'
        settings = prefix / 'drive_c/users/u/AppData/Local/GameViewer'
        settings.mkdir(parents=True)
        (settings / 'setting_account.ini').touch()
        prefix.chmod(0o700)
        bundle = root / 'bundle'
        (bundle / 'runtime').mkdir(parents=True)
        for name in ('bootstrap.exe', 'winlogon.exe'):
            (bundle / 'runtime' / name).write_bytes(b'MZ')
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds
            if clock[0] > 7 * 86400:
                raise AssertionError('the wait never ended')
        bootstrap = Mock()
        bootstrap.poll.return_value = None
        with patch.object(login.trial.bundle_tools, 'verify'), \
                patch.object(login.trial.bundle_tools, 'INPUTS', {}), \
                patch.object(login.trial, 'prefix_processes', return_value=[]), \
                patch.object(login.trial, 'uu_server_alive', side_effect=lambda _: server_alive(clock[0])), \
                patch.object(login.trial, 'cleanup_trial', return_value=[]) as cleanup, \
                patch.object(login.subprocess, 'Popen', return_value=bootstrap), \
                patch.object(login.subprocess, 'run'), \
                patch.object(login.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(login.time, 'sleep', side_effect=sleep):
            code = login.run(prefix, bundle, root / 'state', until_quit=True)
        cleanup.assert_called_once()
        return code, clock[0]

    def test_until_quit_gives_up_when_the_server_never_starts_but_the_bootstrap_lives(self):
        with tempfile.TemporaryDirectory() as directory:
            code, elapsed = self.until_quit_session(Path(directory), lambda now: False)
        self.assertEqual(code, 1)
        self.assertLess(elapsed, login.SERVER_START_SECONDS + 5)

    def test_until_quit_runs_past_the_start_limit_and_ends_on_tray_quit(self):
        session = 6 * 3600
        with tempfile.TemporaryDirectory() as directory:
            code, elapsed = self.until_quit_session(Path(directory), lambda now: 5 <= now < session)
        self.assertEqual(code, 0)
        self.assertGreaterEqual(elapsed, session + login.SERVER_GONE_SECONDS)

    def test_busy_prefix_is_rejected_before_creating_login_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = root / 'prefix'
            prefix.mkdir(mode=0o700)
            with patch.object(login.trial.bundle_tools, 'verify'), \
                    patch.object(login.trial, 'prefix_processes', return_value=[{'pid': 1}]), \
                    patch.object(login.subprocess, 'Popen') as launcher:
                with self.assertRaisesRegex(RuntimeError, 'busy'):
                    login.run(prefix, root / 'bundle', root / 'state')
            launcher.assert_not_called()
            self.assertEqual(list((root / 'state').iterdir()), [])

    def test_existing_loader_is_preserved_and_partial_links_are_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = root / 'prefix'
            prefix.mkdir(mode=0o700)
            app = prefix / 'drive_c/Program Files/Netease/GameViewer/bin'
            app.mkdir(parents=True)
            existing = app / 'existing.dll'
            existing.write_bytes(b'original')
            bundle = root / 'bundle'
            (bundle / 'runtime').mkdir(parents=True)
            for name in ('bootstrap.exe', 'winlogon.exe'):
                (bundle / 'runtime' / name).write_bytes(b'MZ')
            with patch.object(login.trial.bundle_tools, 'verify'), \
                    patch.object(login.trial, 'prefix_processes', return_value=[]), \
                    patch.object(login.trial.bundle_tools, 'INPUTS',
                                 {'app/bin/new.dll': '', 'app/bin/existing.dll': ''}), \
                    patch.object(login.subprocess, 'Popen') as launcher:
                with self.assertRaisesRegex(RuntimeError, 'existing loader'):
                    login.run(prefix, bundle, root / 'state')
            launcher.assert_not_called()
            self.assertEqual(existing.read_bytes(), b'original')
            self.assertFalse((app / 'new.dll').is_symlink())
            self.assertEqual(len(list((root / 'state').glob('login-*/cleanup.json'))), 1)


if __name__ == '__main__':
    unittest.main()
