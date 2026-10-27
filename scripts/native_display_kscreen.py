"""Read-only KScreen backend for the native display service on Plasma Wayland.

UU enumerates every enabled output through the same RPC that Mutter serves on
GNOME. KScreen has no transactional API with a rollback serial, so mode, DPI
and layout writes are refused and Super Screen stays unavailable.
"""
from pathlib import Path

from desktop_backend import plasma_layout, plasma_state

try:
    from gi.repository import Gio, GLib
except ImportError:  # pragma: no cover - the service requires PyGObject on a desktop
    Gio = None
    GLib = None

KWIN_SERVICE = 'org.kde.KWin'


class ReadOnlyDisplayError(RuntimeError):
    pass


def _kwin_owner():
    if Gio is None or GLib is None:
        raise RuntimeError('PyGObject (gi.repository Gio/GLib) is required for the KDE display service')
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    return bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
                         'GetNameOwner', GLib.Variant('(s)', (KWIN_SERVICE,)), None,
                         Gio.DBusCallFlags.NO_AUTO_START, 2000, None).unpack()[0]


class KScreenDisplay:
    def __init__(self, read_state=plasma_state, kwin_owner=_kwin_owner):
        self.read_state = read_state
        self.kwin_owner = kwin_owner

    def identity(self):
        # A restarted KWin may renumber outputs; never let a journal from the
        # previous compositor describe the new one.
        return [Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'kscreen', self.kwin_owner()]

    def monitors(self):
        return [{key: value for key, value in item.items() if key != 'input'}
                for item in plasma_layout(self.read_state())['monitors']]

    def _state(self, monitor, serial):
        return dict(serial=serial, connector=monitor['connector'], mode=monitor['mode'], scale=monitor['scale'],
                    modes=[dict(id=monitor['mode'], width=monitor['width'], height=monitor['height'],
                                refresh=monitor['refresh'], scales=[monitor['scale']])],
                    layout_mode=None, underscanning=False, supports_underscanning=False,
                    can_change_layout=False, multi_output=True)

    def current(self):
        layout = plasma_layout(self.read_state())
        return self._state(layout['monitors'][0], layout['serial'])

    def configure(self, state, target, verify_only):
        raise ReadOnlyDisplayError('KDE display modes are read-only')


class SelectedKScreenDisplay(KScreenDisplay):
    def __init__(self, connector, read_state=plasma_state, kwin_owner=_kwin_owner):
        super().__init__(read_state, kwin_owner)
        self.connector = connector

    def identity(self):
        return [*super().identity(), 'selected-output-v1', self.connector]

    def current(self):
        layout = plasma_layout(self.read_state())
        monitor = next((item for item in layout['monitors'] if item['connector'] == self.connector), None)
        if monitor is None:
            raise ValueError('Selected KDE output is no longer shown')
        return self._state(monitor, layout['serial'])


class UnavailableVirtualDisplays:
    """The display service's virtual-display surface with Super Screen disabled."""
    available = False
    recovery_path = None

    def pump(self):
        pass

    def poll_lifetime(self, allow_idle=True):
        pass

    def find(self, connector):
        return None

    def status(self):
        return dict(supported=False, exclusive=False, managed_connectors=[], outputs=[],
                    tracks_idle=True, cleanup_after_seconds=None, screens=[])

    def session_state(self, pid, tracks):
        if type(tracks) is not int or not 0 <= tracks <= 8:
            raise ValueError('Invalid remote video-track count')
        return dict(completed=True)

    def ioctl(self, code, payload):
        raise RuntimeError('Virtual display support is unavailable on KDE')

    def bind_screen_owner(self, slot, pid):
        raise RuntimeError('Virtual display support is unavailable on KDE')

    def create(self, slot, modes=None):
        raise RuntimeError('Virtual display support is unavailable on KDE')

    def remove(self, slot):
        raise RuntimeError('Virtual display support is unavailable on KDE')

    def release_all(self):
        return dict(released=[], count=0, failed=[])

    def set_output(self, connector, enabled):
        raise ReadOnlyDisplayError('KDE display layout is read-only')

    def close(self):
        pass
