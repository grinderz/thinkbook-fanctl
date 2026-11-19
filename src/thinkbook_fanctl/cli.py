"""Fan status and bounded manual control for Lenovo 21LG / NLCN37WW.

Requires acpi_call and root. Both fan targets must be supplied because the
firmware's manual/automatic selector is global. Only 3000 RPM was tested
on hardware; WQA5's fan-1 limits are used as conservative CLI limits for both.
"""

import argparse
import contextlib
import fcntl
import io
import json
import math
import os
import re
import signal
import struct
import sys
import time
from pathlib import Path
from threading import TIMEOUT_MAX

from . import __version__

IDENTITY = {'sys_vendor': 'LENOVO', 'product_name': '21LG', 'bios_version': 'NLCN37WW'}
EC_PATH = r'\_SB.PCI0.LPC0.EC0'
ENDPOINT = Path('/proc/acpi/call')
LOCK_DIR = Path('/run/lock')
# The writable cancellation state belongs in root's runtime directory.
CONTROL_LOCK = Path('/run/thinkbook-fanctl-control.lock')
STOP_SIGNALS = (
    signal.SIGINT,
    signal.SIGTERM,
    signal.SIGHUP,
    signal.SIGTSTP,
    signal.SIGTTIN,
    signal.SIGTTOU,
)


@contextlib.contextmanager
def locked(path, *, nonblocking=False):
    with path.open('a+') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        except BlockingIOError as error:
            raise RuntimeError('Another manual-control session is running.') from error
        handle.seek(0)
        yield handle


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
        with locked(LOCK_DIR / 'thinkbook-fanctl-acpi.lock'):
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
        if self.mode()['control_mode'] != 'manual':
            raise RuntimeError('EC did not enter manual mode.')

    def check_targets(self, first, second):
        actual = tuple(self.field(fan_id, 0x25) * 100 for fan_id in (1, 2))
        if actual != (first, second):
            raise RuntimeError(f'Fan targets changed or were not accepted: {actual}.')

    def auto(self, attempts=3):
        """Confirm automatic control and return the number of unused attempts."""
        last_error = None
        for remaining in reversed(range(attempts)):
            try:
                # The write can take effect even if the WMI reply is lost.
                try:
                    reply = self.wmi(0x12, 1, 0)
                except OSError, RuntimeError, ValueError:
                    reply = None
                if reply is not None and reply != 1:
                    raise RuntimeError('Automatic-control request failed.')
                if self.mode()['control_mode'] != 'auto':
                    raise RuntimeError('EC is still in manual mode.')
                return remaining
            except (OSError, RuntimeError, ValueError) as error:
                last_error = error
                if remaining:
                    time.sleep(0.2)
        raise RuntimeError(
            'Could not restore auto; reboot to reset fan control.'
        ) from last_error

    def auto_status(self, remaining):
        while True:
            try:
                data = self.status()
            except OSError, RuntimeError, ValueError:
                # A failed fan read can prevent status() from reaching the mode byte.
                try:
                    mode = self.mode()['control_mode']
                except OSError, RuntimeError, ValueError:
                    mode = None
                if mode == 'auto':
                    raise  # Control is confirmed; report the original telemetry error.
            else:
                if data['control_mode'] == 'auto':
                    return data
            if not remaining:
                raise RuntimeError(
                    'Could not confirm automatic control after the last auto request; '
                    'reboot to reset fan control.'
                )
            with control_signals():
                remaining = self.auto(attempts=remaining)


def format_table(data):
    lines = [
        f'{data["model"]} / {data["bios"]} - {data["stage"]}',
        f'Control: {data["control_mode"]} (EC {data["control_mode_raw"]:#04x})',
    ]
    if 'capabilities' in data:
        caps = data['capabilities']
        lines.append(
            f'WQA5: channel {caps["id"]}, {caps["min_rpm"]}-{caps["max_rpm"]} RPM (fan 1)'
        )
    lines.append('Fan    WMI RPM    Indexed RPM    Target RPM    Drive    Flags')
    for fan in data['fans']:
        lines.append(
            f'{fan["id"]:>3} {fan["rpm"]:>10} {fan["indexed_rpm"]:>14}'
            f' {fan["target_rpm"]:>13} {fan["drive_raw"]:>8} {fan["flags_raw"]:>#8x}'
        )
    return '\n'.join(lines) + '\n'


class ReportFile(io.FileIO):
    def write(self, data):
        # TextIOWrapper expects a complete write, including on nonblocking pipes.
        remaining = memoryview(data)
        while remaining:
            remaining = remaining[os.write(self.fileno(), remaining) :]
        return len(data)


@contextlib.contextmanager
def unbuffered_stream(output):
    if isinstance(output, io.TextIOWrapper):
        try:
            fd = output.fileno()
        except io.UnsupportedOperation:
            pass
        else:
            output.flush()
            # Use one encoder for all samples. Interrupted writes must not be
            # retried during interpreter shutdown after the session has ended.
            with (
                ReportFile(fd, 'w', closefd=False) as raw,
                io.TextIOWrapper(
                    raw,
                    encoding=output.encoding,
                    errors=output.errors,
                    write_through=True,
                ) as stream,
            ):
                yield stream
            return
    yield output


@contextlib.contextmanager
def report_output():
    with unbuffered_stream(sys.stdout) as stream, contextlib.redirect_stdout(stream):
        yield


def emit(data, as_json, stage):
    if sys.stdout is None:
        raise OSError('Standard output is unavailable.')
    data = dict(data, stage=stage)
    report = json.dumps(data) if as_json else format_table(data)
    sys.stdout.write(report + '\n')
    sys.stdout.flush()


def interrupted(signum, frame):
    raise KeyboardInterrupt


class ManualDeadline(Exception):
    """The requested manual duration elapsed."""


class ManualSignals:
    def __init__(self):
        self.interruptible = False
        self.stop_requested = False
        self.deadline = None

    def arm(self):
        # Check for external auto even while telemetry or stdout is blocked.
        delay = 0.25
        if self.deadline is not None:
            delay = min(delay, max(0.000001, self.deadline - time.monotonic()))
        signal.setitimer(signal.ITIMER_REAL, delay)

    def handle(self, signum, frame):
        if signum != signal.SIGALRM:
            self.stop_requested = True
        if not self.interruptible:
            return
        if self.stop_requested:
            error = KeyboardInterrupt()
        elif self.deadline is not None and time.monotonic() >= self.deadline:
            error = ManualDeadline()
        elif CONTROL_LOCK.stat().st_size == 0:
            error = RuntimeError('Manual session cancelled by auto.')
        else:
            self.arm()
            return
        # Further signals are deferred even if this one lands during unwinding.
        self.interruptible = False
        raise error

    @contextlib.contextmanager
    def active(self):
        try:
            self.interruptible = True
            if self.stop_requested:
                raise KeyboardInterrupt
            yield
        finally:
            self.interruptible = False


@contextlib.contextmanager
def control_signals(*, timer=False):
    signals = ManualSignals()
    watched = (*STOP_SIGNALS, signal.SIGALRM) if timer else STOP_SIGNALS
    with contextlib.ExitStack() as stack:
        for sig in watched:
            previous = signal.signal(sig, signals.handle)
            stack.callback(signal.signal, sig, previous)
        mask = signal.pthread_sigmask(signal.SIG_UNBLOCK, watched)
        stack.callback(signal.pthread_sigmask, signal.SIG_SETMASK, mask)
        if timer:
            stack.callback(signal.setitimer, signal.ITIMER_REAL, 0)
        yield signals
        if signals.stop_requested:
            raise KeyboardInterrupt


def check_limits(controller, first, second):
    limits = controller.capabilities()
    for rpm in (first, second):
        if not limits['min_rpm'] <= rpm <= limits['max_rpm']:
            raise ValueError(
                f'CLI range for both fans: {limits["min_rpm"]}–{limits["max_rpm"]} RPM.'
                ' These limits are reported for fan 1.'
            )


def monitor_manual(controller, first, second, deadline, as_json):
    while time.monotonic() < deadline:
        sample = controller.status()
        if sample['control_mode'] != 'manual':
            raise RuntimeError('EC left manual mode.')
        actual = tuple(fan['target_rpm'] for fan in sample['fans'])
        if actual != (first, second):
            raise RuntimeError(f'Fan targets changed during manual control: {actual}.')
        emit(sample, as_json, 'manual')
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(1, remaining))


def manual(controller, first, second, seconds, as_json):
    with contextlib.ExitStack() as session:
        with locked(CONTROL_LOCK) as state:
            session.enter_context(
                locked(LOCK_DIR / 'thinkbook-fanctl-manual.lock', nonblocking=True)
            )
            state.truncate(0)
            state.write('pending\n')
        with control_signals(timer=True) as signals:
            attempted = False
            confirmed = False
            try:
                with signals.active():
                    signals.arm()
                    check_limits(controller, first, second)
                    emit(controller.status(), as_json, 'before')
                    with locked(CONTROL_LOCK) as state:
                        if state.read() != 'pending\n':
                            raise RuntimeError('Manual session cancelled by auto.')
                        signals.deadline = time.monotonic() + seconds
                        signals.arm()
                        attempted = True
                        controller.set_targets(first, second)
                        confirmed = True
                    monitor_manual(controller, first, second, signals.deadline, as_json)
            except ManualDeadline:
                if not confirmed:
                    raise RuntimeError(
                        'Deadline expired before both targets and manual mode were confirmed.'
                    ) from None
            finally:
                # The active scope has ended. Stop signals can only set a flag here.
                if attempted:
                    remaining = controller.auto()
        data = controller.auto_status(remaining)
    emit(data, as_json, 'auto_requested')


def sleep_interval(seconds):
    # Linux sleep uses an absolute monotonic deadline, which can overflow for
    # otherwise representable intervals near TIMEOUT_MAX.
    day = 24 * 60 * 60
    while seconds > day:
        time.sleep(day)
        seconds -= day
    time.sleep(seconds)


def positive_seconds(value):
    try:
        result = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError('Expected a number of seconds.') from error
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError(
            'Seconds must be finite and greater than zero.'
        )
    if result > TIMEOUT_MAX:
        raise argparse.ArgumentTypeError(f'Seconds must be at most {TIMEOUT_MAX:.0f}.')
    return result


def rpm_value(value):
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError('Expected integer RPM.') from error
    if not 100 <= result <= 6400 or result % 100:
        raise argparse.ArgumentTypeError(
            'RPM must be a multiple of 100 between 100 and 6400; '
            'use auto to restore firmware control.'
        )
    return result


class FanArgumentParser(argparse.ArgumentParser):
    def _print_message(self, message, file=None):
        if file is sys.stdout and message:
            if file is None:
                raise OSError('Standard output is unavailable.')
            with unbuffered_stream(file) as stream:
                stream.write(message)
                stream.flush()
        else:
            super()._print_message(message, file)

    def error(self, message):
        if sys.stderr is not None:
            with (
                contextlib.suppress(OSError, ValueError),
                unbuffered_stream(sys.stderr) as stream,
                contextlib.redirect_stderr(stream),
            ):
                super().error(message)
        self.exit(2)


def parse_args(argv=None):
    parser = FanArgumentParser(prog='thinkbook-fanctl', description=__doc__)
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


def run_command(args):
    controller = FanController()
    command = args.command or 'status'
    if command == 'auto':
        # A broken output stream must not prevent the firmware recovery request.
        with locked(CONTROL_LOCK) as state:
            with control_signals():
                state.truncate(0)
                remaining = controller.auto()
            data = controller.auto_status(remaining)
        with report_output():
            emit(data, args.json, 'auto_requested')
        return
    with report_output():
        if command == 'set':
            signal.signal(signal.SIGTSTP, interrupted)
            manual(controller, args.fan1_rpm, args.fan2_rpm, args.seconds, args.json)
        elif command == 'watch':
            while True:
                emit(controller.status(), args.json, 'status')
                sleep_interval(args.interval)
        else:
            data = controller.status()
            data['capabilities'] = controller.capabilities()
            emit(data, args.json, 'status')


def main(argv=None):
    try:
        args = parse_args(argv)
        check_hardware()
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, interrupted)
        run_command(args)
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, ValueError) as error:
        if sys.stderr is not None:
            with (
                contextlib.suppress(OSError, ValueError),
                unbuffered_stream(sys.stderr) as stream,
            ):
                print(f'Error: {error}', file=stream, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
