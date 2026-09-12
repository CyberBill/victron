import threading
import unittest
from types import SimpleNamespace

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
        controller.ve = SimpleNamespace(ser=serial)

        self.assertTrue(controller.execute_command('clear_history'))
        self.assertEqual(serial.writes, [b':83010000D\n'])
        self.assertEqual(serial.flushes, 1)

    def test_restore_defaults_requires_explicit_opt_in(self):
        controller = VictronSerial.__new__(VictronSerial)
        controller.type = 'smartshunt'

        self.assertNotIn('restore_defaults', controller.get_supported_commands())
        self.assertIn('restore_defaults', controller.get_supported_commands(True))


if __name__ == '__main__':
    unittest.main()