"""Fan status and bounded manual control for Lenovo 21LG / NLCN37WW.

Requires acpi_call and root. Both fan targets must be supplied because the
firmware's manual/automatic selector is global. Only 3000 RPM was tested
on hardware; WQA5's fan-1 limits are used as conservative CLI limits for both.
"""

import argparse
import contextlib
import fcntl
import json
import math
import os
import re
import signal
import struct
import sys
import time
from pathlib import Path

from . import __version__

IDENTITY = {'sys_vendor': 'LENOVO', 'product_name': '21LG', 'bios_version': 'NLCN37WW'}
EC_PATH = r'\_SB.PCI0.LPC0.EC0'
ENDPOINT = Path('/proc/acpi/call')
LOCK_DIR = Path('/run/lock')


@contextlib.contextmanager
def locked(name, *, nonblocking=False):
    with (LOCK_DIR / f'thinkbook-fanctl-{name}.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        except BlockingIOError as error:
            raise RuntimeError('Another manual-control session is running.') from error
        yield


def check_hardware():
    for key, expected in IDENTITY.items():
        actual = (Path('/sys/class/dmi/id') / key).read_text().strip()
        if actual != expected:
            raise RuntimeError(f'Unsupported {key}: {actual!r}; expected {expected!r}.')
    if os.geteuid() != 0:
        raise RuntimeError(
            'Run with sudo: ACPI reads also require writing /proc/acpi/call.'
        )
    if not ENDPOINT.exists():
        raise RuntimeError(
            '/proc/acpi/call is missing; run sudo modprobe acpi_call first.'
        )


class FanController:
    def call(self, request):
        # Serialize the request/response pair across this utility's processes.
        with locked('acpi'):
            ENDPOINT.write_text(request + '\n')
            response = ENDPOINT.read_bytes().rstrip(b'\0\n').decode('ascii')
        if response.startswith('Error:'):
            raise RuntimeError(response)
        return response

    def wmi(self, operation, fan_id, rpm=0):
        payload = struct.pack('<HBBI', fan_id, 3, 4, rpm).hex()
        return int(self.call(f'\\_SB.GZFD.WMAE 0 {operation:#x} b{payload}'), 0)

    def field(self, fan_id, offset):
        if fan_id not in (1, 2) or offset not in (2, 4, 6, 0x25):
            raise ValueError('Unknown fan field.')
        index = ((0x80 + fan_id) << 8) | offset
        value = int(self.call(f'{EC_PATH}.FANG {index:#x}'), 0)
        if not 0 <= value <= 255:
            raise RuntimeError(f'Unexpected byte at {index:#x}: {value}')
        return value

    def capabilities(self):
        response = self.call(r'\_SB.GZFD.WQA5 0')
        if not re.fullmatch(
            r'\{\s*0x[0-9a-fA-F]{2}(?:,\s*0x[0-9a-fA-F]{2})*\s*\}', response
        ):
            raise RuntimeError(f'Unexpected capability response: {response}')
        block = bytes(int(x, 16) for x in re.findall(r'0x([0-9a-fA-F]{2})', response))
        if len(block) != 16:
            raise RuntimeError('Unexpected capability block length.')
        count, fan_id, maximum, minimum = struct.unpack('<IIII', block)
        if (count, fan_id) != (1, 1) or not 0 < minimum <= maximum <= 6400:
            raise RuntimeError('Capabilities differ from the analyzed firmware.')
        return {'count': count, 'id': fan_id, 'min_rpm': minimum, 'max_rpm': maximum}

    def mode(self):
        # B3 reads EC RAM 0x04ab; bit 3 is the shared automatic-control flag.
        response = self.call(f'{EC_PATH}.ERCD bb304ab000000')
        match = re.fullmatch(
            r'\{\s*0x([0-9a-fA-F]{2})(?:,\s*0x[0-9a-fA-F]{2}){7}\s*\}',
            response,
        )
        if not match:
            raise RuntimeError(f'Unexpected mode-byte response: {response!r}')
        raw = int(match.group(1), 16)
        return {
            'control_mode': 'auto' if raw & 0x08 else 'manual',
            'control_mode_raw': raw,
        }

    def status(self):
        fans = []
        for fan_id in (1, 2):
            fans.append(
                {
                    'id': fan_id,
                    'rpm': self.wmi(0x11, fan_id),
                    'indexed_rpm': self.field(fan_id, 4) * 100,
                    'target_rpm': self.field(fan_id, 0x25) * 100,
                    'drive_raw': self.field(fan_id, 2),
                    'flags_raw': self.field(fan_id, 6),
                }
            )
        return {'model': '21LG', 'bios': 'NLCN37WW', **self.mode(), 'fans': fans}

    def set_targets(self, first, second):
        if self.wmi(0x12, 1, first) != 1:
            raise RuntimeError('Fan 1 speed request failed.')
        # Exactly the tested six-byte ERCD format; returned bytes are not ACKs.
        payload = bytes((0x46, 0x86, second // 100, 0, 0, 0)).hex()
        self.call(f'{EC_PATH}.ERCD b{payload}')
        self.check_targets(first, second)

    def check_targets(self, first, second):
        actual = tuple(self.field(fan_id, 0x25) * 100 for fan_id in (1, 2))
        if actual != (first, second):
            raise RuntimeError(f'Fan targets changed or were not accepted: {actual}.')

    def auto(self):
        for attempt in range(3):
            try:
                if self.wmi(0x12, 1, 0) != 1:
                    raise RuntimeError('Automatic-control request failed.')
                return
            except (OSError, RuntimeError, ValueError) as error:
                if attempt == 2:
                    raise RuntimeError(
                        'Could not restore auto; reboot to reset fan control.'
                    ) from error
                time.sleep(0.2)


def emit(data, as_json, stage):
    data = dict(data, stage=stage)
    if as_json:
        print(json.dumps(data), flush=True)
        return
    print(f'{data["model"]} / {data["bios"]} — {stage}', flush=True)
    print(f'Control: {data["control_mode"]} (EC {data["control_mode_raw"]:#04x})')
    if 'capabilities' in data:
        caps = data['capabilities']
        print(
            f'WQA5: channel {caps["id"]}, {caps["min_rpm"]}–{caps["max_rpm"]} RPM (fan 1)'
        )
    print('Fan    WMI RPM    Indexed RPM    Target RPM    Drive    Flags')
    for fan in data['fans']:
        print(
            f'{fan["id"]:>3} {fan["rpm"]:>10} {fan["indexed_rpm"]:>14}'
            f' {fan["target_rpm"]:>13} {fan["drive_raw"]:>8} {fan["flags_raw"]:>#8x}'
        )
    print(flush=True)


def interrupted(signum, frame):
    raise KeyboardInterrupt


def restore_auto(controller):
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in signals}
    try:
        controller.auto()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def manual(controller, first, second, seconds, as_json):
    limits = controller.capabilities()
    for rpm in (first, second):
        if not limits['min_rpm'] <= rpm <= limits['max_rpm']:
            raise ValueError(
                f'CLI range for both fans: {limits["min_rpm"]}–{limits["max_rpm"]} RPM.'
                ' These limits are reported for fan 1.'
            )
    # Other readers and the auto command remain available during this session.
    with locked('manual', nonblocking=True):
        emit(controller.status(), as_json, 'before')
        deadline = time.monotonic() + seconds
        try:
            controller.set_targets(first, second)
            while True:
                controller.check_targets(first, second)
                emit(controller.status(), as_json, 'manual')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(1, remaining))
        finally:
            # Includes failure after a setter takes effect but before its response.
            restore_auto(controller)
        emit(controller.status(), as_json, 'auto_requested')


def positive_seconds(value):
    try:
        result = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError('Expected a number of seconds.') from error
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError(
            'Seconds must be finite and greater than zero.'
        )
    return result


def rpm_value(value):
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError('Expected integer RPM.') from error
    if not 100 <= result <= 6400 or result % 100:
        raise argparse.ArgumentTypeError(
            'RPM must be a multiple of 100; use auto to restore firmware control.'
        )
    return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog='thinkbook-fanctl', description=__doc__)
    parser.add_argument(
        '--version', action='version', version=f'%(prog)s {__version__}'
    )
    parser.set_defaults(command='status', json=False)
    output = argparse.ArgumentParser(add_help=False)
    output.add_argument(
        '--json',
        action='store_true',
        help='Emit JSON; watch/set emit one object per sample.',
    )
    commands = parser.add_subparsers(dest='command')
    commands.add_parser(
        'status',
        parents=[output],
        help='Read both fans, control mode, and WQA5 capabilities (default).',
    )
    watch = commands.add_parser(
        'watch', parents=[output], help='Read repeatedly; Ctrl+C exits.'
    )
    watch.add_argument(
        '--interval',
        type=positive_seconds,
        default=2,
        help='Seconds between samples (default: 2).',
    )
    setter = commands.add_parser(
        'set', parents=[output], help='Hold BOTH fan targets, then restore auto.'
    )
    setter.add_argument('fan1_rpm', type=rpm_value)
    setter.add_argument('fan2_rpm', type=rpm_value)
    setter.add_argument(
        '--seconds',
        type=positive_seconds,
        default=5,
        help='Hold duration (default: 5).',
    )
    commands.add_parser(
        'auto',
        parents=[output],
        help='Request firmware automatic control for BOTH fans.',
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        check_hardware()
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, interrupted)
        controller = FanController()
        command = args.command or 'status'
        if command == 'set':
            manual(controller, args.fan1_rpm, args.fan2_rpm, args.seconds, args.json)
        elif command == 'auto':
            restore_auto(controller)
            emit(controller.status(), args.json, 'auto_requested')
        elif command == 'watch':
            while True:
                emit(controller.status(), args.json, 'status')
                time.sleep(args.interval)
        else:
            data = controller.status()
            data['capabilities'] = controller.capabilities()
            emit(data, args.json, 'status')
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, ValueError) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
