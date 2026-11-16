"""Exercise fan-control recovery without sending any real ACPI requests."""

import contextlib
import io
import json
import struct
import unittest
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

    def call(self, request):
        self.requests.append(request)
        if request == r'\_SB.GZFD.WQA5 0':
            block = struct.pack('<IIII', 1, 1, 4500, 2200)
            return '{' + ', '.join(f'0x{x:02x}' for x in block) + '}'
        if request == f'{fanctl.EC_PATH}.ERCD bb304ab000000':
            block = bytes((self.mode_raw, *([0] * 7)))
            return '{' + ', '.join(f'0x{x:02x}' for x in block) + '}'
        if '.FANG ' in request:
            index = int(request.split()[-1], 0)
            fan_id = (index >> 8) - 0x80
            offset = index & 255
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
                if self.failure == 'auto_always' or (
                    self.failure == 'auto_twice' and self.auto_attempts <= 2
                ):
                    raise OSError('simulated auto failure')
                self.targets = {1: 0, 2: 0}
                self.mode_raw = 8
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
        if self.failure == 'signal':
            fanctl.interrupted(15, None)
        if self.failure == 'target_mismatch':
            self.targets[2] = 1900
        return '{0x00}'


class FanControlTest(unittest.TestCase):
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
            stack.enter_context(
                patch.object(
                    fanctl,
                    'locked',
                    side_effect=lambda *a, **kw: contextlib.nullcontext(),
                )
            )
            stack.enter_context(
                patch.object(fanctl, 'FanController', return_value=controller)
            )
            stack.enter_context(patch.object(fanctl.signal, 'signal'))
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

    def test_watch_only_reads_and_can_be_interrupted(self):
        code, ec, output, _, _ = self.invoke(['watch', '--json'], 'watch_interrupt')
        self.assertEqual(code, 130)
        self.assertEqual(ec.controls, [])
        self.assertEqual(json.loads(output)['stage'], 'status')

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
        )
        for args in cases:
            with (
                self.subTest(args=args),
                patch.object(fanctl, 'check_hardware') as check,
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

    def test_second_manual_session_does_not_issue_control_requests(self):
        ec = SimulatedEC()
        with (
            patch.object(fanctl, 'locked', side_effect=RuntimeError('busy')),
            self.assertRaisesRegex(RuntimeError, 'busy'),
        ):
            fanctl.manual(ec, 3000, 3000, 5, True)
        self.assertEqual(ec.controls, [])


if __name__ == '__main__':
    unittest.main()
