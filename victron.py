#!/usr/bin/env python3
import argparse
import copy
import faulthandler
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import yaml
from collections import namedtuple
from datetime import datetime, timedelta
from enum import IntEnum
from time import sleep

version = 0.1
command_handlers = {}
command_handlers_lock = threading.Lock()


class LiveDashboard:
    """Continuously render selected telemetry without replacing the configured output."""

    tracked_categories = ('Voltage', 'Current', 'Power', 'State Of Charge')

    def __init__(self, device_name, output, stream=None, refresh_interval=1, start=True):
        self.device_name = device_name
        self.output = output
        self.stream = stream if stream is not None else sys.stdout
        self.refresh_interval = refresh_interval
        self.values = {}
        self.last_device_update = None
        self.last_output_handoff = None
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        if start:
            self.thread = threading.Thread(
                target=self._render_loop,
                name=f'victron-dashboard-{device_name}',
                daemon=True,
            )
            self.thread.start()

    def __call__(self, device_name, category, value, hass_config=False, vunit=None):
        with self.lock:
            if category in self.tracked_categories:
                self.values[category] = (value, vunit)
            if category == 'Last Update':
                self.last_device_update = datetime.now().astimezone()

        try:
            return self.output(device_name, category, value, hass_config, vunit)
        finally:
            with self.lock:
                self.last_output_handoff = datetime.now().astimezone()

    @staticmethod
    def _format_age(timestamp, now):
        if timestamp is None:
            return 'waiting for data'
        return f'{max(0, int((now - timestamp).total_seconds()))}s ago'

    def render(self):
        now = datetime.now().astimezone()
        with self.lock:
            values = self.values.copy()
            device_update_age = self._format_age(self.last_device_update, now)
            output_handoff_age = self._format_age(self.last_output_handoff, now)

        lines = [
            f'Victron live dashboard — {self.device_name}',
            'Press Ctrl+C to stop.',
            '',
        ]
        for category in self.tracked_categories:
            value, unit = values.get(category, ('—', ''))
            suffix = f' {unit}' if unit else ''
            lines.append(f'{category:<18} {value}{suffix}')
        lines.extend((
            '',
            f'Last device update:  {device_update_age}',
            f'Last output handoff: {output_handoff_age}',
            'MQTT delivery status is logged separately.',
        ))
        self.stream.write('\033[2J\033[H' + '\n'.join(lines) + '\n')
        self.stream.flush()

    def _render_loop(self):
        while not self.stop_event.wait(self.refresh_interval):
            self.render()

    def shutdown(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()


def victron_thread(thread_count, config, vdevice_config, thread_q):
    from lib.victron import Victron
    v = Victron(config, vdevice_config, output, args, thread_count, thread_q)
    if v.commands_enabled:
        with command_handlers_lock:
            command_handlers[vdevice_config['name']] = v.handle_command
    logger.debug("victron library loaded, start connect_disconnect_loop()")
    v.connect_disconnect_loop()


def output_print(device_name, category, value, hass_config=False, vunit=None):
    if type(value) == dict:
        map = {}
        map[category] = value
        print(json.dumps(map))
    else:
        print(f'{category}:{value}')


def output_json(device_name, category, value, hass_config=False, vunit=None):
    map = {}
    if type(value) == dict:
        map[category] = value
    else:
        map[category] = {
            'value': value,
            'unit': vunit
        }
    print(json.dumps(map))


def output_syslog(device_name, category, value, hass_config=False, vunit=None):
    if type(value) == dict:
        map = {}
        map[category] = value
        return_data = json.dumps(map)
    else:
        return_data = f"{device_name}|{category}:{value}"

    subprocess.run(
        [
            "/usr/bin/logger",
            f"--id={os.getpid()}",
            "-t",
            "victron",
            return_data,
        ]
    )


def mqtt_onconnect(client, userdata, flags, rc):
    client.publish(mqtt_lwt, payload=1, qos=0, retain=True)
    if config['mqtt'].get('hass', False) and config['mqtt'].get('hass_commands', False):
        command_topic = f'{config["mqtt"]["base_topic"]}/+/command/+'
        client.subscribe(command_topic)
        logger.info(f'MQTT command subscription established: {command_topic}')


def mqtt_ondisconnect(client, userdata, rc):
    if rc == 0:
        logger.info('MQTT broker connection closed')
    else:
        logger.warning(f'MQTT broker connection lost (result code {rc}); reconnecting')


def mqtt_onmessage(client, userdata, message):
    if message.retain or message.payload != b'PRESS':
        logger.warning(f'Ignoring invalid MQTT command on {message.topic}')
        return

    prefix = f'{config["mqtt"]["base_topic"]}/'
    if not message.topic.startswith(prefix):
        return
    topic_parts = message.topic[len(prefix):].split('/')
    if len(topic_parts) != 3 or topic_parts[1] != 'command':
        logger.warning(f'Ignoring invalid MQTT command topic: {message.topic}')
        return

    device_name, _, action = topic_parts
    with command_handlers_lock:
        handler = command_handlers.get(device_name)
    if handler is None:
        logger.warning(f'{device_name}: rejected MQTT command {action!r}; device controls are unavailable')
        return
    handler(action)


def output_mqtt(device_name, subtopic, value, hass_config=False, vunit=None):
    global client
    global config
    retain = False

    if hass_config:
        pub = f'{subtopic}'
        data = value
        retain = True
    else:
        if value == "":
            pub = f'{config["mqtt"]["base_topic"]}/{device_name}'
            data = subtopic
        else:
            pub = f'{config["mqtt"]["base_topic"]}/{device_name}/{subtopic}'
            if type(value) is dict:
                data = json.dumps(value)
            else:
                data = value

    logger.debug(f'MQTT publish -> topic={pub} payload={data!r} type={type(data).__name__} retain={retain}')
    result = client.publish(pub, data, retain=retain)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        logger.warning(f'MQTT publish failed for {pub}: result code {result.rc}')


def get_helper_string_device(devices):
    return_string = ""
    for count, device in enumerate(devices):
        return_string += f"{count}: {device['name']} | "
    return return_string


def check_if_required_device_argument():
    for x in ['-h', '--help', '-v', '--version', '-l', '--list-config-devices']:
        if x in sys.argv:
            return False
    return True

if __name__ == "__main__":
    if os.path.exists('config.yml'):
        with open('config.yml', 'r') as ymlfile:
            config = yaml.full_load(ymlfile)
    else:
        config = None

    parser = argparse.ArgumentParser(description="Victron Reader (Bluetooth, BLE and Serial) \n\n"
                                                 "Current supported devices:\n"
                                                 "  Full: \n" 
                                                 "    - Smart Shunt (Bluetooth BLE)\n"
                                                 "    - Phoenix Inverter (Serial)\n"
                                                 "    - Smart Shunt (Serial)\n"
                                                 "    - Smart Solar (Serial)\n"
                                                 "    - Blue Solar (Serial)\n"
                                                 "  Partial: \n"
                                                 "    - Smart Shunt (Bluetooth)\n"
                                                 "    - Smart Solar (Bluetooth)\n"
                                                 "    - Orion Smart (Bluetooth)\n"
                                                 "Default behavior:\n"
                                                 "  1. It will connect to given device\n"
                                                 "  2. Collect and log data summary as defined at the config file\n"
                                                 "  3. Disconnect and start over with timers set in config file",
                                     formatter_class=argparse.RawTextHelpFormatter)
    group01 = parser.add_argument_group()
    group01.add_argument("--debug", action="store_true", help="Set log level to debug")
    group01.add_argument(
        "--verbose",
        action="store_true",
        help="Log parsed device packets to the application log without terminal debug output",
    )
    group01.add_argument("--quiet", action="store_true", help="Set log level to error")
    group01.add_argument(
        "--dashboard",
        action="store_true",
        help="Show live voltage, current, power, and state of charge in the terminal",
    )

    group02 = parser.add_argument_group()
    group02.add_argument(
        "-c",
        "--collection",
        action="store_true",
        help="Output only collections specified in config instead of single values",
        required=False,
    )
    group02.add_argument(
        "-C",
        "--config-file",
        type=str,
        help="Specify different config file [Default: config.yml]",
        required=False,
    )
    group02.add_argument(
        "-D",
        "--direct-disconnect",
        action="store_true",
        help="Disconnect direct after getting values",
        required=False,
    )
    group02.add_argument(
        "-v",
        "--version",
        action="store_true",
        help="Show version and exit",
        required=False,
    )
    group02.add_argument(
        "-l",
        "--list-config-devices",
        action="store_true",
        help="Show devices from loaded config and exit",
        required=False,
    )

    group03 = parser.add_argument_group()
    group03.add_argument(
        "-d",
        "--device",
        metavar="NUM / NAME",
        type=str,
        help=get_helper_string_device(config['devices']) if config is not None else "",
        required=check_if_required_device_argument(),
    )
    args = parser.parse_args()

    if args.config_file:
        with open(args.config_file, 'r') as ymlfile:
            config = yaml.full_load(ymlfile)

    if args.version:
        print(version)
        sys.exit(0)

    if args.list_config_devices:
        print(get_helper_string_device(config['devices']))
        sys.exit(0)

    if config is None:
        print("config.yml missing. Please create or specify another config file with -C")
        sys.exit(1)

    try:
        dev_id = int(args.device)
    except ValueError:
        dev_id = None
        for count, device_config in enumerate(config['devices']):
            if device_config['name'] == args.device:
                dev_id = count
                break
        if dev_id is None:
            print(f'{args.device} not found in config')
            sys.exit(1)
    devices_config = config['devices'][dev_id]

    logger_format = '[%(levelname)-7s] (%(asctime)s) %(filename)s::%(lineno)d %(message)s'
    logging.basicConfig(level=logging.INFO,
                        format=logger_format,
                        datefmt='%Y-%m-%d %H:%M:%S',
                        filename=f'logs/victron-{devices_config["name"]}.log')
    logger = logging.getLogger()

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter(logger_format)
    handler.setFormatter(formatter)

    if args.debug or args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    if args.debug:
        handler.setLevel(logging.DEBUG)
    elif args.quiet:
        logging.getLogger().setLevel(logging.ERROR)
        handler.setLevel(logging.ERROR)

    if config['logger'] == 'mqtt':
        if not args.dashboard:
            logger.addHandler(handler)

        import paho.mqtt.client as mqtt
        client = mqtt.Client()
        if "username" in config['mqtt'] and "password" in config['mqtt']:
            client.username_pw_set(username=config['mqtt']['username'],password=config['mqtt']['password'])

        mqtt_lwt = f'{config["mqtt"]["base_topic"]}/{devices_config["name"]}/online'
        client.will_set(mqtt_lwt, payload=0, qos=0, retain=True)
        client.on_connect = mqtt_onconnect
        client.on_disconnect = mqtt_ondisconnect
        client.on_message = mqtt_onmessage

        client.connect(config['mqtt']['host'], config['mqtt']['port'], 60)
        client.loop_start()

        output = output_mqtt
    elif config['logger'] == 'syslog':
        logger.addHandler(handler)
        output = output_syslog
    elif config['logger'] == 'print':
        output = output_print
    elif config['logger'] == 'json':
        output = output_json
    else:
        logger.addHandler(handler)
        logger.error('No output specified!')
        sys.exit(1)

    dashboard = None
    if args.dashboard:
        dashboard = LiveDashboard(devices_config['name'], output)
        output = dashboard

    q = queue.Queue()

    #logger.debug("Start worker thread")
    #t = threading.Timer(2+(1*5), victron_thread, args=(1, config, devices_config, q))
    #t.start()

    try:
        victron_thread(1, config, devices_config, q)
    finally:
        if dashboard is not None:
            dashboard.shutdown()
