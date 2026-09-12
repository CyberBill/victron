import logging
import time
import threading
from datetime import datetime
from vedirect import Vedirect

logger = logging.getLogger()

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

# hack! patch vedirect's read_data_callback method to support exiting the main loop

#vedirect.read_data_callback = lambda self, callbackFunction:
def read_data_callback(self, callbackFunction):
    self.keep_running = True
    while self.keep_running:
        data = self.ser.read()
        for byte in data:
            packet = self.input(byte)
            if (packet != None):
                callbackFunction(packet)
Vedirect.read_data_callback = read_data_callback

class VictronSerial:
    def __init__(self, device_config, output_callback):
        self.device_config = device_config
        self.output_callback = output_callback
        self.name = device_config['name']
        self.type = device_config['type']
        self.port = device_config['port']
        self.command_lock = threading.Lock()

        if self.type == 'phoenix':
            from lib.victron_serial.victron_phoenix import value_description_map
        elif self.type == 'smartshunt':
            from lib.victron_serial.victron_smartshunt import value_description_map
        elif self.type == 'smartsolar':
            from lib.victron_serial.victron_smartsolar import value_description_map
        else:
            raise RuntimeError(f'Got unknown type ({self.type}) from config!')
        self.map = value_description_map

        self.ve = Vedirect(self.port, 60)
        callback_wrapper = lambda packet: self.read_data_callback(packet)
        self.thread = threading.Thread(target=self.ve.read_data_callback, args=(callback_wrapper,))
        self.thread.start()
        # TODO: stop this thread when the application quits

        self.last_packet = None
        self.last_packet_ready = threading.Event()
        self.timer_elapsed = True

    def get_device_info(self):
        data = None
        while data is None:
            self.last_packet_ready.wait()
            self.last_packet_ready.clear()
            # on startup, sometimes incomplete packets show up
            if all([x in self.last_packet for x in ['PID', 'FW']]):
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
            with self.command_lock:
                if not self.ve.ser.is_open:
                    logger.error(f'{self.name}: cannot execute {action}; serial port is closed')
                    return False
                self.ve.ser.write(frame)
                self.ve.ser.flush()
        except Exception:
            logger.exception(f'{self.name}: failed to execute SmartShunt command {action}')
            return False

        logger.warning(f'{self.name}: sent SmartShunt command {action}')
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
        if hasattr(self.ve, 'keep_running'):
            logging.info(f'Shutting down {self.name} thread')
            self.ve.keep_running = False
            self.thread.join()

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

