"""Read-only KScreen display RPC served through the shared display router."""
import copy
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))

from native_display_guardian import DisplayRouter  # noqa: E402
from native_display_kscreen import (KScreenDisplay, ReadOnlyDisplayError, SelectedKScreenDisplay,  # noqa: E402
                                    UnavailableVirtualDisplays)
from test_desktop_backend import two_outputs  # noqa: E402


class KScreenDisplayTests(unittest.TestCase):
    def setUp(self):
        self.state = two_outputs()
        self.directory = tempfile.TemporaryDirectory()
        self.parent = Path(self.directory.name)
        self.parent.chmod(0o700)
        owner = lambda: ':1.42'  # noqa: E731
        reader = lambda: copy.deepcopy(self.state)  # noqa: E731
        self.backend = KScreenDisplay(reader, owner)
        self.router = DisplayRouter(self.backend, self.parent,
                                    selected_factory=lambda connector: SelectedKScreenDisplay(connector, reader, owner))

    def tearDown(self):
        self.directory.cleanup()

    def test_inspect_lists_every_output_for_the_windows_shim(self):
        reply = self.router.request(dict(version=1, op='inspect'))
        self.assertEqual([item['connector'] for item in reply['monitors']], ['DP-2', 'DP-1'])
        self.assertEqual((reply['width'], reply['height'], reply['scale']), (3840, 2160, 1.4))
        self.assertFalse(reply['pending'])
        for item in reply['monitors']:
            self.assertNotIn('input', item)
            self.assertEqual(sum(mode['width'] == item['width'] and mode['height'] == item['height'] and
                                 mode['refresh'] == item['refresh'] for mode in item['modes']), 1)
        self.assertEqual(sum(item['primary'] for item in reply['monitors']), 1)

    def test_secondary_output_is_inspected_by_its_own_connector(self):
        reply = self.router.request(dict(version=1, op='inspect', output=1))
        self.assertEqual((reply['width'], reply['height'], reply['refresh']), (2560, 1440, 144.0))

    def test_mode_changes_are_refused_without_a_pending_transaction(self):
        serial = self.router.request(dict(version=1, op='inspect'))['serial']
        for op in ('verify', 'apply'):
            with self.subTest(op=op), self.assertRaises(ReadOnlyDisplayError):
                self.router.request(dict(version=1, op=op, serial=serial, width=3840, height=2160,
                                         refresh=144.0, scale=1.4))
            with self.subTest(op=op, advertised=False), self.assertRaises(ValueError):
                self.router.request(dict(version=1, op=op, serial=serial, width=3840, height=2160,
                                         refresh=144.0, scale=1.0))
        self.assertTrue(all(item.transaction.pending is None for item in self.router.guardians.values()))

    def test_a_removed_output_cannot_be_inspected(self):
        self.router.request(dict(version=1, op='inspect', output=1))
        self.state['outputs'][0]['enabled'] = False
        with self.assertRaises(ValueError):
            self.router.request(dict(version=1, op='inspect', output=1))

    def test_windows_shim_accepts_the_inspect_reply(self):
        executable = self.parent / 'dump'
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-I', str(ROOT / 'src'),
                        str(ROOT / 'tests/probes/native_display_topology_dump.c'),
                        str(ROOT / 'src/native_display_client.c'), '-ljson-c', '-o', str(executable)],
                       check=True, capture_output=True, timeout=30)
        endpoint = self.parent / 'display.sock'
        payload = json.dumps(dict(ok=True, **self.router.request(dict(version=1, op='inspect')),
                                  virtual_displays=UnavailableVirtualDisplays().status())).encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as server:
            server.bind(str(endpoint)); endpoint.chmod(0o600); server.listen(1); server.settimeout(5)

            def reply():
                with server.accept()[0] as client:
                    client.recv(2048)
                    client.sendall(payload)
            worker = threading.Thread(target=reply)
            worker.start()
            result = subprocess.run([str(executable), str(endpoint)], capture_output=True, text=True, timeout=10)
            worker.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [line.split() for line in result.stdout.splitlines()]
        self.assertEqual([row[0] for row in rows], ['\\\\.\\DISPLAY1', '\\\\.\\DISPLAY2'])
        self.assertEqual(rows[0][1:], ['0', '0', '3840', '2160', '144000', '1400', '1'])
        self.assertEqual(rows[1][3:], ['2560', '1440', '144000', '1000', '0'])

    def test_virtual_displays_are_reported_unsupported(self):
        virtual = UnavailableVirtualDisplays()
        self.assertFalse(virtual.status()['supported'])
        self.assertEqual(virtual.status()['managed_connectors'], [])
        self.assertEqual(virtual.session_state(1234, 2), dict(completed=True))
        with self.assertRaises(ValueError):
            virtual.session_state(1234, 9)
        with self.assertRaises(RuntimeError):
            virtual.ioctl(0, [])


if __name__ == '__main__':
    unittest.main()
