"""Read-only Plasma topology and service policy checks."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('desktop_backend', ROOT / 'scripts/desktop_backend.py')
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


def fixture():
    return dict(outputs=[dict(name='eDP-1', connected=True, enabled=True, currentModeId='1',
                              modes=[dict(id='1', size=dict(width=1920, height=1080), refreshRate=60.0)],
                              size=dict(width=1920, height=1080), pos=dict(x=0, y=0),
                              scale=1.25, rotation=1, replicationSource=0)])


def two_outputs():
    def output(name, x, width, height, scale, mode_id, refresh, priority):
        size = dict(width=width, height=height)
        return dict(name=name, connected=True, enabled=True, currentModeId=mode_id,
                    modes=[dict(id=mode_id, size=dict(size), refreshRate=refresh)],
                    size=size, pos=dict(x=x, y=0), scale=scale, rotation=1, replicationSource=0,
                    priority=priority)
    return dict(outputs=[output('DP-1', 2743, 2560, 1440, 1, '2', 359.9989929199219, 2),
                         output('DP-2', 0, 3840, 2160, 1.4, '20', 144.0500030517578, 1)])


class PlasmaTests(unittest.TestCase):
    def test_topology_query_does_not_force_accessibility_or_change_parent_environment(self):
        for forced in (None, '', '0', '1'):
            environment = dict(QT_ACCESSIBILITY='1', NO_AT_BRIDGE='0',
                               AT_SPI_BUS_ADDRESS='unix:path=/fixture/accessibility-bus',
                               WAYLAND_DISPLAY='wayland-0',
                               DBUS_SESSION_BUS_ADDRESS='unix:path=/fixture/session-bus')
            if forced is not None:
                environment['QT_LINUX_ACCESSIBILITY_ALWAYS_ON'] = forced
            with self.subTest(forced=forced), patch.dict(os.environ, environment, clear=True), \
                    patch.object(backend.subprocess, 'run',
                                 return_value=Mock(stdout=json.dumps(fixture()))) as run:
                before = dict(os.environ)
                self.assertEqual(backend.current_plasma_topology()['connectors'], ['eDP-1'])
                args, kwargs = run.call_args
                self.assertEqual(args[0], ['kscreen-doctor', '-j'])
                self.assertNotIn('QT_LINUX_ACCESSIBILITY_ALWAYS_ON', kwargs['env'])
                self.assertNotIn('AT_SPI_BUS_ADDRESS', kwargs['env'])
                self.assertEqual(kwargs['env']['QT_ACCESSIBILITY'], '0')
                self.assertEqual(kwargs['env']['NO_AT_BRIDGE'], '1')
                self.assertEqual(kwargs['env']['WAYLAND_DISPLAY'], environment['WAYLAND_DISPLAY'])
                self.assertEqual(kwargs['env']['DBUS_SESSION_BUS_ADDRESS'], environment['DBUS_SESSION_BUS_ADDRESS'])
                self.assertEqual(dict(os.environ), before)

    def test_user_wine_is_used_only_when_system_wine_is_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            system, local = Path(directory) / 'system', Path(directory) / 'local'
            system.mkdir()
            local.mkdir()
            # The packaged path is host state: on a machine that has uuway-git
            # installed it exists, which would otherwise short-circuit the call.
            with patch.object(backend, '_packaged_wine_directory', return_value=Path(directory) / 'packaged'):
                self.assertEqual(backend.wine_bin_directory(system, local), system)
                (local / 'wine').touch()
                self.assertEqual(backend.wine_bin_directory(system, local), local)
                (system / 'wine').touch()
                self.assertEqual(backend.wine_bin_directory(system, local), system)

    def test_session_detection(self):
        self.assertTrue(backend.plasma_wayland(dict(XDG_SESSION_TYPE='wayland', XDG_CURRENT_DESKTOP='KDE')))
        self.assertFalse(backend.plasma_wayland(dict(XDG_SESSION_TYPE='x11', XDG_CURRENT_DESKTOP='KDE')))
        self.assertFalse(backend.plasma_wayland(dict(XDG_SESSION_TYPE='wayland', XDG_CURRENT_DESKTOP='GNOME')))

    def test_single_output_returns_stable_generation_and_pixel_dimensions(self):
        data = fixture()
        first = backend.plasma_topology(data)
        self.assertEqual(first, backend.plasma_topology(data))
        self.assertEqual(first['connectors'], ['eDP-1'])
        self.assertEqual(first['modes'], [dict(width=1920, height=1080)])
        self.assertEqual(first['layout'][0]['scale'], 1.25)
        self.assertEqual(first['input_map'], '0,0,1920,1080,0,0,65535,65535')
        data['outputs'][0]['currentModeId'] = '2'
        data['outputs'][0]['modes'].append(dict(id='2', size=dict(width=1920, height=1080), refreshRate=50.0))
        self.assertNotEqual(first['generation'], backend.plasma_topology(data)['generation'])

    def test_mixed_scale_outputs_map_windows_pixels_onto_kwin_logical_space(self):
        # The owner's desk: a 4K panel at 140 % at the origin and a 1440p
        # 360 Hz panel to its right. KWin spans 2743 + 2560 logical pixels.
        data = two_outputs()
        layout = backend.plasma_layout(data)
        first, second = layout['monitors']
        self.assertEqual((first['connector'], first['gdi_name'], first['primary']), ('DP-2', '\\\\.\\DISPLAY1', True))
        self.assertEqual((first['x'], first['y'], first['width'], first['height']), (0, 0, 3840, 2160))
        self.assertEqual((second['connector'], second['primary']), ('DP-1', False))
        self.assertGreaterEqual(second['x'], first['x'] + first['width'])
        self.assertEqual((second['width'], second['height']), (2560, 1440))
        # The Windows display ABI accepts at most 144 Hz.
        self.assertEqual((first['refresh'], second['refresh']), (144.0, 144.0))
        self.assertEqual(second['modes'], [dict(width=2560, height=1440, refresh=144.0, scales=[1.0])])
        total = 2743 + 2560
        self.assertAlmostEqual(first['input']['width'] / 65535 * total, 3840 / 1.4, delta=1)
        self.assertAlmostEqual(second['input']['x'] / 65535 * total, 2743, delta=1)
        self.assertEqual(first['input']['height'], 65535)
        self.assertAlmostEqual(second['input']['height'] / 65535 * (2160 / 1.4), 1440, delta=1)
        topology = backend.plasma_topology(data)
        self.assertEqual(topology['connectors'], ['DP-2'])
        self.assertEqual(topology['input_map'].count(';'), 1)

    def test_any_output_change_changes_the_compared_topology(self):
        # The worker ignores ``generation`` when comparing, so a change to a
        # secondary output must show up in the compared input map.
        data = two_outputs()
        before = backend.plasma_topology(data)
        data['outputs'][0]['pos']['y'] = 200
        after = backend.plasma_topology(data)
        self.assertEqual(before['connectors'], after['connectors'])
        self.assertNotEqual(before['input_map'], after['input_map'])

    def test_windows_rectangles_never_overlap_after_rounding(self):
        for scale, width in ((1.4, 3840), (1.5, 1920), (1.25, 2560), (1.75, 3840), (2.0, 4096)):
            data = two_outputs()
            right = data['outputs'][0]
            left = data['outputs'][1]
            left.update(scale=scale, size=dict(width=width, height=1080))
            left['modes'][0]['size'] = dict(width=width, height=1080)
            right['pos']['x'] = round(width / scale)
            first, second = backend.plasma_layout(data)['monitors']
            with self.subTest(scale=scale, width=width):
                self.assertGreaterEqual(second['x'], first['x'] + first['width'])

    def test_primary_is_display_one_even_away_from_the_origin(self):
        data = two_outputs()
        data['outputs'][0]['priority'], data['outputs'][1]['priority'] = 1, 2
        first, second = backend.plasma_layout(data)['monitors']
        self.assertEqual(first['connector'], 'DP-1')
        self.assertEqual((first['x'], first['y']), (0, 0))
        self.assertLess(second['x'], 0)
        self.assertLessEqual(second['x'] + second['width'], 0)

    def test_outputs_windows_cannot_show_stay_in_the_kwin_bounding_box(self):
        data = two_outputs()
        data['outputs'][0]['rotation'] = 2
        monitors = backend.plasma_layout(data)['monitors']
        self.assertEqual([item['connector'] for item in monitors], ['DP-2'])
        # KWin still spans the rotated output, so DP-2 keeps its share of the axis.
        self.assertAlmostEqual(monitors[0]['input']['width'] / 65535, 2743 / (2743 + 1440), delta=0.001)

    def test_layouts_without_a_streamable_output_are_rejected(self):
        changes = [lambda d: d['outputs'][0].update(rotation=2),
                   lambda d: d['outputs'][0].update(size=dict(width=1919, height=1080)),
                   lambda d: d['outputs'][0].update(replicationSource=1),
                   lambda d: d['outputs'][0].update(enabled=False)]
        for change in changes:
            data = fixture()
            change(data)
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                backend.plasma_topology(data)

    def test_malformed_outputs_are_rejected(self):
        changes = [lambda d: d['outputs'][0].update(scale=float('nan')),
                   lambda d: d['outputs'][0].update(connected=False),
                   lambda d: d['outputs'][0]['pos'].update(x='0'),
                   lambda d: d.update(outputs=[])]
        for change in changes:
            data = fixture()
            change(data)
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                backend.plasma_layout(data)

    def test_more_outputs_than_windows_enumerates_are_rejected(self):
        data = fixture()
        template = data['outputs'][0]
        data['outputs'] = [dict(template, name='DP-%d' % index, pos=dict(x=1536 * index, y=0), priority=index + 1)
                           for index in range(9)]
        with self.assertRaises(RuntimeError):
            backend.plasma_layout(data)
        data['outputs'] = data['outputs'][:8]
        self.assertEqual(len(backend.plasma_layout(data)['monitors']), 8)

    def test_runtime_uses_kscreen_only_on_plasma(self):
        spec = importlib.util.spec_from_file_location('kde_trial', ROOT / 'scripts/uu-native-trial.py')
        trial = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(trial)
        with patch.object(trial, 'plasma_wayland', return_value=True), \
                patch.object(trial, 'current_plasma_topology', return_value=backend.plasma_topology(fixture())) as query:
            self.assertEqual(trial.native_topology()['connectors'], ['eDP-1'])
            query.assert_called_once_with()
