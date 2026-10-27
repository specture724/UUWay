#!/usr/bin/python3
"""Open UU login with temporary native loaders and restore them on exit."""
import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('login_trial', ROOT / 'scripts/uu-native-trial.py')
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)

LOGIN_DEADLINE_SECONDS = 3600
# The bootstrap applies the same grace before treating a missing server as gone.
SERVER_GONE_SECONDS = 15
# UU's server comes up within seconds; the bootstrap itself never exits, so a
# start that fails would otherwise keep hosting paused forever.
SERVER_START_SECONDS = 120


class QuitWatcher:
    """UU's tray Quit stops GameViewerServer.exe; while UU runs, Healthd restarts it within seconds."""

    def __init__(self, started: float) -> None:
        self.started = started
        self.seen = False
        self.missing_since: float | None = None

    def quit(self, server_alive: bool, now: float) -> bool:
        if server_alive:
            self.seen = True
            self.missing_since = None
            return False
        if not self.seen:
            return now - self.started >= SERVER_START_SECONDS
        if self.missing_since is None:
            self.missing_since = now
        return now - self.missing_since >= SERVER_GONE_SECONDS


def logged_in(prefix):
    return any(not path.name.startswith('setting_guest_') for path in
               prefix.glob('drive_c/users/*/AppData/Local/GameViewer/setting_*.ini'))


def run(prefix, bundle, state_parent, wait_for_user=False, until_quit=False):
    assert not (wait_for_user and until_quit)
    prefix = prefix.resolve()
    bundle = bundle.resolve()
    trial.private_directory(prefix)
    trial.bundle_tools.verify(bundle)
    state_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    trial.private_directory(state_parent)
    if trial.prefix_processes(prefix):
        raise RuntimeError('Quit UU before opening native login: the Wine prefix is busy')
    directory = Path(tempfile.mkdtemp(prefix='login-', dir=state_parent))
    env = trial.wine_locale(dict(os.environ, WINEPREFIX=str(prefix), WINELOADER=trial.WINE,
                                WINEDEBUG='-all', UURB_NATIVE_BOOTSTRAP='1',
                                WINEDLLOVERRIDES='mscoree,mshtml=', QT_OPENGL='software',
                                QT_ANGLE_PLATFORM='warp', __GLX_VENDOR_LIBRARY_NAME='nvidia'))
    for name in ('UURB_NATIVE_INPUT_ENABLED', 'QT_IM_MODULE', 'QT_LINUX_ACCESSIBILITY_ALWAYS_ON',
                 'QT_WAYLAND_RECONNECT', 'SDL_IM_MODULE'):
        env.pop(name, None)
    app = prefix / 'drive_c/Program Files/Netease/GameViewer'
    token = 'UURB-NATIVE-' + uuid.uuid4().hex
    arguments = [str(directory / 'bootstrap.exe'), '--run', str(directory / 'capture.sock'),
                 trial.winpath(bundle / 'dxvk.conf'), trial.winpath(app / 'GameViewer.exe'), token]
    created, launcher = [], None
    stopping = False

    def stop(*unused):
        nonlocal stopping
        stopping = True

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        for source, name in (('runtime/bootstrap.exe', 'bootstrap.exe'),
                             ('runtime/winlogon.exe', 'winlogon.exe')):
            shutil.copy2(bundle / source, directory / name)
        for name in trial.bundle_tools.INPUTS:
            if name.startswith('app/bin/'):
                path = app / 'bin' / Path(name).name
            elif name.startswith('system32/'):
                path = prefix / 'drive_c/windows/system32' / Path(name).name
            else:
                continue
            if path.exists() or path.is_symlink():
                raise RuntimeError('Refusing an existing loader: ' + path.name)
            target = (bundle / name).resolve()
            path.symlink_to(target)
            info = path.lstat()
            created.append((path, target, (info.st_dev, info.st_ino)))
        with (directory / 'wine.log').open('w') as log:
            launcher = subprocess.Popen([trial.WINE, *arguments], env=env, cwd=app,
                                        stdout=log, stderr=log, start_new_session=True)
        print('请在 UU 窗口中登录，登录成功后安装向导会自动继续。', flush=True)
        started = time.monotonic()
        restored = False
        watcher = QuitWatcher(started)
        # A session used to control other computers may legitimately last for hours.
        while not stopping and (until_quit or time.monotonic() - started < LOGIN_DEADLINE_SECONDS):
            if not restored and time.monotonic() - started >= 8:
                subprocess.run([trial.WINE, str(ROOT / 'build/native-presenter/uu-show-login.exe')],
                               env=env, timeout=15, check=True)
                restored = True
                if wait_for_user:
                    input('登录完成后从 UU 托盘完全退出，然后按回车继续…')
                    break
            if until_quit:
                if watcher.quit(trial.uu_server_alive(prefix), time.monotonic()):
                    break
            elif not wait_for_user and logged_in(prefix):
                break
            if launcher.poll() is not None:
                break
            time.sleep(1)
        if until_quit and not watcher.seen and not stopping:
            print(f'UU 没有在 {SERVER_START_SECONDS} 秒内启动（未出现 GameViewerServer.exe），已关闭窗口。',
                  file=sys.stderr, flush=True)
        success = logged_in(prefix) and not stopping and (watcher.seen or not until_quit)
    finally:
        failures = trial.cleanup_trial(launcher, None, env, directory, arguments, token, created)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if failures == ['stop_owned_prefix'] and not trial.prefix_processes(prefix):
            if {'event': 'configuration_removed', 'code': 0} in trial.boot_events(directory / 'wine.log'):
                failures = []
        if failures:
            raise RuntimeError('Native login cleanup failed; inspect ' + str(directory / 'cleanup.json'))
    return 0 if success else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, required=True)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--state-parent', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--wait-for-user', action='store_true')
    mode.add_argument('--until-quit', action='store_true',
                      help='keep the window open until UU is quit from its tray icon')
    args = parser.parse_args()
    return run(args.prefix, args.bundle, args.state_parent, args.wait_for_user, args.until_quit)


if __name__ == '__main__':
    raise SystemExit(main())
