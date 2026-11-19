"""Exercise fan-control recovery without sending any real ACPI requests."""

import contextlib
import io
import json
import multiprocessing
import os
import signal
import struct
import subprocess
import sys
import tempfile
import unittest
from multiprocessing.managers import BaseManager
from pathlib import Path
from unittest.mock import patch

from thinkbook_fanctl import cli as fanctl


class SimulatedEC(fanctl.FanController):
    def __init__(self, failure=None):
        self.targets = {1: 2200, 2: 1900}
        self.mode_raw = 8
        self.requests = []
        self.controls = []
        self.failure = failure
        self.auto_attempts = 0
        self.final_read_failed = False

    def call(self, request):
        self.requests.append(request)
        if request == r'\_SB.GZFD.WQA5 0':
            block = struct.pack('<IIII', 1, 1, 4500, 2200)
            return '{' + ', '.join(f'0x{x:02x}' for x in block) + '}'
        if request == f'{fanctl.EC_PATH}.ERCD bb304ab000000':
            if self.failure == 'unreadable_final_mode' and self.final_read_failed:
                raise OSError('simulated mode read failure')
            block = bytes((self.mode_raw, *([0] * 7)))
            return '{' + ', '.join(f'0x{x:02x}' for x in block) + '}'
        if '.FANG ' in request:
            index = int(request.split()[-1], 0)
            fan_id = (index >> 8) - 0x80
            offset = index & 255
            if (
                offset == 4
                and self.auto_attempts
                and (
                    self.failure in ('final_rpm_failure', 'unreadable_final_mode')
                    or (
                        self.failure == 'manual_with_rpm_failure'
                        and self.auto_attempts == 1
                    )
                )
            ):
                self.final_read_failed = True
                if self.failure == 'manual_with_rpm_failure':
                    self.mode_raw = 0
                raise OSError('simulated final RPM read failure')
            values = {
                2: 30,
                4: self.targets[fan_id] // 100,
                6: 5,
                0x25: self.targets[fan_id] // 100,
            }
            if self.failure == 'read_after_set' and self.controls and offset == 4:
                raise OSError('simulated telemetry failure')
            return hex(values[offset])
        if '.WMAE ' in request:
            _, instance, operation, payload = request.split()
            assert instance == '0'
            fan_id, feature, device, rpm = struct.unpack(
                '<HBBI', bytes.fromhex(payload[1:])
            )
            assert (feature, device) == (3, 4)
            if int(operation, 0) == 0x11:
                assert rpm == 0
                return hex(self.targets[fan_id])
            assert int(operation, 0) == 0x12 and fan_id == 1
            self.controls.append(('fan1', rpm))
            if rpm == 0:
                self.auto_attempts += 1
                if self.failure == 'auto_stuck' or (
                    self.failure in ('auto_stuck_twice', 'manual_after_last_attempt')
                    and self.auto_attempts <= 2
                ):
                    self.mode_raw = 0
                    return '0x1'
                if self.failure == 'auto_always' or (
                    self.failure == 'auto_twice' and self.auto_attempts <= 2
                ):
                    raise OSError('simulated auto failure')
                self.targets = {1: 0, 2: 0}
                self.mode_raw = 8
                if self.failure == 'lost_auto_reply':
                    raise OSError('simulated lost auto reply')
            else:
                self.targets[1] = rpm
                self.mode_raw = 0
                if self.failure == 'first_response':
                    raise OSError('simulated lost first setter response')
            return '0x1'
        assert request.startswith(f'{fanctl.EC_PATH}.ERCD b')
        payload = bytes.fromhex(request.split()[-1][1:])
        assert (
            len(payload) == 6
            and payload[:2] == b'\x46\x86'
            and payload[3:] == b'\0\0\0'
        )
        self.targets[2] = payload[2] * 100
        self.mode_raw = 0
        self.controls.append(('fan2', self.targets[2]))
        if self.failure == 'second_response':
            raise OSError('simulated lost second setter response')
        if self.failure == 'setter_timeout':
            raise fanctl.ManualDeadline
        if self.failure == 'signal':
            fanctl.interrupted(15, None)
        if self.failure == 'target_mismatch':
            self.targets[2] = 1900
        return '{0x00}'

    def control_history(self):
        return list(self.controls)

    def status(self):
        if self.failure == 'drift_during_sample' and self.mode_raw == 0:
            self.targets[2] = 2200
        if self.auto_attempts and (
            self.failure in ('manual_at_final_read', 'manual_after_last_attempt')
            or (
                self.failure == 'manual_at_first_final_read' and self.auto_attempts == 1
            )
        ):
            self.mode_raw = 0
        return super().status()


class ECManager(BaseManager):
    pass


ECManager.register('SimulatedEC', SimulatedEC)


def run_simulated_cli(
    directory, ec, args, pause_at=None, ready=None, resume=None, pipe_fd=None
):
    write_sample = fanctl.emit

    def checkpoint(stage):
        if stage == pause_at:
            ready.set()
            if not resume.wait(10):
                raise AssertionError('Timed out waiting for the test to resume')

    def emit(data, as_json, stage):
        if pipe_fd is not None and stage == pause_at:
            ready.set()
            write_sample(data, as_json, stage)
        else:
            checkpoint(stage)

    class RemoteController(fanctl.FanController):
        def call(self, request):
            return ec.call(request)

        def wmi(self, operation, fan_id, rpm=0):
            result = super().wmi(operation, fan_id, rpm)
            if operation == 0x12 and rpm:
                checkpoint('first_setter')
            return result

        def auto(self, attempts=3):
            checkpoint('cleanup')
            return super().auto(attempts=attempts)

    with (
        contextlib.ExitStack() as output,
        patch.multiple(
            fanctl, LOCK_DIR=directory, CONTROL_LOCK=directory / 'control.lock'
        ),
        patch.object(fanctl, 'check_hardware'),
        patch.object(fanctl, 'FanController', RemoteController),
        patch.object(fanctl, 'emit', side_effect=emit),
        contextlib.redirect_stderr(io.StringIO()),
    ):
        if pipe_fd is not None:
            stream = output.enter_context(os.fdopen(pipe_fd, 'w', closefd=False))
            output.enter_context(contextlib.redirect_stdout(stream))
        raise SystemExit(fanctl.main(args))


class FanControlTest(unittest.TestCase):
    def subprocess_cli(
        self, args, *, encoding='utf-8', stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ):
        script = """
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from test_cli import SimulatedEC
from thinkbook_fanctl import cli
with TemporaryDirectory() as tmp, patch.multiple(
    cli, check_hardware=lambda: None, FanController=SimulatedEC,
    LOCK_DIR=Path(tmp), CONTROL_LOCK=Path(tmp) / 'control.lock',
):
    sys.exit(cli.main(sys.argv[1:]))
"""
        return subprocess.run(
            [sys.executable, '-c', script, *args],
            check=False,
            env=dict(
                os.environ,
                PYTHONPATH=os.pathsep.join(sys.path),
                PYTHONIOENCODING=encoding,
            ),
            stdout=stdout,
            stderr=stderr,
            timeout=5,
        )

    def test_closed_output_pipe_keeps_cli_error_code(self):
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        with os.fdopen(write_fd, 'wb') as sink:
            for args in (
                ['status', '--json'],
                ['--help'],
                ['auto', '--help'],
                ['--version'],
            ):
                with self.subTest(args=args):
                    result = self.subprocess_cli(args, stdout=sink)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(b'Broken pipe', result.stderr)
                    self.assertNotIn(b'Exception ignored', result.stderr)

    def test_broken_diagnostic_pipe_preserves_exit_codes(self):
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        with os.fdopen(write_fd, 'wb') as sink:
            for rpm, expected in (('2100', 1), ('6500', 2)):
                with self.subTest(rpm=rpm):
                    result = self.subprocess_cli(
                        ['set', rpm, '3000', '--json'], stderr=sink
                    )
                    self.assertEqual(result.returncode, expected)
                    self.assertEqual(result.stdout, b'')

    def test_missing_stderr_keeps_errors_out_of_json(self):
        with (
            patch.object(
                fanctl, 'check_hardware', side_effect=RuntimeError('Unsupported BIOS')
            ),
            contextlib.redirect_stderr(None),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(fanctl.main(['status', '--json']), 1)
            with self.assertRaises(SystemExit) as invalid:
                fanctl.main(['set', '6500', '3000', '--json'])
            self.assertEqual(invalid.exception.code, 2)
            self.assertEqual(output.getvalue(), '')

    def test_rpm_error_includes_the_parser_range(self):
        result = self.subprocess_cli(['set', '6500', '3000'])
        self.assertEqual(result.returncode, 2)
        self.assertIn(b'6400', result.stderr)

    def test_status_table_on_ascii_stdout(self):
        result = self.subprocess_cli(['status'], encoding='ascii')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('2200-4500 RPM', result.stdout.decode('ascii'))

    def test_auto_restores_firmware_with_unavailable_stdout(self):
        ec = SimulatedEC()
        ec.mode_raw = 0
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.multiple(
                fanctl,
                check_hardware=lambda: None,
                CONTROL_LOCK=Path(directory) / 'control.lock',
                FanController=lambda: ec,
            ),
            patch.object(fanctl.signal, 'signal'),
            contextlib.redirect_stdout(None),
            contextlib.redirect_stderr(io.StringIO()) as error,
        ):
            self.assertEqual(fanctl.main(['auto']), 1)
        self.assertEqual(ec.mode()['control_mode'], 'auto')
        self.assertIn('Standard output is unavailable', error.getvalue())

    def test_set_emits_one_utf16_stream(self):
        result = self.subprocess_cli(
            ['set', '3000', '3000', '--seconds', '0.1', '--json'], encoding='utf-16'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        samples = [
            json.loads(line) for line in result.stdout.decode('utf-16').splitlines()
        ]
        self.assertEqual(
            [sample['stage'] for sample in samples],
            ['before', 'manual', 'auto_requested'],
        )

    def invoke(self, arguments, failure=None):
        controller = SimulatedEC(failure)
        stdout, stderr = io.StringIO(), io.StringIO()
        now = [0.0]

        def sleep(seconds):
            if failure == 'watch_interrupt':
                raise KeyboardInterrupt
            now[0] += seconds

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(fanctl, 'check_hardware'))
            directory = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            stack.enter_context(
                patch.multiple(
                    fanctl, LOCK_DIR=directory, CONTROL_LOCK=directory / 'control.lock'
                )
            )
            stack.enter_context(
                patch.object(fanctl, 'FanController', return_value=controller)
            )
            stack.enter_context(patch.object(fanctl.signal, 'signal'))
            stack.enter_context(patch.object(fanctl.signal, 'setitimer'))
            stack.enter_context(patch.object(fanctl.time, 'sleep', side_effect=sleep))
            stack.enter_context(
                patch.object(fanctl.time, 'monotonic', side_effect=lambda: now[0])
            )
            stack.enter_context(contextlib.redirect_stdout(stdout))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            code = fanctl.main(arguments)
        return code, controller, stdout.getvalue(), stderr.getvalue(), now[0]

    def test_default_and_json_status_only_read(self):
        for args in ([], ['status', '--json']):
            with self.subTest(args=args):
                code, ec, output, _, _ = self.invoke(args)
                self.assertEqual(code, 0)
                self.assertEqual(ec.controls, [])
                if args:
                    data = json.loads(output)
                    self.assertEqual([f['rpm'] for f in data['fans']], [2200, 1900])
                    self.assertEqual(data['capabilities']['count'], 1)
                    self.assertEqual(data['control_mode'], 'auto')
                    self.assertEqual(data['control_mode_raw'], 8)
                else:
                    self.assertIn('Control: auto (EC 0x08)', output)
                self.assertIn(f'{fanctl.EC_PATH}.ERCD bb304ab000000', ec.requests)

    def test_different_targets_are_encoded_and_restored_after_duration(self):
        code, ec, output, _, elapsed = self.invoke(['set', '3000', '2300', '--json'])
        self.assertEqual(code, 0)
        self.assertEqual(ec.controls, [('fan1', 3000), ('fan2', 2300), ('fan1', 0)])
        self.assertEqual(elapsed, 5)
        samples = [json.loads(line) for line in output.splitlines()]
        manual = next(data for data in samples if data['stage'] == 'manual')
        self.assertEqual([f['target_rpm'] for f in manual['fans']], [3000, 2300])
        self.assertEqual(manual['control_mode'], 'manual')
        self.assertEqual(samples[-1]['stage'], 'auto_requested')
        self.assertEqual(samples[-1]['control_mode'], 'auto')
        self.assertEqual(ec.targets, {1: 0, 2: 0})

    def test_mode_read_rejects_malformed_response(self):
        ec = SimulatedEC()
        with (
            patch.object(ec, 'call', return_value='{0x08}'),
            self.assertRaisesRegex(RuntimeError, 'mode-byte response'),
        ):
            ec.mode()

    def test_deadline_skips_final_manual_sample(self):
        _, _, output, _, _ = self.invoke(
            ['set', '3000', '3000', '--seconds', '1', '--json']
        )
        stages = [json.loads(line)['stage'] for line in output.splitlines()]
        self.assertEqual(stages, ['before', 'manual', 'auto_requested'])

    def test_targets_alone_do_not_confirm_manual_mode(self):
        with (
            patch.object(
                SimulatedEC,
                'mode',
                return_value={'control_mode': 'auto', 'control_mode_raw': 8},
            ),
            patch.object(
                fanctl, 'monitor_manual', side_effect=fanctl.ManualDeadline
            ) as monitor,
        ):
            code, ec, output, error, _ = self.invoke(['set', '3000', '2300', '--json'])
        self.assertEqual(code, 1)
        self.assertIn('manual mode', error)
        self.assertEqual(json.loads(output)['stage'], 'before')
        self.assertEqual(ec.controls, [('fan1', 3000), ('fan2', 2300), ('fan1', 0)])
        monitor.assert_not_called()

    def test_mode_uses_bit_3_without_discarding_other_bits(self):
        ec = SimulatedEC()
        for raw, expected in ((0x0C, 'auto'), (0x04, 'manual')):
            with self.subTest(raw=raw):
                ec.mode_raw = raw
                self.assertEqual(
                    ec.mode(), {'control_mode': expected, 'control_mode_raw': raw}
                )

    def test_failure_after_either_setter_and_telemetry_failure_restore_auto(self):
        for failure in (
            'first_response',
            'second_response',
            'setter_timeout',
            'read_after_set',
            'target_mismatch',
        ):
            with self.subTest(failure=failure):
                code, ec, _, error, _ = self.invoke(['set', '3000', '3000'], failure)
                self.assertEqual(code, 1)
                self.assertIn('Error:', error)
                self.assertEqual(ec.controls[-1], ('fan1', 0))
                self.assertEqual(ec.targets, {1: 0, 2: 0})

    def test_signal_during_manual_control_restores_auto(self):
        code, ec, _, _, _ = self.invoke(['set', '3000', '3000'], 'signal')
        self.assertEqual(code, 130)
        self.assertEqual(ec.targets, {1: 0, 2: 0})

    def test_cleanup_retries_and_reports_exhaustion(self):
        for failure, code in (('auto_twice', 0), ('auto_always', 1)):
            with self.subTest(failure=failure):
                result, ec, _, error, _ = self.invoke(['set', '3000', '3000'], failure)
                self.assertEqual(result, code)
                self.assertEqual(ec.auto_attempts, 3)
                if code:
                    self.assertIn('reboot', error)

    def test_auto_is_available_without_capability_query(self):
        code, ec, _, _, _ = self.invoke(['auto', '--json'])
        self.assertEqual(code, 0)
        self.assertEqual(ec.controls, [('fan1', 0)])
        self.assertNotIn(r'\_SB.GZFD.WQA5 0', ec.requests)

    def test_lost_auto_reply_is_confirmed_by_mode_readback(self):
        for args in (['auto', '--json'], ['set', '3000', '3000', '--json']):
            with self.subTest(args=args):
                code, ec, output, error, _ = self.invoke(args, 'lost_auto_reply')
                self.assertEqual(code, 0, error)
                self.assertEqual(ec.auto_attempts, 1)
                self.assertEqual(
                    json.loads(output.splitlines()[-1])['control_mode'], 'auto'
                )

    def test_manual_target_drift_in_the_last_sample_is_reported(self):
        code, ec, output, error, _ = self.invoke(
            ['set', '3000', '3000', '--seconds', '0.5', '--json'], 'drift_during_sample'
        )
        self.assertEqual(code, 1)
        self.assertIn('2200', error)
        self.assertEqual(json.loads(output)['stage'], 'before')
        self.assertEqual(ec.mode()['control_mode'], 'auto')

    def test_final_mode_read_uses_remaining_auto_attempts(self):
        for args in (['auto', '--json'], ['set', '3000', '3000', '--json']):
            for failure, code, attempts in (
                ('manual_at_first_final_read', 0, 2),
                ('manual_at_final_read', 1, 3),
                ('manual_after_last_attempt', 1, 3),
            ):
                with self.subTest(args=args, failure=failure):
                    result, ec, output, error, _ = self.invoke(args, failure)
                    self.assertEqual(result, code, error)
                    self.assertEqual(ec.auto_attempts, attempts)
                    if code:
                        self.assertIn('reboot', error)
                        self.assertNotIn('auto_requested', output)
                    else:
                        self.assertEqual(
                            json.loads(output.splitlines()[-1])['control_mode'], 'auto'
                        )

    def test_auto_retries_until_ec_confirms_mode(self):
        for args in (['auto'], ['set', '3000', '3000']):
            for failure, expected in (('auto_stuck', 1), ('auto_stuck_twice', 0)):
                with self.subTest(args=args, failure=failure):
                    code, ec, _, error, _ = self.invoke(args, failure)
                    self.assertEqual(code, expected)
                    self.assertEqual(ec.auto_attempts, 3)
                    if code:
                        self.assertIn('reboot', error)

    def test_final_rpm_failure_checks_mode_independently(self):
        for args in (['auto', '--json'], ['set', '3000', '3000', '--json']):
            for failure, code, attempts, message in (
                ('manual_with_rpm_failure', 0, 2, ''),
                ('final_rpm_failure', 1, 1, 'final RPM read failure'),
                ('unreadable_final_mode', 1, 3, 'reboot'),
            ):
                with self.subTest(args=args, failure=failure):
                    result, ec, output, error, _ = self.invoke(args, failure)
                    self.assertEqual(result, code, error)
                    self.assertEqual(ec.auto_attempts, attempts)
                    if code:
                        self.assertIn(message, error)
                        self.assertNotIn('auto_requested', output)
                    else:
                        self.assertEqual(error, '')
                        self.assertEqual(
                            json.loads(output.splitlines()[-1])['control_mode'], 'auto'
                        )

    def test_watch_only_reads_and_can_be_interrupted(self):
        code, ec, output, _, _ = self.invoke(['watch', '--json'], 'watch_interrupt')
        self.assertEqual(code, 130)
        self.assertEqual(ec.controls, [])
        self.assertEqual(json.loads(output)['stage'], 'status')

    def test_watch_splits_large_intervals_without_sampling_early(self):
        with patch.object(fanctl.time, 'sleep') as sleep:
            fanctl.sleep_interval(86401.5)
            self.assertEqual(
                [call.args[0] for call in sleep.call_args_list], [86400, 1.5]
            )
        with patch.object(fanctl.time, 'sleep', side_effect=KeyboardInterrupt) as sleep:
            with self.assertRaises(KeyboardInterrupt):
                fanctl.sleep_interval(fanctl.TIMEOUT_MAX)
            sleep.assert_called_once_with(86400)

    def test_out_of_cli_range_does_not_change_either_fan(self):
        for targets in (('2100', '3000'), ('3000', '4600')):
            with self.subTest(targets=targets):
                code, ec, _, _, _ = self.invoke(['set', *targets])
                self.assertEqual(code, 1)
                self.assertEqual(ec.controls, [])

    def test_invalid_arguments_fail_before_hardware_access(self):
        cases = (
            ['set', '3000'],
            ['set', '0', '3000'],
            ['set', '3050', '3000'],
            ['set', '3000', '3000', '--seconds', 'nan'],
            ['set', '3000', '3000', '--seconds', '0'],
            ['watch', '--interval', 'inf'],
            ['watch', '--interval', '1e100'],
            ['set', '3000', '3000', '--seconds', '1e100'],
        )
        for args in cases:
            with (
                self.subTest(args=args),
                patch.object(
                    fanctl,
                    'check_hardware',
                    side_effect=AssertionError('Unexpected hardware access'),
                ) as check,
            ):
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    fanctl.main(args)
                check.assert_not_called()

    def test_other_hardware_is_rejected_before_creating_controller(self):
        identity = dict(fanctl.IDENTITY, bios_version='OTHER')
        with (
            patch.object(
                fanctl.Path,
                'read_text',
                autospec=True,
                side_effect=lambda path: identity[path.name],
            ),
            patch.object(fanctl, 'FanController') as controller,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(fanctl.main(['auto']), 1)
            controller.assert_not_called()


class ConcurrentFanControlTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ctx = multiprocessing.get_context('fork')
        manager = self.enterContext(ECManager(ctx=self.ctx))
        self.ec = manager.SimulatedEC()
        self.ready = self.ctx.Event()
        self.resume = self.ctx.Event()

    def start(self, args, pause_at=None, pipe_fd=None):
        child = self.ctx.Process(
            target=run_simulated_cli,
            args=(
                self.directory,
                self.ec,
                args,
                pause_at,
                self.ready,
                self.resume,
                pipe_fd,
            ),
        )
        child.start()
        self.addCleanup(self.stop, child)
        if pause_at:
            self.assertTrue(self.ready.wait(5), f'CLI did not reach {pause_at}')
        return child

    @staticmethod
    def stop(child):
        if child.is_alive():
            child.kill()
        child.join()
        child.close()

    def assert_exit(self, child, expected):
        child.join(5)
        self.assertEqual(child.exitcode, expected)

    def test_auto_cancels_pending_set_and_second_set_is_rejected(self):
        args = ['set', '3000', '3000']
        first = self.start(args, 'before')
        self.assert_exit(self.start(args), 1)
        self.assertEqual(self.ec.control_history(), [])
        self.assert_exit(self.start(['auto']), 0)
        self.resume.set()
        self.assert_exit(first, 1)
        self.assertTrue(
            all(control == ('fan1', 0) for control in self.ec.control_history())
        )
        self.assertEqual(self.ec.mode()['control_mode'], 'auto')

    def test_ctrl_z_ends_manual_session(self):
        child = self.start(['set', '3000', '3000', '--seconds', '30'], 'manual')
        os.kill(child.pid, signal.SIGTSTP)
        self.assert_exit(child, 130)
        self.assertEqual(self.ec.mode()['control_mode'], 'auto')

    def test_terminal_job_control_signals_restore_auto(self):
        for sig in (signal.SIGTTOU, signal.SIGTTIN):
            with self.subTest(signal=sig):
                self.ready.clear()
                child = self.start(['set', '3000', '3000', '--seconds', '30'], 'manual')
                os.kill(child.pid, sig)
                self.assert_exit(child, 130)
                self.assertEqual(self.ec.mode()['control_mode'], 'auto')

    def test_signals_at_cleanup_entry_are_deferred_until_auto_is_confirmed(self):
        child = self.start(['set', '3000', '3000', '--seconds', '0.2'], 'cleanup')
        os.kill(child.pid, signal.SIGTERM)
        os.kill(child.pid, signal.SIGINT)
        self.resume.set()
        self.assert_exit(child, 130)
        self.assertEqual(self.ec.mode()['control_mode'], 'auto')
        self.assertEqual(self.ec.control_history()[-1], ('fan1', 0))

    def test_auto_during_first_setter_waits_for_both_writes(self):
        first = self.start(['set', '3000', '2300', '--seconds', '30'], 'first_setter')
        auto = self.start(['auto'])
        auto.join(0.2)
        self.assertIsNone(auto.exitcode)
        self.assertEqual(self.ec.control_history(), [('fan1', 3000)])
        self.resume.set()
        self.assert_exit(auto, 0)
        self.assert_exit(first, 1)
        self.assertEqual(
            self.ec.control_history(),
            [('fan1', 3000), ('fan2', 2300), ('fan1', 0), ('fan1', 0)],
        )
        self.assertEqual(self.ec.mode()['control_mode'], 'auto')

    def full_pipe(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)
        os.set_blocking(write_fd, False)
        with self.assertRaises(BlockingIOError):
            while True:
                os.write(write_fd, b'x' * 4096)
        os.set_blocking(write_fd, True)
        return write_fd

    def test_auto_releases_session_blocked_on_initial_output(self):
        child = self.start(['set', '3000', '3000'], 'before', self.full_pipe())
        self.assert_exit(self.start(['auto']), 0)
        self.assert_exit(child, 1)
        self.assertEqual(self.ec.control_history(), [('fan1', 0)])
        self.assert_exit(self.start(['set', '3000', '3000', '--seconds', '0.2']), 0)

    def test_deadline_restores_auto_with_full_output_pipe(self):
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
        try:
            child = self.start(
                ['set', '3000', '3000', '--seconds', '0.5'], 'manual', self.full_pipe()
            )
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        self.assert_exit(child, 0)
        self.assertEqual(self.ec.mode()['control_mode'], 'auto')


if __name__ == '__main__':
    unittest.main()
