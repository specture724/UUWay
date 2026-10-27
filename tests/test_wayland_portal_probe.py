import subprocess
import importlib.util
import os
import tempfile
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/probe-wayland-portal.py'
BINARY = ROOT / 'build/native-presenter/uu-pipewire-dmabuf-probe'
spec = importlib.util.spec_from_file_location('wayland_portal_probe', SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class WaylandPortalProbeTests(unittest.TestCase):
    def test_a_grant_must_be_the_expected_kde_output(self):
        state = dict(outputs=[dict(name='DP-1', enabled=True, pos=dict(x=2743, y=0)),
                              dict(name='DP-2', enabled=True, pos=dict(x=0, y=0)),
                              dict(name='HDMI-A-1', enabled=False, pos=dict(x=0, y=0))])
        position = probe.expected_output_position('DP-1', state)
        self.assertEqual(position, (2743, 0))
        probe.check_stream_output({'position': (2743, 0), 'size': (2560, 1440)}, 'DP-1', position)
        with self.assertRaisesRegex(RuntimeError, 'DP-1'):
            probe.check_stream_output({'position': (0, 0)}, 'DP-1', position)
        with self.assertRaises(RuntimeError):
            probe.check_stream_output({'size': (2560, 1440)}, 'DP-1', position)
        with self.assertRaisesRegex(RuntimeError, 'not an enabled'):
            probe.expected_output_position('HDMI-A-1', state)
        state['outputs'][0]['pos'] = dict(x=0, y=0)
        with self.assertRaisesRegex(RuntimeError, 'mirrored'):
            probe.expected_output_position('DP-1', state)

    def test_a_restored_grant_names_the_kde_output_it_shares(self):
        state = dict(outputs=[dict(name='DP-1', enabled=True, pos=dict(x=2743, y=0)),
                              dict(name='DP-2', enabled=True, pos=dict(x=0, y=0)),
                              dict(name='HDMI-A-1', enabled=False, pos=dict(x=2743, y=0))])
        self.assertEqual(probe.stream_output({'position': (2743, 0)}, state), 'DP-1')
        self.assertEqual(probe.stream_output({'position': (0, 0)}, state), 'DP-2')
        for properties in ({'position': (5000, 0)}, {'size': (2560, 1440)}):
            with self.subTest(properties=properties), self.assertRaises(RuntimeError):
                probe.stream_output(properties, state)
        state['outputs'][0]['pos'] = dict(x=0, y=0)
        with self.assertRaisesRegex(RuntimeError, 'exactly one'):
            probe.stream_output({'position': (0, 0)}, state)

    def test_identify_output_needs_a_restore_state_and_excludes_expect_output(self):
        for args in [['--identify-output'],
                     ['--identify-output', '--restore-state', '/unused', '--expect-output', 'DP-1']]:
            result = subprocess.run(['/usr/bin/python3', str(SCRIPT), *args],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2, args)
            self.assertNotIn('Portal CreateSession:', result.stderr)

    def test_kde_registers_installed_identity_once_before_portal_requests(self):
        from gi.repository import Gio
        bus = Mock()
        bus.get_unique_name.return_value = ':test.registry'
        with patch.dict(os.environ, XDG_CURRENT_DESKTOP='KDE', XDG_SESSION_TYPE='wayland'), \
                patch.object(Gio.DesktopAppInfo, 'new', return_value=Mock()), \
                patch.object(probe, 'REGISTERED_CONNECTIONS', set()):
            self.assertTrue(probe.register_portal_app(bus))
            self.assertTrue(probe.register_portal_app(bus))
        bus.call_sync.assert_called_once()
        args = bus.call_sync.call_args.args
        self.assertEqual(args[2:4], ('org.freedesktop.host.portal.Registry', 'Register'))
        self.assertEqual(args[4].unpack(), ('uuway', {}))

    def test_other_desktops_and_uninstalled_probe_keep_existing_identity(self):
        from gi.repository import Gio
        bus = Mock()
        with patch.dict(os.environ, XDG_CURRENT_DESKTOP='GNOME', XDG_SESSION_TYPE='wayland'):
            self.assertFalse(probe.register_portal_app(bus))
        with patch.dict(os.environ, XDG_CURRENT_DESKTOP='KDE', XDG_SESSION_TYPE='wayland'), \
                patch.object(Gio.DesktopAppInfo, 'new', return_value=None):
            self.assertFalse(probe.register_portal_app(bus))
        bus.call_sync.assert_not_called()

    def test_cursor_policy_is_explicit_and_capability_checked(self):
        self.assertEqual(probe.cursor_mode_value('embedded', 7), 2)
        self.assertEqual(probe.cursor_mode_value('hidden', 7), 1)
        self.assertEqual(probe.cursor_mode_value('metadata', 7), 4)
        self.assertEqual(probe.cursor_mode_value('composited', 7), 4)
        with self.assertRaises(RuntimeError):
            probe.cursor_mode_value('composited', 3)
        for name, available in [('hidden', 2), ('embedded', 1), ('hidden', 0)]:
            with self.subTest(name=name, available=available), self.assertRaises(RuntimeError):
                probe.cursor_mode_value(name, available)
        with self.assertRaises(ValueError):
            probe.cursor_mode_value('unknown', 7)

    def test_new_diagnostic_arguments_fail_before_requesting_permission(self):
        for args in [['--source', 'rdp'], ['--authorization-timeout', '4'],
                     ['--authorization-timeout', '121'], ['--snapshot', '/unused'],
                     ['--cursor-mode', 'metadata'], ['--cursor-mode', 'composited'], ['--capture-status-fd', '3'],
                     ['--capture-status-fd', '3', '--gpu-relay-fd', '3']]:
            result = subprocess.run(['/usr/bin/python3', str(SCRIPT), *args],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn('Portal CreateSession:', result.stderr)

    def test_composition_report_requires_real_and_consistent_counts(self):
        good = 'UURB_CURSOR_COMPOSITE {"cursor_only_frames":3,"desktop_frames":7}'
        self.assertEqual(probe.composition_report(good, 10),
                         dict(cursor_composited=True, cursor_only_frames=3, desktop_frames=7))
        for value, frames in [('', 10), (good + '\n' + good, 10), (good, 11), (good, True),
                ('UURB_CURSOR_COMPOSITE {"cursor_only_frames":10,"desktop_frames":0}', 10),
                ('UURB_CURSOR_COMPOSITE {"cursor_only_frames":true,"desktop_frames":9}', 10),
                ('UURB_CURSOR_COMPOSITE {"cursor_only_frames":-1,"desktop_frames":11}', 10)]:
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                probe.composition_report(value, frames)

    def test_private_restore_state_roundtrip_and_rotation(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / 'state.json'
            self.assertIsNone(probe.load_restore_token(state))
            probe.save_restore_token(state, 'test-token')
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)
            self.assertEqual(probe.load_restore_token(state), 'test-token')
            probe.save_restore_token(state, 'rotated')
            self.assertEqual(probe.load_restore_token(state), 'rotated')
            self.assertEqual(len(list(Path(temporary).iterdir())), 1)

    def test_insecure_or_symlink_restore_state_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / 'state.json'
            probe.save_restore_token(state, 'test-token')
            state.chmod(0o644)
            with self.assertRaises(RuntimeError):
                probe.load_restore_token(state)
            link = Path(temporary) / 'link'
            link.symlink_to(state)
            with self.assertRaises(OSError):
                probe.load_restore_token(link)
            fifo = Path(temporary) / 'fifo'
            os.mkfifo(fifo, 0o600)
            with self.assertRaises(RuntimeError):
                probe.load_restore_token(fifo)

    def test_invalid_token_cannot_replace_valid_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / 'state.json'
            probe.save_restore_token(state, 'keep')
            for invalid in ['', None, 1, 'x' * 4097]:
                with self.assertRaises(RuntimeError):
                    probe.save_restore_token(state, invalid)
                self.assertEqual(probe.load_restore_token(state), 'keep')

    def test_capture_modes_are_mutually_exclusive(self):
        result = subprocess.run(['/usr/bin/python3', str(SCRIPT), '--native-dmabuf', '--capture-check'],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)

    def test_help_does_not_request_capture(self):
        result = subprocess.run(['/usr/bin/python3', str(SCRIPT), '--help'],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertIn('--capture-check', result.stdout)
        self.assertNotIn('Portal CreateSession:', result.stderr)

    def test_unknown_mode_is_rejected_before_portal_access(self):
        result = subprocess.run(['/usr/bin/python3', str(SCRIPT), '--cpu-fallback'],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('Portal CreateSession:', result.stderr)

    @unittest.skipUnless(BINARY.is_file(), 'Build the native PipeWire probe first')
    def test_native_invalid_arguments_fail_before_capture(self):
        for args in [[], ['-1', '1'], ['0', '1'], ['3', '0'],
                     ['3', '1', '--cpu-fallback']]:
            with self.subTest(args=args):
                result = subprocess.run([str(BINARY), *args], capture_output=True,
                                        text=True, timeout=5)
                self.assertEqual(result.returncode, 2)
                self.assertIn('usage:', result.stderr)


if __name__ == '__main__':
    unittest.main()
