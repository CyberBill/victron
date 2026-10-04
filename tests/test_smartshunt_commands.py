import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from serial import SerialException

from lib.victron_serial.victron_serial import (
    SMARTSHUNT_COMMANDS,
    VictronSerial,
    build_ve_hex_set_command,
)


class SmartShuntCommandTests(unittest.TestCase):
    def test_official_write_only_command_frames(self):
        expected_frames = {
            'zero_current': b':829100014\n',
            'synchronize': b':82C100011\n',
            'clear_history': b':83010000D\n',
            'restore_defaults': b':804000049\n',
        }
        for action, expected_frame in expected_frames.items():
            with self.subTest(action=action):
                register = SMARTSHUNT_COMMANDS[action]['register']
                self.assertEqual(build_ve_hex_set_command(register), expected_frame)

    def test_each_frame_has_a_valid_ve_hex_checksum(self):
        for command in SMARTSHUNT_COMMANDS.values():
            frame = build_ve_hex_set_command(command['register'])
            raw = bytes((0x08,)) + bytes.fromhex(frame[2:-1].decode('ascii'))
            self.assertEqual(sum(raw) & 0xFF, 0x55)

    def test_execute_command_writes_the_allowlisted_frame(self):
        serial = SimpleNamespace(is_open=True, writes=[], flushes=0)
        serial.write = serial.writes.append
        serial.flush = lambda: setattr(serial, 'flushes', serial.flushes + 1)
        controller = VictronSerial.__new__(VictronSerial)
        controller.name = 'Test SmartShunt'
        controller.type = 'smartshunt'
        controller.command_lock = threading.Lock()
        controller.connection_lock = threading.Lock()
        controller.ve = SimpleNamespace(ser=serial)

        self.assertTrue(controller.execute_command('clear_history'))
        self.assertEqual(serial.writes, [b':83010000D\n'])
        self.assertEqual(serial.flushes, 1)

    def test_restore_defaults_requires_explicit_opt_in(self):
        controller = VictronSerial.__new__(VictronSerial)
        controller.type = 'smartshunt'

        self.assertNotIn('restore_defaults', controller.get_supported_commands())
        self.assertIn('restore_defaults', controller.get_supported_commands(True))

    def test_serial_reader_reconnects_after_open_and_read_failures(self):
        packet_received = threading.Event()
        output = []

        class FakeSerial:
            def __init__(self, fail):
                self.fail = fail
                self.is_open = True
                self.sent_packet = False

            def read(self):
                if self.fail:
                    raise SerialException('device disconnected')
                if not self.sent_packet:
                    self.sent_packet = True
                    return b'x'
                return b''

            def close(self):
                self.is_open = False

        class FakeVedirect:
            instances = 0

            def __init__(self, port, timeout):
                type(self).instances += 1
                if type(self).instances == 1:
                    raise SerialException('port not found')
                self.ser = FakeSerial(fail=type(self).instances == 2)

            def input(self, byte):
                if byte == ord('x'):
                    return {'V': '12000'}
                return None

        def output_callback(description, value, unit):
            output.append((description, value, unit))
            if description == 'Voltage':
                packet_received.set()

        with patch('lib.victron_serial.victron_serial.Vedirect', FakeVedirect), \
             patch('lib.victron_serial.victron_serial.SERIAL_RECONNECT_DELAY', 0.01):
            controller = VictronSerial(
                {'name': 'Test SmartShunt', 'type': 'smartshunt', 'port': '/dev/fake'},
                output_callback,
            )
            try:
                self.assertTrue(packet_received.wait(1))
            finally:
                controller.shutdown()

        self.assertGreaterEqual(FakeVedirect.instances, 3)
        self.assertIn(('Voltage', 12, 'V'), output)

    def test_execute_command_fails_cleanly_while_disconnected(self):
        controller = VictronSerial.__new__(VictronSerial)
        controller.name = 'Test SmartShunt'
        controller.type = 'smartshunt'
        controller.command_lock = threading.Lock()
        controller.connection_lock = threading.Lock()
        controller.ve = None

        self.assertFalse(controller.execute_command('clear_history'))

    def test_watchdog_closes_a_stalled_serial_connection(self):
        serial = SimpleNamespace(is_open=True, closes=0)
        serial.close = lambda: setattr(serial, 'closes', serial.closes + 1)
        controller = VictronSerial.__new__(VictronSerial)
        controller.name = 'Test SmartShunt'
        controller.connection_lock = threading.Lock()
        controller.stop_event = threading.Event()
        controller.ve = SimpleNamespace(ser=serial)
        controller.last_packet_at = 0

        with patch('lib.victron_serial.victron_serial.time.monotonic', return_value=91), \
             patch('lib.victron_serial.victron_serial.SERIAL_PACKET_TIMEOUT', 90), \
             patch('lib.victron_serial.victron_serial.SERIAL_WATCHDOG_INTERVAL', 0.01):
            watchdog = threading.Thread(target=controller._watchdog_loop)
            watchdog.start()
            try:
                for _ in range(100):
                    if serial.closes:
                        break
                    threading.Event().wait(0.001)
            finally:
                controller.stop_event.set()
                watchdog.join()

        self.assertEqual(serial.closes, 1)
        self.assertIsNone(controller.ve)

    def test_parser_state_keeps_valid_cross_frame_values(self):
        controller = VictronSerial.__new__(VictronSerial)
        controller.name = 'Test SmartShunt'
        controller.map = {'H1': (), 'I': (), 'P': ()}
        parser = SimpleNamespace(dict={
            'H1': '-17200',
            'I': '-1250',
            'P': '-60',
            'corrupted field': 'ignored',
        })

        controller._discard_unknown_packet_fields(parser)

        self.assertEqual(parser.dict, {'H1': '-17200', 'I': '-1250', 'P': '-60'})


if __name__ == '__main__':
    unittest.main()