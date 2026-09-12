import json
import re

## START: BLUETOOTH CONVERT FUNCTIONS
def extract_firmware_version(value):
    if value == b'\xff\xff\xff':
        return 'NO FIRMWARE'
    if value[2] != 0:
        version = f'v{value[2]}{value[1]:02}.{value[0]:02}'
    else:
        version = f'v{value[1]}.{value[0]:02}'
    return version

def convert_value_number(value, command):
    converted = int.from_bytes(value, "little", signed=command[4])
    result = converted / command[3]
    # return numeric type (float or int)
    if float(result).is_integer():
        return int(result)
    return float(result)

def convert_value_int(value, command):
    converted = int.from_bytes(value, "little", signed=command[4])
    return int(converted / command[3])

def convert_value_string(value, command):
    return str(value.decode("ASCII"))

def convert_value_firmware(value, command):
    return extract_firmware_version(value[1:])

def convert_value_udf(value, command):
    return extract_firmware_version(value[0:3])

def convert_value_identify(value, command):
    if int.from_bytes(value, "little") == 0:
        return "normal operation (default)"
    else:
        return "identification mode (blink/beep)"

def convert_value_unknown(value, command):
    return str(value)
## END: BLUETOOTH CONVERT FUNCTIONS

# TODO: Merge Bluetooth and Serial convert functions

## START: SERIAL CONVERT FUNCTIONS
def convert_int_factor(value, command):
    try:
        int(value)
    except ValueError:
        return str(value)

    data = int(value) * command[3]
    # Return numeric types: int when whole, float otherwise (rounded to 2 decimals)
    try:
        f = float(data)
        if f.is_integer():
            return int(f)
        return round(f, 2)
    except Exception:
        return data


def convert_time_to_go(value, command):
    """Convert the VE.Direct TTG -1 sentinel to Home Assistant's unknown state."""
    if str(value) == '-1':
        return 'unknown'
    return convert_int_factor(value, command)


def convert_str_out(value, command):
    return value


def convert_map_out(value, command):
    # Return only the mapped name (e.g. "SmartShunt 300A/50mV") without the hex PID prefix
    try:
        return command[3][value]
    except Exception:
        # fallback to a reasonable string if mapping missing
        return str(value)


def convert_warn_ar(value, command):
    value = int(value)
    raw_str = f"{value}: "
    raw_helper = []
    for i in range(13, -1, -1):
        if (2 ** i) <= value:
            raw_helper.append(command[3][2 ** i])
            value = value - (2 ** i)
    if len(raw_helper) == 0:
        raw_helper.append('None')
    return raw_str + "|".join(raw_helper)


def convert_firmware(fw_raw, command):
    if fw_raw == b'\xff\xff\xff':
        return 'NO FIRMWARE'
    if fw_raw[0] == '0':
        return f'{fw_raw[1:2]}.{fw_raw[2:]}'
    else:
        return f'{fw_raw[0:1]}.{fw_raw[1:2]}{fw_raw[2:]}'


def convert_production_date(value, command):
    return f'year: 20{value[2:4]}, week: {value[4:6]}'
## END: SERIAL CONVERT FUNCTIONS


def collection_check_full(collection):
    for value in collection.values():
        if value is None:
            return False
    return True


def slugify_identifier(value):
    """Return a Home Assistant MQTT discovery topic-safe identifier."""
    return re.sub(r'[^A-Za-z0-9_-]+', '_', str(value)).strip('_') or 'sensor'


def add_hass_sensor_metadata(config, category, description, unit):
    """Add Home Assistant sensor metadata for a mapped Victron value."""
    device_classes = {
        '%': 'battery',
        'V': 'voltage',
        'A': 'current',
        'W': 'power',
        '°C': 'temperature',
        'Wh': 'energy',
        'kWh': 'energy',
        'min': 'duration',
        's': 'duration',
    }
    display_precision = {
        '%': 1,
        'V': 2,
        'A': 2,
        'Ah': 2,
        'W': 0,
        'Wh': 2,
        'kWh': 2,
        '°C': 0,
        'min': 0,
        's': 0,
    }

    # Home Assistant has no valid sensor device_class for Ah (amp-hours).
    if unit:
        config['unit_of_measurement'] = unit
        config['state_class'] = 'measurement'
        config['suggested_display_precision'] = display_precision.get(unit, 0)

    if unit in device_classes:
        config['device_class'] = device_classes[unit]

    # Only lifetime energy counters are monotonic. Daily and yesterday values reset.
    if description in ('Energy All Time', 'Total Charged Energy', 'Total Discharged Energy'):
        config['state_class'] = 'total_increasing'


def build_hass_discovery_config(device_name, model, serial, firmware, sensor_config, base_topic, subtopic, value_template, collection):
    """
    Builds the config for homeassistant mqtt discovery
    :param device_name: Name of device
    :param model: model description of device
    :param serial: serial number of device
    :param firmware: firmware of device
    :param sensor_config: mapping row from device classes
    :param base_topic: MQTT base topic
    :param subtopic: Subtopic is either the name of the sensor (e.g. Voltage) or of the collection (e.g. latest)
    :param value_template: sensor (e.g. Voltage)
    :param collection: None or a collection
    :return:
    """
    category, description, unit, _, _ = sensor_config
    device_id = slugify_identifier(device_name)
    entity_id = slugify_identifier(value_template)
    hass_config_topic = f'homeassistant/sensor/{device_id}/{entity_id}/config'
    hass_config_data = {
        'unique_id': f'victron_{device_id}_{entity_id}',
        'name': value_template,
        'state_topic': f'{base_topic}/{device_name}/{subtopic}',
        'availability_topic': f'{base_topic}/{device_name}/online',
        'payload_available': '1',
        'payload_not_available': '0',
    }

    if unit == 'timestamp':
        hass_config_data['device_class'] = 'timestamp'
    else:
        add_hass_sensor_metadata(hass_config_data, category, description, unit)

    if category in ('Latest', 'Battery'):
        hass_config_data['expire_after'] = 600

    # Direct topics now publish a single numeric value and need no template.
    # Collections include each value as {'value': ..., 'unit': ..., 'updated': ...}.
    if collection is not None:
        hass_config_data['value_template'] = "{{ value_json['" + value_template + "']['value'] }}"

    hass_device = {
        "identifiers": [f'victron_{device_name}'],
        "manufacturer": 'Victron',
        "model": f'{model}' + (f' Serial: {serial}' if serial not in (None, '', 'SER# NOT SUPPORTED') else ''),
        "name": device_name,
        "sw_version": firmware
    }

    hass_config_data["device"] = hass_device

    return hass_config_topic, json.dumps(hass_config_data)


def send_hass_config_payload(device_name, pid, ser, fw, mapping_table, base_topic, output, collections):
    for key, value in mapping_table.items():
        subtopic = value[1]
        value_template = value[1]
        collection = None

        if collections is not None:
            for ckey, cvalue in collections.items():
                if value[1] in cvalue:
                    subtopic = ckey
                    collection = ckey

        hass_config_subtopic, hass_config_data = build_hass_discovery_config(
            device_name,
            pid,
            ser,
            fw,
            value,
            base_topic,
            subtopic,
            value_template,
            collection
        )

        output(device_name, hass_config_subtopic, hass_config_data, hass_config=True)

    # Remove the legacy synthetic timestamp entry before registering its dedicated
    # replacement. The new state topic receives an ISO-8601 timestamp per packet.
    output(device_name, f'homeassistant/sensor/{slugify_identifier(device_name)}/Updated/config', '', True)

    hass_config_subtopic, hass_config_data = build_hass_discovery_config(
        device_name,
        pid,
        ser,
        fw,
        ('Meta', 'Last Update', 'timestamp', 0, None),
        base_topic,
        'Last Update',
        'Last Update',
        None
    )
    output(device_name, hass_config_subtopic, hass_config_data, True)


def send_hass_button_config_payload(device_name, pid, ser, fw, base_topic, output, commands):
    """Publish Home Assistant MQTT Button discovery for allowed device commands."""
    device_id = slugify_identifier(device_name)
    hass_device = {
        "identifiers": [f'victron_{device_name}'],
        "manufacturer": 'Victron',
        "model": f'{pid}' + (f' Serial: {ser}' if ser not in (None, '', 'SER# NOT SUPPORTED') else ''),
        "name": device_name,
        "sw_version": fw,
    }

    for action, command in commands.items():
        name = command['name']
        hass_config_topic = f'homeassistant/button/{device_id}/{action}/config'
        hass_config_data = {
            'unique_id': f'victron_{device_id}_{action}',
            'name': name,
            'command_topic': f'{base_topic}/{device_name}/command/{action}',
            'payload_press': 'PRESS',
            'availability_topic': f'{base_topic}/{device_name}/online',
            'payload_available': '1',
            'payload_not_available': '0',
            'entity_category': 'config',
            'icon': command['icon'],
            'device': hass_device,
        }
        if command.get('enabled_by_default') is False:
            hass_config_data['enabled_by_default'] = False

        output(device_name, hass_config_topic, json.dumps(hass_config_data), hass_config=True)
