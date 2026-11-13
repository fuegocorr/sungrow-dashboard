# Technische Details

🇬🇧 [English version](technical.md)

Ergänzung zur [Kurzanleitung](../README.de.md): Einstellungen, API, Register, MQTT-Topics, systemd und Test mit dem Simulator.

Start von Hand: `python server.py`, Optionen `--port 9000` und `--host 192.168.1.10` (nur an eine Schnittstelle binden).

## Einstellungen

| Feld | Standard | Hinweis |
|---|---|---|
| IP-Adresse | – | IPv4 oder Hostname |
| Port | 502 | |
| Gerätetyp | WiNet-S / Wechselrichter | Unit-ID 1, SH-Registermap; iHomeManager: Unit-ID 247, iHM-Map; kombiniert: beide (siehe unten) |
| Unit-ID | vom Gerätetyp | überschreibbar |
| Abfrageintervall | 10 s | 2 bis 3600 s |

"Verbindung testen" liest alle Register der gewählten Map und zeigt die Werte.
Der Test trennt dafür kurz die laufende Verbindung, weil Sungrow nur einen
Modbus-Client gleichzeitig verträgt.

## API

| Methode | Pfad | |
|---|---|---|
| GET | `/` | Dashboard |
| GET | `/api/values` | aktuelle Werte mit Zeitstempel |
| GET | `/api/settings` | gespeicherte Einstellungen |
| POST | `/api/settings` | speichern, Poller verbindet neu |
| POST | `/api/test` | Verbindungstest ohne Speichern |

Body für beide POSTs:

```json
{"host": "192.168.1.50", "port": 502, "device_type": "winet", "unit_id": 1, "interval": 10}
```

Vorzeichen in `/api/values`: `battery_power` > 0 = Entladen, < 0 = Laden;
`grid_power` > 0 = Netzbezug, < 0 = Einspeisung. Nicht verfügbare Werte sind `null`.

Fehlermeldungen kommen auf Deutsch; mit `?lang=en` an der Adresse (z. B. `/api/values?lang=en`) auf Englisch.
Optionale MQTT-Felder im Body: `mqtt_enabled`, `mqtt_host`, `mqtt_port`, `mqtt_user`, `mqtt_password`,
`mqtt_prefix`, `mqtt_language` (`de` oder `en`, Sprache der Sensornamen).

## Register

Die Adressen stehen mit Quellenangabe in `registers.py` und stammen aus:

- SH-Map: `modbus_sungrow.yaml` aus mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant
- iHM-Map: `templates/definition/meter/sungrow-ihm.yaml` aus dem evcc-Repo

Die iHM-Map enthält nur WR-Ausgangsleistung (AC), Netz- und Batterieleistung
sowie SOC. Register 8155 ist am echten Gerät die AC-Leistung des Wechselrichters
(PV + Batterie-Entladung - Verluste), nicht die PV-Leistung.

### Gerätetyp "iHomeManager + Wechselrichter (kombiniert)"

Hängt der Wechselrichter am iHomeManager, reicht dieser Unit-ID 1 an den
Wechselrichter durch. Der kombinierte Modus liest über eine Verbindung beide Maps:

| Wert | Quelle |
|---|---|
| PV-Leistung, Tagesertrag, Status | Wechselrichter (Unit-ID 1) |
| Batterie-SOC, Batterie-Leistung | Wechselrichter, ersatzweise iHM |
| Netz | iHM, Register 8157 (beim Wechselrichter steht hier 0, der Zähler hängt am iHM) |
| Hausverbrauch | berechnet: iHM-AC-Leistung (8155) + Netzbezug |

Im reinen iHM-Modus fehlen Tagesertrag und Status; die PV-Leistung wird dort
als AC-Leistung minus Batterie-Entladung genähert (ohne Wandlungsverluste).

## Home Assistant (MQTT)

Im Einstellungsformular unter "Home Assistant (MQTT)" aktivieren und Broker,
Port (Standard 1883), Benutzer, Passwort und Topic-Präfix (Standard `sungrow`)
eintragen. Voraussetzung in Home Assistant: ein MQTT-Broker (z. B. das Add-on
"Mosquitto broker") und die MQTT-Integration mit aktivierter Discovery (Standard).

Das Gerät "Sungrow SH15T" erscheint dann automatisch mit diesen Sensoren:

| Sensor | Entität | Einheit |
|---|---|---|
| PV-Leistung | `sensor.sungrow_pv_power` | W |
| Tagesertrag | `sensor.sungrow_daily_yield` | kWh |
| Batterie-SOC | `sensor.sungrow_battery_soc` | % |
| Batterie-Leistung (> 0 = Entladen) | `sensor.sungrow_battery_power` | W |
| Batterie Laden / Entladen (jeweils ≥ 0) | `sensor.sungrow_battery_charge_power`, `..._discharge_power` | W |
| Netzleistung (> 0 = Bezug) | `sensor.sungrow_grid_power` | W |
| Netzbezug / Einspeisung (jeweils ≥ 0) | `sensor.sungrow_grid_import_power`, `..._export_power` | W |
| Hausverbrauch | `sensor.sungrow_load_power` | W |
| Wechselrichter-Status | `sensor.sungrow_status` | Text |

Topics:

- `sungrow/state`: JSON mit allen Werten, nach jeder Abfrage
- `sungrow/status`: `online` / `offline` (retained, Last Will). Offline heißt:
  Server läuft nicht oder der Wechselrichter antwortet nicht; die Sensoren sind
  dann in Home Assistant "nicht verfügbar".
- `homeassistant/sensor/sungrow/<wert>/config`: Discovery (retained)

Wird MQTT abgeschaltet oder das Präfix geändert, entfernt der Server die
Discovery-Einträge wieder. Der Server veröffentlicht nur und nimmt über MQTT
keine Befehle an. Das Passwort liegt im Klartext in `settings.json` (unter
Linux nur für den Dienstbenutzer lesbar) und wird nie an den Browser zurückgegeben.

Für das Energie-Dashboard braucht Home Assistant Energiewerte (kWh). Der
Tagesertrag ist direkt nutzbar; für Netz und Batterie in Home Assistant je einen
Helfer "Integral (Riemann-Summe)" auf die getrennten Leistungssensoren anlegen.

## Netzwerk

Der Server lauscht auf allen Schnittstellen, beantwortet aber nur Anfragen aus
privaten Adressbereichen (sonst HTTP 403). Keine Portfreigabe im Router einrichten.

## Dauerbetrieb mit systemd

`deploy/install.sh` richtet den Dienst automatisch ein. Von Hand: `/etc/systemd/system/sungrow-dashboard.service` (Pfade und Benutzer anpassen):

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

## Test ohne Wechselrichter

`tools/sim_modbus.py` ist ein kleiner Modbus-Simulator mit Beispielwerten für beide Maps:

```bash
python tools/sim_modbus.py --port 5020 &
python server.py --port 8080 &
curl -X POST localhost:8080/api/settings -H "Content-Type: application/json" \
  -d '{"host":"127.0.0.1","port":5020,"device_type":"winet","unit_id":1,"interval":5}'
curl localhost:8080/api/values
```
