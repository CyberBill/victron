import logging
import time
import threading
from datetime import datetime
from serial import SerialException
from vedirect import Vedirect

logger = logging.getLogger()

SERIAL_READ_TIMEOUT = 1
SERIAL_RECONNECT_DELAY = 5
SERIAL_PACKET_TIMEOUT = 30
SERIAL_WATCHDOG_INTERVAL = 5

SMARTSHUNT_COMMANDS = {
    'zero_current': {
        'name': 'Zero Current',
        'register': 0x1029,
        'icon': 'mdi:current-dc',
    },
    'synchronize': {
        'name': 'Synchronize Battery Monitor',
        'register': 0x102C,
        'icon': 'mdi:battery-sync',
    },
    'clear_history': {
        'name': 'Clear SmartShunt History',
        'register': 0x1030,
        'icon': 'mdi:chart-timeline-variant-shimmer',
    },
    'restore_defaults': {
        'name': 'Restore SmartShunt Defaults',
        'register': 0x0004,
        'icon': 'mdi:backup-restore',
        'enabled_by_default': False,
    },
}


def build_ve_hex_set_command(register):
    """Build an official VE.Hex Set frame for a write-only BMV command register."""
    command = 0x08
    data = bytes((register & 0xFF, register >> 8, 0x00))
    checksum = (0x55 - command - sum(data)) & 0xFF
    return b':' + f'{command:X}'.encode('ascii') + data.hex().upper().encode('ascii') + f'{checksum:02X}'.encode('ascii') + b'\n'

class VictronSerial:
    def __init__(self, device_config, output_callback):
        self.device_config = device_config
        self.output_callback = output_callback
        self.name = device_config['name']
        self.type = device_config['type']
        self.port = device_config['port']
        self.command_lock = threading.Lock()
        self.connection_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.last_packet = None
        self.last_packet_ready = threading.Event()
        self.last_packet_at = time.monotonic()
        self.timer_elapsed = True
        self.ve = None

        if self.type == 'phoenix':
            from lib.victron_serial.victron_phoenix import value_description_map
        elif self.type == 'smartshunt':
            from lib.victron_serial.victron_smartshunt import value_description_map
        elif self.type == 'smartsolar':
            from lib.victron_serial.victron_smartsolar import value_description_map
        else:
            raise RuntimeError(f'Got unknown type ({self.type}) from config!')
        self.map = value_description_map

        self.thread = threading.Thread(
            target=self._read_loop,
            name=f'victron-serial-{self.name}',
        )
        self.thread.start()
        self.watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name=f'victron-serial-watchdog-{self.name}',
        )
        self.watchdog_thread.start()

    def _read_loop(self):
        """Keep the reader alive while the USB serial device is absent or reconnecting."""
        while not self.stop_event.is_set():
            ve = None
            try:
                ve = Vedirect(self.port, SERIAL_READ_TIMEOUT)
                with self.connection_lock:
                    self.ve = ve
                    self.last_packet_at = time.monotonic()
                logger.info(f'{self.name}: connected to serial port {self.port}')

                while not self.stop_event.is_set():
                    data = ve.ser.read()
                    with self.connection_lock:
                        if self.ve is not ve:
                            break
                    for byte in data:
                        packet = ve.input(byte)
                        if packet is not None:
                            try:
                                self.last_packet_at = time.monotonic()
                                self.read_data_callback(packet)
                            except Exception:
                                logger.exception(f'{self.name}: failed to process serial packet')
                            finally:
                                self._discard_unknown_packet_fields(ve)
            except (SerialException, OSError) as error:
                if not self.stop_event.is_set():
                    logger.warning(f'{self.name}: serial connection lost: {error}; retrying')
            except Exception:
                if not self.stop_event.is_set():
                    logger.exception(f'{self.name}: serial reader failed; retrying')
            finally:
                with self.connection_lock:
                    if self.ve is ve:
                        self.ve = None
                if ve is not None:
                    try:
                        ve.ser.close()
                    except (SerialException, OSError):
                        pass

            self.stop_event.wait(SERIAL_RECONNECT_DELAY)

    def _watchdog_loop(self):
        """Reset a serial connection that stays open but stops yielding valid frames."""
        while not self.stop_event.wait(SERIAL_WATCHDOG_INTERVAL):
            with self.connection_lock:
                ve = self.ve
                inactive_for = time.monotonic() - self.last_packet_at
                if ve is None or inactive_for < SERIAL_PACKET_TIMEOUT:
                    continue
                self.ve = None

            logger.warning(
                f'{self.name}: no valid VE.Direct packet for {inactive_for:.0f} seconds; '
                'resetting serial connection'
            )
            try:
                ve.ser.close()
            except (SerialException, OSError):
                pass

    def _discard_unknown_packet_fields(self, ve):
        """Keep valid values accumulated by vedirect while removing corrupted field names."""
        packet_fields = getattr(ve, 'dict', None)
        if packet_fields is None:
            return
        unknown_keys = set(packet_fields) - set(self.map)
        for key in unknown_keys:
            logger.warning(f'{self.name}: discarded invalid VE.Direct field {key!r}')
            del packet_fields[key]

    def get_device_info(self):
        data = None
        while data is None:
            self.last_packet_ready.wait()
            self.last_packet_ready.clear()
            # on startup, sometimes incomplete packets show up
            if self.last_packet is not None and all(x in self.last_packet for x in ['PID', 'FW']):
                data = self.last_packet
            else:
                logging.info('Skipping incomplete packet, waiting for next packet for device info')
        pid = self.map['PID'][4](data['PID'], self.map['PID'])
        # serial may not be provided by some devices; return None if missing
        if 'SER#' in self.map and 'SER#' in data:
            ser = self.map['SER#'][4](data['SER#'], self.map['SER#'])
        else:
            ser = None
        fw = self.map['FW'][4](data['FW'], self.map['FW'])
        return pid, ser, fw

    def get_mapping_table(self):
        return self.map

    def get_supported_commands(self, include_restore_defaults=False):
        if self.type != 'smartshunt':
            return {}
        if include_restore_defaults:
            return SMARTSHUNT_COMMANDS
        return {
            action: command for action, command in SMARTSHUNT_COMMANDS.items()
            if action != 'restore_defaults'
        }

    def execute_command(self, action, include_restore_defaults=False):
        """Write one allowlisted VE.Hex command without interrupting serial reads."""
        command = self.get_supported_commands(include_restore_defaults).get(action)
        if command is None:
            logger.warning(f'{self.name}: rejected unsupported command {action!r}')
            return False

        frame = build_ve_hex_set_command(command['register'])
        try:
            with self.command_lock, self.connection_lock:
                if self.ve is None or not self.ve.ser.is_open:
                    logger.error(f'{self.name}: cannot execute {action}; serial port is disconnected')
                    return False
                self.ve.ser.write(frame)
                self.ve.ser.flush()
        except Exception:
            logger.exception(f'{self.name}: failed to execute SmartShunt command {action}')
            return False

        logger.warning(
            f'{self.name}: sent SmartShunt command {action}; '
            'the device does not provide an acknowledgement'
        )
        return True

    def finished_target(self):
        logger.debug(f'{self.name} finished')

    def connect_disconnect_loop(self, args, timer):
        logger.debug("Executing connect_disconnect_loop in victron_serial")
        while True:
            if args.direct_disconnect:
                self.last_packet_ready.wait()
                self.last_packet_ready.clear()
                self.shutdown()
                self.finished_target()
                return
            else:
                try:
                    time.sleep(timer['serial']['repeat'])
                except KeyboardInterrupt:
                    self.shutdown()
                    raise
                self.timer_elapsed = True

    def shutdown(self):
        logger.info(f'Shutting down {self.name} thread')
        self.stop_event.set()
        with self.connection_lock:
            if self.ve is not None:
                try:
                    self.ve.ser.close()
                except (SerialException, OSError):
                    pass
        self.thread.join()
        self.watchdog_thread.join()

    def read_data_callback(self, packet):
        logger.debug(f'Got data from port {self.port}: {packet}')
        self.last_packet = packet
        self.last_packet_ready.set()

        if self.timer_elapsed:
            self.timer_elapsed = False
            self.process_packet(packet)

    def process_packet(self, packet):
        logger.debug(f'Processing packet with {len(packet)} items')
        for key, value in packet.items():
            # for devices with a serial number, extract the production date as additional property
            if key == 'SER#':
                self.send_out('PROD', value)
            self.send_out(key, value)
        self.output_callback('Last Update', datetime.now().astimezone().isoformat(), 'timestamp')

    def send_out(self, key, value):
        if key not in self.map:
            logger.warning(f'{self.name}: {key} not found in mapping dictionary')
            return
        map_entry = self.map[key]
        category, description, unit, factor, helper_function = map_entry
        data = helper_function(value, map_entry)
        self.output_callback(description, data, unit)

