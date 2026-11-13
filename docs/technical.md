# Technical details

🇩🇪 [Deutsche Version](technik.md)

Supplement to the [quick guide](../README.md): settings, API, registers, MQTT topics, systemd and testing with the simulator.

Manual start: `python server.py`, options `--port 9000` and `--host 192.168.1.10` (bind to one interface only).

## Settings

| Field | Default | Note |
|---|---|---|
| IP address | – | IPv4 or hostname |
| Port | 502 | |
| Device type | WiNet-S / inverter | Unit ID 1, SH register map; iHomeManager: unit ID 247, iHM map; combined: both (see below) |
| Unit ID | from device type | can be overridden |
| Polling interval | 10 s | 2 to 3600 s |

"Test connection" reads all registers of the selected map and shows the values.
For this the test briefly closes the running connection, because Sungrow only
tolerates one Modbus client at a time.

## API

| Method | Path | |
|---|---|---|
| GET | `/` | Dashboard |
| GET | `/api/values` | current values with timestamp |
| GET | `/api/settings` | stored settings |
| POST | `/api/settings` | save, poller reconnects |
| POST | `/api/test` | connection test without saving |

Body for both POSTs:

```json
{"host": "192.168.1.50", "port": 502, "device_type": "winet", "unit_id": 1, "interval": 10}
```

Signs in `/api/values`: `battery_power` > 0 = discharging, < 0 = charging;
`grid_power` > 0 = grid import, < 0 = feed-in. Unavailable values are `null`.

Error messages are in German; append `?lang=en` to the address (e.g. `/api/values?lang=en`) to get them in English.
Optional MQTT fields in the body: `mqtt_enabled`, `mqtt_host`, `mqtt_port`, `mqtt_user`, `mqtt_password`,
`mqtt_prefix`, `mqtt_language` (`de` or `en`, language of the sensor names).

## Registers

The addresses are listed with their source in `registers.py` and come from:

- SH map: `modbus_sungrow.yaml` from mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant
- iHM map: `templates/definition/meter/sungrow-ihm.yaml` from the evcc repository

The iHM map only contains inverter output power (AC), grid power, battery power
and SOC. On the real device, register 8155 is the AC power of the inverter
(PV + battery discharge - losses), not the PV power.

### Device type "iHomeManager + inverter (combined)"

If the inverter is connected to the iHomeManager, the iHomeManager passes unit
ID 1 through to the inverter. The combined mode reads both maps over one connection:

| Value | Source |
|---|---|
| PV power, daily yield, status | Inverter (unit ID 1) |
| Battery SOC, battery power | Inverter, iHM as fallback |
| Grid | iHM, register 8157 (the inverter reports 0 here, the meter is connected to the iHM) |
| Home consumption | calculated: iHM AC power (8155) + grid import |

In pure iHM mode, daily yield and status are missing; PV power is approximated
there as AC power minus battery discharge (without conversion losses).

## Home Assistant (MQTT)

Enable it in the settings form under "Home Assistant (MQTT)" and enter broker,
port (default 1883), user, password and topic prefix (default `sungrow`).
Required in Home Assistant: an MQTT broker (e.g. the "Mosquitto broker" add-on)
and the MQTT integration with discovery enabled (the default).

The device "Sungrow SH15T" then appears automatically with these sensors
(names shown for sensor name language English):

| Sensor | Entity | Unit |
|---|---|---|
| PV power | `sensor.sungrow_pv_power` | W |
| Daily yield | `sensor.sungrow_daily_yield` | kWh |
| Battery SOC | `sensor.sungrow_battery_soc` | % |
| Battery power (> 0 = discharging) | `sensor.sungrow_battery_power` | W |
| Battery charging / discharging (each ≥ 0) | `sensor.sungrow_battery_charge_power`, `..._discharge_power` | W |
| Grid power (> 0 = import) | `sensor.sungrow_grid_power` | W |
| Grid import / feed-in (each ≥ 0) | `sensor.sungrow_grid_import_power`, `..._export_power` | W |
| Home consumption | `sensor.sungrow_load_power` | W |
| Inverter status | `sensor.sungrow_status` | text |

Topics:

- `sungrow/state`: JSON with all values, after every poll
- `sungrow/status`: `online` / `offline` (retained, last will). Offline means:
  the server is not running or the inverter does not answer; the sensors are
  then "unavailable" in Home Assistant.
- `homeassistant/sensor/sungrow/<value>/config`: discovery (retained)

If MQTT is switched off or the prefix is changed, the server removes its
discovery entries again. The server only publishes and accepts no commands via
MQTT. The password is stored in plain text in `settings.json` (on Linux readable
only by the service user) and is never sent back to the browser.

For the energy dashboard Home Assistant needs energy values (kWh). The daily
yield can be used directly; for grid and battery, create one "Integral (Riemann
sum)" helper each in Home Assistant on the separate power sensors.

## Network

The server listens on all interfaces but only answers requests from private
address ranges (HTTP 403 otherwise). Do not set up port forwarding on your router.

## Permanent operation with systemd

`deploy/install.sh` sets up the service automatically. Manually: `/etc/systemd/system/sungrow-dashboard.service` (adjust paths and user):

```ini
[Unit]
Description=Sungrow SH15T Dashboard
After=network-online.target
Wants=network-online.target

[Service]
User=sungrow
WorkingDirectory=/opt/sungrow-dashboard
ExecStart=/opt/sungrow-dashboard/.venv/bin/python server.py --port 8080
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/sungrow-dashboard
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sungrow-dashboard
journalctl -u sungrow-dashboard -f
```

## Testing without an inverter

`tools/sim_modbus.py` is a small Modbus simulator with example values for both maps:

```bash
python tools/sim_modbus.py --port 5020 &
python server.py --port 8080 &
curl -X POST localhost:8080/api/settings -H "Content-Type: application/json" \
  -d '{"host":"127.0.0.1","port":5020,"device_type":"winet","unit_id":1,"interval":5}'
curl localhost:8080/api/values
```
