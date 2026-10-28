import argparse
import json
from pathlib import Path
import socket
import time


def request(endpoint, payload):
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
        client.settimeout(5)
        client.connect(str(endpoint))
        client.sendall(json.dumps(payload).encode())
        packet, _, flags, _ = client.recvmsg(65536)
    if flags & socket.MSG_TRUNC:
        raise RuntimeError('Display response was truncated')
    return json.loads(packet)


def inspect(endpoint):
    result = request(endpoint, dict(version=1, op='inspect'))
    if result.get('ok') is not True:
        raise RuntimeError('Display inspection failed: %s' % result)
    return result


def observe(endpoint, duration):
    identities = {}
    previous = None
    deadline = time.monotonic() + duration
    while True:
        result = inspect(endpoint)
        monitors = result['monitors']
        names = set()
        for monitor in monitors:
            connector = monitor['connector']
            identity = (monitor['gdi_name'], monitor['device_path'])
            if connector in identities and identity != identities[connector]:
                raise RuntimeError('Display identity changed for %s' % connector)
            identities[connector] = identity
            if identity[0] in names:
                raise RuntimeError('Duplicate Windows display name')
            names.add(identity[0])
            current = (monitor['width'], monitor['height'], monitor['refresh'])
            modes = [(mode['width'], mode['height'], mode['refresh']) for mode in monitor['modes']]
            if current not in modes:
                raise RuntimeError('Current mode is absent from the mode catalogue')
            if monitor.get('slot') is not None:
                if modes != [current] or type(monitor.get('node')) is not int:
                    raise RuntimeError('Managed virtual display has invalid modes or stream node')
        snapshot = json.dumps(monitors, sort_keys=True)
        if snapshot != previous:
            print(json.dumps(dict(event='verified_display_snapshot', monitors=monitors)), flush=True)
            previous = snapshot
        if time.monotonic() >= deadline:
            return
        time.sleep(0.5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--socket', type=Path, required=True)
    parser.add_argument('--duration', type=float, default=0)
    parser.add_argument('--expect-unauthorized', action='store_true')
    arguments = parser.parse_args()
    if arguments.expect_unauthorized:
        reply = request(arguments.socket, dict(version=1, op='virtual-control', code=0, payload=[]))
        if reply.get('ok') is not False:
            raise RuntimeError('An unauthorized virtual display device was exposed')
        if inspect(arguments.socket)['virtual_displays']['supported'] is not False:
            raise RuntimeError('Unauthorized virtual displays were reported as supported')
        print('Unauthorized IDD discovery rejected; physical display inspection passed', flush=True)
    observe(arguments.socket, arguments.duration)
