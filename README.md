# VictronConnect Bluetooth & Serial

**This repository is in no way approved by or affiliated with the official Victron Energy repository.**
**I am not responsible for any problems or damages with your devices or this code**

**Only run this script if you are sure you know what you are doing!**

This repository is based on [https://github.com/vvvrrooomm/victron](https://github.com/vvvrrooomm/victron).

I use this repository to have a running version with mqtt for my camping car.
If you want more information about the development and a wireshark dissector, refer to vvvrrooomm's repository.

## New version for the new bluetooth gatt api used in the beta firmware of the SmartShunt
See [https://community.victronenergy.com/questions/93919/victron-bluetooth-ble-protocol-publication.html](https://community.victronenergy.com/questions/93919/victron-bluetooth-ble-protocol-publication.html)
for more information about how to activate the new gatt protocol with the beta firmware.

The Bluetooth-BLE (new gatt protocol) and the serial protocol works quiet stable. The normal bluetooth protocol is the hardest to implement. Many values are still missing.

More features will be added soon. 

## Ability of this repository
The script is tested with python > 3.7
### Supported/tested devices:
Bluetooth BLE: 
- Smart Shunt

Bluetooth:
- Smart Shunt
- Smart Solar 100/30
- Orion Smart 12/12-30

Serial:
- Phoenix Inverter 12 800VA 230V
- Smart Shunt
- Smart Solar 100/30

### Outputs (Single values or as collection of values)
- mqtt
- syslog
- print
- json

### Autostart scripts (systemd)
These scripts are written for my specific config file. If you have your devices in different order, you may need to adjust them.

## Howto use
---
**NOTE**

If you want serial communication with devices such as the Phoenix Inverter or a SmartShunt, you must install this library:
- vedirect: https://github.com/karioja/vedirect

There are some more commandline arguments, you can view them with `python3 victron.py --help`

---

1. You need to install python3-dbus 
   For debian based system: `apt install python3-dbus`
   For arch linux: `pacman -S python-dbus`
2. You need to install some requirements: `pip3 install -r requirements.txt`
3. Add your devices to the configuration file. See "Configuration in detail" section for examples.
4. If you are using bluetooth or bluetooth ble you must be pairing your devices via bluetoothctl
    ```
    # Open bluetoothctl from commandline
    bluetoothctl
    
    # Enable scanning
    scan on
      
    # Get the mac address of your device
    # pair device and enter pin
    pair MAC
    
    ```
5. Start the script for your desired device: `python3 victron.py -d 0`
6. For autostart tweak systemd files to your need, link them to `/etc/systemd/system/`, then start and/or enable systemd file
    ```
    ln -s /opt/victron/systemd/victron-shunt.service /etc/systemd/system/victron-shunt.service
    
    systemctl enable victron-shunt.service
    systemctl start victron-shunt.service
    ```

### Commandline arguments
```
./victron.py -h
usage: victron.py [-h] [--debug] [--quiet] [-c] [-C CONFIG_FILE] [-D] [-v] [-d NUM / NAME]

Victron Reader (Bluetooth, BLE and Serial) 

Current supported devices:
  Full: 
    - Smart Shunt (Bluetooth BLE)
    - Phoenix Inverter (Serial)
    - Smart Shunt (Serial)
    - Smart Solar (Serial)
    - Blue Solar (Serial)
  Partial: 
    - Smart Shunt (Bluetooth)
    - Smart Solar (Bluetooth)
    - Orion Smart (Bluetooth)
Default behavior:
  1. It will connect to given device
  2. Collect and log data summary as defined at the config file
  3. Disconnect and start over with timers set in config file

options:
  -h, --help            show this help message and exit

  --debug               Set log level to debug
  --quiet               Set log level to error

  -c, --collection      Output only collections specified in config instead of single values
  -C CONFIG_FILE, --config-file CONFIG_FILE
                        Specify different config file [Default: config.yml]
  -D, --direct-disconnect
                        Disconnect direct after getting values
  -v, --version         Show version and exit

  -d NUM / NAME, --device NUM / NAME
                        0: Shunt1 | 

```

#### Meta
##### -h / --help
Show help.

##### -v / --version
Show version.

#### Mandatory
##### -d / --device NUMBER|NAME
You need to specify the device from configuration which you want to connect to.

#### Optional
##### --debug / --info
Set log level.

##### -c / --collection
Define a collection to "merge" values in to one output instead of output every value separately. A collection must look like:
```
collections:
  DEVICENAME:
    COLLECTIONNAME:
      - VALUENAME
      - VALUENAME2
```
You need to set the DEVICENAME the same as your device. You can choose the COLLECTIONNAME freely, it will be used in json output as key. 
The VALUENAMES must match with the code, see config.yml for possible VALUENAMES and more examples.

##### -C / --config-file FILENAME
Specify a config file other than default (config.yml).

##### -D / --direct-disconnect
Default the program will not exit on its own. If you want to collect the values from a device and exit, use this option. Be aware, that it will act different on the different connection protocols.

**serial / bluetooth-ble**: It will exit after the first value. To get all values once, you need to specify a collection to exit after all values are returned.\
**bluetooth**: It will exit after the auto disconnect of the device and return all values gathered until then.

### FAQ
#### No output shown with following log message "error: Not connected"
Please check if you paired the Victron device correctly via Bluetooth using bluetoothctl. If you changed the PIN of the Victron device, delete and repair the device.

### Configuration in detail
#### Device section
**Mandatory:**
Name, type, protocol, and MAC address or serial port, depending on the type. Bluetooth and Bluetooth BLE need the Bluetooth MAC address of the Victron device specified.
<br>
Name: Choose yourself<br>
Type: phoenix, smartshunt, smartsolar, orionsmart<br>
Protocol: serial, bluetooth, bluetooth-ble
```buildoutcfg
    - name: Phoenix1
      type: phoenix
      protocol: serial
      port: /dev/victron-phoenix
```
or
```buildoutcfg
    - name: Shunt1
      type: smartshunt
      protocol: bluetooth-ble
      mac: fd:d4:50:0f:6c:1b
```
or
```buildoutcfg
    - name: Solar1
      type: smartsolar
      protocol: bluetooth
      mac: F9:8E:1C:EC:9C:72
```
**Optional:**


#### Output section
You can choose between:
```buildoutcfg
logger: mqtt
```
or 
```buildoutcfg
logger: syslog
```
or 
```buildoutcfg
logger: print
```
or 
```buildoutcfg
logger: json
```
#### MQTT section
Choose host, port, base_topic and whether to enable Home Assistant Discovery. Authentication is supported with the optional `username` and `password` settings.
```buildoutcfg
mqtt:
    host: 192.168.3.2
    port: 1883
    base_topic: victron
    hass: True
```

  ##### SmartShunt Home Assistant controls
  For a serial SmartShunt, set `hass_commands: True` to publish Home Assistant MQTT buttons for **Zero Current**, **Synchronize Battery Monitor**, and **Clear SmartShunt History**. Each button only publishes the fixed `PRESS` payload to its dedicated command topic; arbitrary VE.Hex commands are never accepted.

  `hass_restore_defaults` defaults to `False`. Set it to `True` only when a **Restore SmartShunt Defaults** button is explicitly required, because it overwrites SmartShunt settings. The device pauses normal VE.Direct text telemetry for several seconds after any command, and this integration reports that the frame was sent—not that the device acknowledged it.
#### Collections section

You can specify if you want the values get summarized into one json output statement. Otherwise it will send out every value as soon as it is collected from victron device. 
See configfile for more information!

## Known issues
- The devices with bluetooth protocol are currently auto disconnecting after 30 seconds. This may prevent some values from being gathered.
- Orion Smart must be more reverse engineered to get some more interesting values
- Bluetooth: You cannot get history values from SmartSolar. The protocol itself is decoded and working for this part, but SmartSolar does not send the data. Another initialization sequence may be needed; the correct sequence is not known yet.
- Serial: Smart Solar history currently not gathered

Feel free to help improving this repository.

## Future plans:
- Choose via config file which values should be printed
- Choose how often values should be printed (especially Bluetooth with notifications)
- CMD Parameter instead of config (easier testing of new devices)
- SmartSolar history values
- Get device settings via serial and bluetooth
- Add a config checker to avoid invalid config parameter
