"""Portal-backed virtual displays for Plasma Wayland.

UU's IDD feature ("Super Screen", 防窥/隐私屏, 关闭被控端显示屏) asks the host
for an extra output.  Plasma 6.8's ScreenCast portal can create one at runtime:
``SelectSources(types=VIRTUAL)`` makes KWin add a real virtual output, and the
returned PipeWire stream is that output's content.  KScreen can then enable,
position and prioritise it like any other output, and disabling the physical
outputs makes the desktop migrate onto the virtual one while the panel goes
dark.

That replaces the Mutter ``RecordVirtual`` backend.  The public RPC surface is
identical to :class:`native_virtual_display.VirtualDisplays` so the display
service only swaps the object.  Differences on purpose:

* The virtual output's resolution is fixed by the portal session, not by a
  Mutter mode negotiation, so the advertised mode catalogue is narrowed to the
  mode the output really has.
* KDE display configuration is a real layout change instead of a temporary
  Mutter transaction.  The pre-change layout is journalled to
  ``virtual-layout-recovery.json``; a service crash or an unplugged virtual
  session puts the physical outputs back instead of leaving the panel dark.
"""
import copy
import json
import os
from pathlib import Path
import secrets
import select
import socket
import struct
import subprocess
import time

from desktop_backend import plasma_layout, plasma_state

SERVICE = 'org.freedesktop.portal.Desktop'
PATH = '/org/freedesktop/portal/desktop'
SCREENCAST = 'org.freedesktop.portal.ScreenCast'
SESSION_INTERFACE = 'org.freedesktop.portal.Session'
REGISTRY_INTERFACE = 'org.freedesktop.host.portal.Registry'
SOURCE_VIRTUAL = 4
CURSOR_EMBEDDED = 2
PERSIST_WHILE_RUNNING = 2
KWIN_SERVICE = 'org.kde.KWin'
ADAPTER_NAME = 'GameViewer Virtual Display Adapter'
MAX_MODES = 64
MAX_REFRESH_HZ = 144
AUTHORIZATION_SCRIPT = 'authorize-kde-virtual-display.py'
# 0x55520001 is UUWay's internal bridge RPC carrying UU's SetDisplayConfig
# request; the 0x30c0xx codes are UU's reviewed IDD control messages.
TOPOLOGY_CONTROL = 0x55520001
KSCREEN_JSON_TIMEOUT = 5
KScreen = '/usr/bin/kscreen-doctor'


def _kscreen(args):
    """Run one bounded KScreen change and raise a bounded error on failure."""
    result = subprocess.run([KScreen, *args], capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        reason = (result.stderr or result.stdout or '').strip().splitlines()
        raise RuntimeError('KScreen rejected %s: %s' % (
            args[0] if args else 'request', (reason[-1] if reason else 'exit %d' % result.returncode)[:200]))
    return result.stdout


def _outputs():
    data = plasma_state()
    return [item for item in data.get('outputs') or [] if isinstance(item, dict)]


def _size(output):
    size = output.get('size') or {}
    width, height = size.get('width'), size.get('height')
    return width, height


def _position(output):
    pos = output.get('pos') or {}
    return pos.get('x'), pos.get('y')


def _active(output):
    """Portal-owned outputs omit ``enabled``; KScreen still renders them."""
    return output.get('enabled') is True or str(output.get('name', '')).startswith('Virtual-')


def _mode_refresh(output):
    current = output.get('currentModeId')
    for mode in output.get('modes') or []:
        if isinstance(mode, dict) and mode.get('id') == current:
            refresh = mode.get('refreshRate')
            if isinstance(refresh, (int, float)) and refresh > 0:
                return min(round(float(refresh), 3), float(MAX_REFRESH_HZ))
    return None


def _mode_entry(width, height, refresh):
    return dict(id='%dx%d@%.3f' % (width, height, refresh), width=width, height=height,
                refresh=float(refresh), scales=[1.0])


def validate_modes(modes):
    """UU's catalogue, bounded exactly like the Mutter backend."""
    if not isinstance(modes, (list, tuple)) or not 1 <= len(modes) <= MAX_MODES:
        raise ValueError('Expected 1 to 64 virtual display modes')
    result = []
    for index, mode in enumerate(modes):
        if (not isinstance(mode, (list, tuple)) or len(mode) != 3 or
                any(type(value) is not int for value in mode)):
            raise ValueError('Invalid virtual mode tuple at index %d' % index)
        if (any(not 320 <= value <= 4096 or value % 2 for value in mode[:2]) or
                not 1 <= mode[2] <= MAX_REFRESH_HZ):
            raise ValueError('Unsupported virtual mode at index %d: %dx%d@%d' % (*mode,))
        if tuple(mode) not in result:
            result.append(tuple(mode))
    return result


class _Portal:
    """Minimal ScreenCast portal client bound to one D-Bus connection."""

    def __init__(self, bus, Gio, GLib):
        self.bus, self.Gio, self.GLib = bus, Gio, GLib
        self.sender = bus.get_unique_name()[1:].replace('.', '_')
        self.restore_path = None

    def register(self):
        try:
            self.bus.call_sync(SERVICE, PATH, REGISTRY_INTERFACE, 'Register',
                               self.GLib.Variant('(sa{sv})', ('uuway', {})), None,
                               self.Gio.DBusCallFlags.NONE, 10000, None)
        except self.GLib.Error:
            pass

    def request(self, method, signature, arguments, options, timeout=2):
        token = 'uurb_' + secrets.token_hex(12)
        path = '%s/request/%s/%s' % (PATH, self.sender, token)
        options = dict(options, handle_token=self.GLib.Variant('s', token))
        loop = self.GLib.MainLoop()
        response = []

        def receive(connection, name, object_path, interface, signal, parameters, user_data):
            response.append(parameters.unpack())
            loop.quit()

        subscription = self.bus.signal_subscribe(SERVICE, 'org.freedesktop.portal.Request', 'Response',
                                                 path, None, self.Gio.DBusSignalFlags.NONE, receive, None)
        deadline = self.GLib.timeout_add_seconds(timeout, lambda: (loop.quit(), False)[1])
        try:
            returned = self.bus.call_sync(SERVICE, PATH, SCREENCAST, method,
                                          self.GLib.Variant(signature, (*arguments, options)), None,
                                          self.Gio.DBusCallFlags.NONE, min(timeout * 1000, 10000), None).unpack()[0]
            if returned != path:
                raise RuntimeError('Unexpected portal request handle')
            if not response:
                loop.run()
            if not response:
                raise RuntimeError('Screen share request %s timed out' % method)
            code, values = response[0]
            if code:
                raise RuntimeError('Screen share %s was cancelled or failed (response %d)' % (method, code))
            return values
        finally:
            self.bus.signal_unsubscribe(subscription)
            if self.GLib.MainContext.default().find_source_by_id(deadline):
                self.GLib.source_remove(deadline)
            if not response:
                try:
                    self.bus.call_sync(SERVICE, path, 'org.freedesktop.portal.Request', 'Close',
                                       None, None, self.Gio.DBusCallFlags.NONE, 3000, None)
                except self.GLib.Error:
                    pass

    def open(self, restore_path, interactive=False, expected_size=None):
        """Create one virtual-screen session; returns (session, node, size, token)."""
        token = load_token(restore_path) if restore_path is not None else None
        if not interactive and token is None:
            raise RuntimeError('Run %s before enabling virtual displays' % AUTHORIZATION_SCRIPT)
        timeout = 120 if interactive else 2
        self.register()
        values = self.request('CreateSession', '(a{sv})', (),
                              {'session_handle_token': self.GLib.Variant('s', 'uurb_' + secrets.token_hex(12))},
                              timeout=timeout)
        session = values['session_handle']
        selection = {'types': self.GLib.Variant('u', SOURCE_VIRTUAL),
                     'multiple': self.GLib.Variant('b', False),
                     'cursor_mode': self.GLib.Variant('u', CURSOR_EMBEDDED),
                     'persist_mode': self.GLib.Variant('u', PERSIST_WHILE_RUNNING)}
        if token:
            selection['restore_token'] = self.GLib.Variant('s', token)
        try:
            self.request('SelectSources', '(oa{sv})', (session,), selection, timeout=timeout)
            started = self.request('Start', '(osa{sv})', (session, ''), {}, timeout=timeout)
        except BaseException:
            self.close(session)
            raise
        streams = started.get('streams') or []
        if len(streams) != 1:
            self.close(session)
            raise RuntimeError('Expected exactly one virtual screen stream')
        node, properties = streams[0]
        if not 0 < int(node) <= 0xffffffff:
            self.close(session)
            raise RuntimeError('Invalid virtual screen stream node')
        size = properties.get('size') if isinstance(properties, dict) else None
        width, height = (size or (0, 0))[:2]
        if not (320 <= int(width) <= 4096 and 320 <= int(height) <= 4096):
            self.close(session)
            raise RuntimeError('Unexpected virtual screen size')
        if expected_size is not None and (int(width), int(height)) != expected_size:
            self.close(session)
            raise RuntimeError('Select %dx%d in the KDE authorization dialog' % expected_size)
        if restore_path is not None and started.get('restore_token'):
            save_token(restore_path, started['restore_token'])
        return dict(session=session, node=int(node), width=int(width), height=int(height),
                    mapping=str(properties.get('mapping_id') or '')[:128])

    def close(self, session):
        try:
            self.bus.call_sync(SERVICE, session, SESSION_INTERFACE, 'Close', None, None,
                               self.Gio.DBusCallFlags.NONE, 3000, None)
        except self.GLib.Error:
            pass


def load_token(path):
    try:
        info = path.lstat()
        if not path.is_file() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            return None
        value = json.loads(path.read_text())
        token = value.get('restore_token') if isinstance(value, dict) else None
        return token if isinstance(token, str) and 0 < len(token) <= 4096 else None
    except (OSError, ValueError):
        return None


def save_token(path, token):
    temp = path.with_name(path.name + '.new')
    temp.write_text(json.dumps(dict(version=1, restore_token=token)))
    os.chmod(temp, 0o600)
    os.replace(temp, path)


class KScreenVirtualDisplays:
    """UU's virtual display surface, backed by a portal virtual output."""

    available = True

    def __init__(self, backend, portal_state, recovery_path, capture_binary=None):
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.backend = backend
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.portal = _Portal(self.bus, Gio, GLib)
        self.portal_state = Path(portal_state)
        self.recovery_path = Path(recovery_path) if recovery_path is not None else None
        self.capture_binary = capture_binary
        self.screens = {}
        self.modes = {index: [(1920, 1080, 60)] for index in range(8)}
        self.exclusive_baseline = None
        self.owner_fd = None
        self.owner_pid = None
        self.parent_fd = None
        self.session_seen = False
        self.idle_since = None
        self.session_updated = 0
        self.managed_connectors = set()
        self.display_ids = {}

    # -- events ---------------------------------------------------------
    def _event(self, name, **fields):
        value = dict(event='virtual_' + name)
        for key, item in fields.items():
            if isinstance(item, (str, int, bool)):
                value[key] = item
        print(json.dumps(value, sort_keys=True), flush=True)

    def pump(self):
        context = self.GLib.MainContext.default()
        while context.pending():
            context.iteration(False)

    # -- lookup ---------------------------------------------------------
    def _id_map(self):
        return {identity: connector for connector, identity in self.display_ids.items()}

    def find(self, connector):
        return next((screen for screen in self.screens.values() if screen['connector'] == connector), None)

    def entries(self, screen):
        return [_mode_entry(screen['width'], screen['height'], screen['refresh'])]

    # -- lifecycle ------------------------------------------------------
    def create(self, slot, modes=None):
        if type(slot) is not int or not 0 <= slot < 8:
            raise ValueError('Invalid virtual display slot')
        if slot in self.screens:
            return self.screens[slot]
        if modes is not None:
            self.modes[slot] = validate_modes(modes)
        before = {item['name']: item for item in _outputs()}
        if len([name for name, item in before.items() if _active(item)]) >= 8:
            raise ValueError('At most eight active displays are supported')
        baseline = self._raw_layout()
        self._event('create_begin', slot=slot, active_before=len(before))
        self.portal_state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        created = self.portal.open(self.portal_state)
        stage = 'identify_output'
        try:
            connector = self._wait_connector(before, created, timeout=5)
            stage = 'position_output'
            refresh = self._refresh_for(connector)
            actual = (created['width'], created['height'], int(round(refresh)))
            catalogue = [mode for mode in self.modes.get(slot, []) if tuple(mode) != actual]
            self.modes[slot] = [actual] + catalogue
            self._place(connector, created['width'], baseline)
            primary = next((item for item in baseline['outputs'] if item['priority'] == 1), None)
            if primary is not None:
                _kscreen(['output.%s.priority.1' % primary['connector']])
            stage = 'record'
            screen = dict(slot=slot, session=created['session'], node=created['node'],
                          connector=connector, width=created['width'], height=created['height'],
                          refresh=refresh, baseline_topology=copy.deepcopy(baseline),
                          mapping=created['mapping'])
            self.screens[slot] = screen
            self.managed_connectors.add(connector)
            if self.idle_since is not None:
                self.idle_since = time.monotonic()
            self._event('create_complete', slot=slot, connector=connector, width=created['width'],
                        height=created['height'], refresh=int(round(refresh)))
            return screen
        except BaseException as error:
            self._event('create_failed', slot=slot, stage=stage, error_type=type(error).__name__)
            self.portal.close(created['session'])
            try:
                self._restore(baseline)
            except Exception:
                pass
            raise

    def _wait_connector(self, before, created, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump()
            fresh = [item for item in _outputs() if item['name'] not in before]
            candidates = [item for item in fresh if _size(item) == (created['width'], created['height'])]
            if len(candidates) == 1:
                return candidates[0]['name']
            time.sleep(0.1)
        raise RuntimeError('The portal virtual output did not appear in KScreen')

    def _refresh_for(self, connector):
        for item in _outputs():
            if item['name'] == connector:
                refresh = _mode_refresh(item)
                if refresh is not None:
                    return refresh
        return 60.0

    def _place(self, connector, width, baseline):
        """Enable the new output to the right of the current layout."""
        right = max((_position(item)[0] or 0) + (_size(item)[0] or 0)
                    for item in _outputs() if _active(item) and item['name'] != connector) if baseline else 0
        _kscreen(['output.%s.enable' % connector, 'output.%s.position.%d,0' % (connector, int(right) or width)])

    def remove(self, slot):
        if type(slot) is not int or not 0 <= slot < 8:
            raise ValueError('Invalid virtual display slot')
        screen = self.screens.pop(slot, None)
        if screen is None:
            return dict(removed=False)
        self._event('remove_begin', slot=slot, connector=screen['connector'])
        if self.exclusive_baseline is not None:
            self.restore_physical()
        baseline = screen.get('baseline_topology')
        self.portal.close(screen['session'])
        self.managed_connectors.discard(screen['connector'])
        if screen.get('owner_fd') is not None:
            os.close(screen['owner_fd'])
        self.pump()
        if baseline is not None:
            try:
                self._restore(baseline)
            except Exception as error:
                self._event('remove_failed', slot=slot, error_type=type(error).__name__,
                            reason=str(error)[:200])
                raise
        self._event('remove_complete', slot=slot)
        return dict(removed=True)

    def release_all(self):
        removed, failed = [], []
        for slot in reversed(tuple(self.screens)):
            try:
                if self.remove(slot).get('removed'):
                    removed.append(slot)
            except Exception as error:
                failed.append(slot)
                self._event('release_failed', slot=slot, error_type=type(error).__name__)
        return dict(released=removed, count=len(removed), failed=failed)

    def close(self):
        try:
            self.release_all()
        finally:
            for descriptor in ('owner_fd', 'parent_fd'):
                value = getattr(self, descriptor)
                if value is not None:
                    os.close(value)
                    setattr(self, descriptor, None)

    # -- layout ---------------------------------------------------------
    def _layout(self):
        """The topology UU sees: same serial and Windows-space geometry as the
        display snapshot, so a serial it read from `inspect` still matches."""
        state = plasma_layout(plasma_state())
        return dict(serial=state['serial'], outputs=[
            dict(connector=item['connector'], x=int(item['x']), y=int(item['y']),
                 width=int(item['width']), height=int(item['height']), mode=item.get('mode'),
                 refresh=float(item.get('refresh') or 60.0), priority=1 if item.get('primary') else 0)
            for item in state['monitors']])

    def _raw_layout(self):
        """KScreen-space layout used to restore a previous physical arrangement."""
        outputs = []
        for item in _outputs():
            if not _active(item):
                continue
            width, height = _size(item)
            if not (isinstance(width, int) and isinstance(height, int) and width and height):
                continue
            x, y = _position(item)
            outputs.append(dict(connector=item['name'], x=int(x or 0), y=int(y or 0),
                                width=width, height=height, mode=item.get('currentModeId'),
                                refresh=_mode_refresh(item) or 60.0,
                                priority=int(item.get('priority') or 0)))
        return dict(serial=self._layout()['serial'], outputs=outputs)

    def _restore(self, baseline):
        """Re-enable and reposition the physical outputs recorded before a change."""
        live = {item['name']: item for item in _outputs()}
        for item in baseline['outputs']:
            if item['connector'] not in live:
                continue
            if not _active(live[item['connector']]):
                _kscreen(['output.%s.enable' % item['connector']])
        for item in baseline['outputs']:
            if item['connector'] not in live:
                continue
            _kscreen(['output.%s.position.%d,%d' % (item['connector'], item['x'], item['y'])])
        primary = next((item for item in baseline['outputs'] if item['priority'] == 1), None)
        if primary is not None and primary['connector'] in live:
            _kscreen(['output.%s.priority.1' % primary['connector']])
        if self.exclusive_baseline is not None and not self.screens:
            self.exclusive_baseline = None
        if self.recovery_path is not None:
            self.recovery_path.unlink(missing_ok=True)

    def restore_physical(self):
        baseline = self.exclusive_baseline
        if baseline is None:
            return
        self._event('exclusive_restore_begin')
        self._restore(baseline)
        self.exclusive_baseline = None
        self._event('exclusive_restore_complete')

    def apply_topology(self, data):
        """UU's exclusive virtual topology: only the virtual output stays active."""
        if len(data) < 36:
            raise ValueError('Incomplete native topology')
        serial, flags, count = struct.unpack_from('<3I', data)
        if not 1 <= count <= 8 or len(data) != 12 + count * 24 or flags not in (0, 1):
            raise ValueError('Invalid bounded native topology')
        live = self._layout()
        raw = self._raw_layout()
        self._event('topology_received', requested_serial=serial, current_serial=live['serial'],
                    paths=count, verify=bool(flags),
                    catalogue=json.dumps([[entry[0], entry[1], entry[2], entry[3], entry[4], entry[5]]
                                          for entry in struct.iter_unpack('<4I2i', data[12:])],
                                         separators=(',', ':')))
        if serial != live['serial']:
            raise ValueError('Stale native topology')
        requested = list(struct.iter_unpack('<4I2i', data[12:]))
        # Windows source id 使用 connector 对应的固定身份。
        by_id = self._id_map()
        names = [by_id.get(entry[0]) for entry in requested]
        if None in names:
            raise ValueError('Unknown native output identity')
        owned = {screen['connector'] for screen in self.screens.values()}
        if not set(names) <= owned:
            if set(names) & owned:
                raise ValueError('Mixed physical and virtual topology is unsupported')
            if not flags:
                self.restore_physical()
            self._event('topology_physical_only', paths=count)
            return dict(completed=True)
        if len(set(names)) != count:
            raise ValueError('Duplicate native output')
        if sum(x == 0 and y == 0 for _, _, _, _, x, y in requested) != 1:
            raise ValueError('Virtual topology needs one origin output')
        for name, (_, width, height, refresh, x, y) in zip(names, requested):
            mode = next((entry for entry in self.entries(self.find(name))
                         if entry['width'] == width and entry['height'] == height and
                         abs(entry['refresh'] - refresh / 1000) <= 1), None)
            if mode is None:
                raise ValueError('Topology requests must select an advertised virtual mode')
            if x < 0 or y < 0:
                raise ValueError('Virtual topology must use non-negative origins')
        if flags:
            return dict(completed=True)
        if self.exclusive_baseline is None:
            self.exclusive_baseline = copy.deepcopy(raw)
            if self.recovery_path is not None:
                from native_runtime_state import write_private
                write_private(self.recovery_path, dict(version=1, identity=self.backend.identity(),
                                                      baseline=self.exclusive_baseline))
        self._event('exclusive_begin', outputs=len(requested))
        # Enable and place the selected outputs first: a desktop must always
        # keep one active output, so never disable the old one before the new
        # one is live.
        for name, (_, width, height, refresh, x, y) in zip(names, requested):
            _kscreen(['output.%s.enable' % name, 'output.%s.position.%d,%d' % (name, x, y)])
        primary = next(name for name, entry in zip(names, requested) if entry[4] == 0 and entry[5] == 0)
        _kscreen(['output.%s.priority.1' % primary])
        for item in raw['outputs']:
            if item['connector'] not in names:
                _kscreen(['output.%s.disable' % item['connector']])
        self._event('exclusive_complete', outputs=len(names))
        return dict(completed=True)

    def set_output(self, connector, enabled):
        if not isinstance(connector, str) or len(connector) > 128 or type(enabled) is not bool:
            raise ValueError('Invalid output selection')
        if self.find(connector):
            raise ValueError('Use virtual-remove for owned Super Screens')
        live = {item['name']: item for item in _outputs()}
        if connector not in live:
            raise ValueError('Output is no longer connected')
        if _active(live[connector]) == enabled:
            return dict(completed=True)
        if not enabled:
            remaining = [item for name, item in live.items() if name != connector and _active(item)]
            if not remaining:
                raise ValueError('The last active desktop output cannot be disabled')
            _kscreen(['output.%s.disable' % connector])
        else:
            x = max((_position(item)[0] or 0) + (_size(item)[0] or 0) for item in live.values() if _active(item))
            _kscreen(['output.%s.enable' % connector, 'output.%s.position.%d,0' % (connector, int(x))])
        self._event('output_changed', connector=connector, enabled=enabled, persistent=False)
        return dict(completed=True)

    # -- UU IDD wire ----------------------------------------------------
    def ioctl(self, code, payload):
        if type(code) is not int or not isinstance(payload, list) or len(payload) > 1024 or any(
                type(value) is not int or not 0 <= value <= 255 for value in payload):
            raise ValueError('Invalid bounded virtual display request')
        data = bytes(payload)
        if code == TOPOLOGY_CONTROL:
            try:
                return self.apply_topology(data)
            except (ValueError, RuntimeError) as error:
                if self.exclusive_baseline is not None:
                    try:
                        self.restore_physical()
                    except Exception:
                        pass
                self._event('topology_rejected', reason=str(error)[:200])
                raise
        if code == 0 and not data:
            if not self.screens and load_token(self.portal_state) is None:
                raise RuntimeError('KDE virtual display authorization is required')
            return self.status()
        if code == 0x30c014 and len(data) == 1:
            return dict(completed=True)
        if code == 0x30c004 and len(data) == 24:
            slot, edid_type = struct.unpack_from('<2I', data)
            if edid_type > 2:
                raise ValueError('Unknown virtual monitor description')
            self.create(slot)
            return dict(completed=True)
        if code == 0x30c008 and len(data) == 4:
            self.remove(struct.unpack('<I', data)[0])
            return dict(completed=True)
        if code in (0x30c00c, 0x30c010) and len(data) >= 20:
            slot, count = struct.unpack_from('<2I', data)
            if slot >= 8 or not 1 <= count <= MAX_MODES or len(data) != 8 + count * 12:
                raise ValueError('Invalid virtual monitor mode array')
            received = [list(mode) for mode in struct.iter_unpack('<3I', data[8:])]
            self._event('modes_received', slot=slot, count=count)
            requested = validate_modes(received)
            screen = self.screens.get(slot)
            actual = (screen['width'], screen['height'], int(round(screen['refresh']))) if screen else None
            modes = ([actual] if actual is not None else []) + [mode for mode in requested if mode != actual]
            self.modes[slot] = modes
            self._event('modes_accepted', slot=slot, count=len(modes),
                        portal_mode=json.dumps(list(actual)) if actual else None)
            return dict(completed=True)
        raise ValueError('Unsupported virtual display operation')

    # -- ownership ------------------------------------------------------
    def session_state(self, pid, tracks):
        if type(tracks) is not int or not 0 <= tracks <= 8:
            raise ValueError('Invalid remote video-track count')
        if pid != self.owner_pid:
            owner_fd = os.pidfd_open(pid)
            try:
                process_stat = (Path('/proc') / str(pid) / 'stat').read_text()
                parent_pid = int(process_stat[process_stat.rindex(')') + 2:].split()[1])
                parent_fd = os.pidfd_open(parent_pid)
            except BaseException:
                os.close(owner_fd)
                raise
            if self.owner_fd is not None:
                self.release_all()
                os.close(self.owner_fd)
            self.owner_fd, self.owner_pid = owner_fd, pid
            if self.parent_fd is not None:
                os.close(self.parent_fd)
            self.parent_fd = parent_fd
            self._event('session_owner', pid=pid)
        now = time.monotonic()
        if tracks:
            self.session_seen = True
            self.idle_since = None
        elif self.idle_since is None:
            self.idle_since = now
        self.session_updated = now
        return dict(completed=True)

    def bind_screen_owner(self, slot, pid):
        screen = self.screens.get(slot)
        if screen and screen.get('owner_pid') != pid:
            if screen.get('owner_fd') is not None:
                os.close(screen['owner_fd'])
            screen.update(owner_pid=pid, owner_fd=os.pidfd_open(pid))

    def poll_lifetime(self, allow_idle=True):
        now = time.monotonic()
        if self.owner_fd is not None and (select.select([self.owner_fd, self.parent_fd], [], [], 0)[0] or
                                          now - self.session_updated > 30):
            self._event('owner_lost', pid=self.owner_pid)
            self.release_all()
            os.close(self.owner_fd)
            self.owner_fd, self.owner_pid = None, None
            if self.parent_fd is not None:
                os.close(self.parent_fd)
                self.parent_fd = None
        for slot, screen in list(self.screens.items()):
            if screen.get('owner_fd') is not None and select.select([screen['owner_fd']], [], [], 0)[0]:
                self._event('uu_process_exited', slot=slot)
                try:
                    self.remove(slot)
                except Exception as error:
                    self._event('owner_cleanup_failed', slot=slot, error_type=type(error).__name__)
        if allow_idle and self.screens and self.idle_since is not None and now - self.idle_since >= 15:
            self._event('remote_tracks_closed', quiet_seconds=15)
            self.release_all()

    # -- display ABI ----------------------------------------------------
    def decorate(self, monitors):
        """Tag the virtual output and publish its real mode catalogue."""
        for screen in self.screens.values():
            entry = dict(connector=screen['connector'], gdi_name=None, virtual=True,
                         friendly=ADAPTER_NAME, slot=screen['slot'])
            target = next((item for item in monitors if item['connector'] == screen['connector']), None)
            if target is None:
                continue
            target.update(entry)
            target['modes'] = [dict(width=item['width'], height=item['height'],
                                    refresh=item['refresh'], scales=[1.0])
                               for item in self.entries(screen)]
        for item in monitors:
            if item['connector'] not in self.display_ids:
                used = set(self.display_ids.values())
                value = next((index for index in range(8) if index not in used), None)
                if value is None:
                    raise ValueError('Native display identity capacity exceeded')
                self.display_ids[item['connector']] = value
            value = self.display_ids[item['connector']]
            item['gdi_name'] = '\\\\.\\DISPLAY%d' % (value + 1)
            item['device_path'] = '\\\\?\\DISPLAY#UURB#%s#%d' % (item['connector'], value)
        return monitors

    def status(self):
        self.pump()
        outputs = []
        for item in _outputs():
            screen = self.find(item['name'])
            width, height = _size(item)
            outputs.append(dict(connector=item['name'], enabled=_active(item),
                                name=item['name'][:128],
                                kind='super_screen' if screen else 'portal' if str(item['name']).startswith('Virtual-')
                                     else 'physical',
                                managed=screen is not None,
                                slot=screen['slot'] if screen else None,
                                width=width, height=height,
                                refresh=_mode_refresh(item)))
        return dict(supported=bool(self.screens) or load_token(self.portal_state) is not None,
                    exclusive=self.exclusive_baseline is not None,
                    managed_connectors=sorted(self.managed_connectors), outputs=outputs,
                    tracks_idle=self.idle_since is not None,
                    cleanup_after_seconds=max(0, round(15 - (time.monotonic() - self.idle_since)))
                        if self.screens and self.idle_since is not None else None,
                    screens=[dict(slot=item['slot'], connector=item['connector'], node=item['node'])
                             for item in self.screens.values()])


def recover_layout(config):
    """Put the physical outputs back after an unclean display-service exit."""
    from native_runtime_state import read_private
    path = Path(config['state_parent']) / 'virtual-layout-recovery.json'
    if not path.exists():
        return 0
    record = read_private(path)
    if record.get('version') != 1:
        path.unlink(missing_ok=True)
        return 0
    baseline = record.get('baseline') or {}
    live = {item['name'] for item in _outputs()}
    restored = 0
    for item in baseline.get('outputs', []):
        if item['connector'] not in live:
            continue
        _kscreen(['output.%s.enable' % item['connector'],
                  'output.%s.position.%d,%d' % (item['connector'], item['x'], item['y'])])
        if item['priority'] == 1:
            _kscreen(['output.%s.priority.1' % item['connector']])
        restored += 1
    path.unlink(missing_ok=True)
    print(json.dumps(dict(event='virtual_crash_layout_restored', outputs=restored)), flush=True)
    return restored
