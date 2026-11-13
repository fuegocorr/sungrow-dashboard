"""MQTT-Ausgabe für Home Assistant (MQTT Discovery).

Topics (Präfix einstellbar, Standard "sungrow"):
  <präfix>/status   "online" / "offline"  (retained, Last Will)
                    online = Server läuft UND Wechselrichter antwortet
  <präfix>/state    JSON mit allen Werten, nach jeder Abfrage
  homeassistant/sensor/<präfix>/<wert>/config   Discovery (retained)

Es wird nur veröffentlicht; der Server abonniert nichts und nimmt über MQTT
keine Befehle an.
"""

import json
import logging
import threading
import time

import paho.mqtt.client as mqtt

log = logging.getLogger("sungrow.mqtt")

DISCOVERY_PREFIX = "homeassistant"

# key, Name (de, en), Einheit, device_class, state_class, Icon
SENSORS = [
    ("pv_power", ("PV-Leistung", "PV power"), "W", "power", "measurement", "mdi:solar-power"),
    ("daily_yield", ("Tagesertrag", "Daily yield"), "kWh", "energy", "total_increasing", "mdi:solar-power-variant"),
    ("battery_soc", ("Batterie-SOC", "Battery SOC"), "%", "battery", "measurement", None),
    ("battery_power", ("Batterie-Leistung", "Battery power"), "W", "power", "measurement", "mdi:home-battery"),
    ("battery_charge_power", ("Batterie Laden", "Battery charging"), "W", "power", "measurement", "mdi:battery-arrow-up"),
    ("battery_discharge_power", ("Batterie Entladen", "Battery discharging"), "W", "power", "measurement", "mdi:battery-arrow-down"),
    ("grid_power", ("Netzleistung", "Grid power"), "W", "power", "measurement", "mdi:transmission-tower"),
    ("grid_import_power", ("Netzbezug", "Grid import"), "W", "power", "measurement", "mdi:transmission-tower-import"),
    ("grid_export_power", ("Einspeisung", "Grid feed-in"), "W", "power", "measurement", "mdi:transmission-tower-export"),
    ("load_power", ("Hausverbrauch", "Home consumption"), "W", "power", "measurement", "mdi:home-lightning-bolt"),
    ("status", ("Wechselrichter-Status", "Inverter status"), None, None, None, "mdi:information-outline"),
]


def state_payload(values: dict) -> dict:
    """Dashboard-Werte -> MQTT-Zustand (zusätzlich getrennte Lade-/Bezugswerte)."""
    v = values or {}
    out = {k: v.get(k) for k in ("pv_power", "daily_yield", "battery_soc", "battery_power",
                                 "grid_power", "load_power", "status")}
    bat, grid = out["battery_power"], out["grid_power"]
    # Vorzeichen wie im Dashboard: Batterie > 0 = Entladen, Netz > 0 = Bezug
    out["battery_charge_power"] = None if bat is None else max(0, -bat)
    out["battery_discharge_power"] = None if bat is None else max(0, bat)
    out["grid_import_power"] = None if grid is None else max(0, grid)
    out["grid_export_power"] = None if grid is None else max(0, -grid)
    return out


def discovery_messages(prefix: str, lang: str = "de") -> list:
    """Liste (topic, payload) der Discovery-Konfigurationen; lang = Sprache der Sensornamen."""
    device = {
        "identifiers": [f"sungrow_dashboard_{prefix}"],
        "name": "Sungrow SH15T",
        "manufacturer": "Sungrow",
        "model": "SH15T",
    }
    msgs = []
    for key, names, unit, dev_class, state_class, icon in SENSORS:
        cfg = {
            "name": names[1] if lang == "en" else names[0],
            "unique_id": f"{prefix}_{key}",
            "object_id": f"{prefix}_{key}",
            "state_topic": f"{prefix}/state",
            # JSON null wird zu "None" -> Home Assistant zeigt "unbekannt" statt 0
            "value_template": "{{ value_json.%s }}" % key,
            "availability_topic": f"{prefix}/status",
            "device": device,
        }
        if unit:
            cfg["unit_of_measurement"] = unit
        if dev_class:
            cfg["device_class"] = dev_class
        if state_class:
            cfg["state_class"] = state_class
        if icon:
            cfg["icon"] = icon
        msgs.append((f"{DISCOVERY_PREFIX}/sensor/{prefix}/{key}/config", json.dumps(cfg)))
    return msgs


class MqttPublisher:
    """Hält die Verbindung zum Broker (paho, eigener Thread, automatischer Reconnect)."""

    def __init__(self):
        self.client = None
        self.cfg = None  # aktive Einstellungen (nur MQTT-Teil)
        self.connected = False
        self.error = None  # (Meldungsschlüssel, Detail); der Server übersetzt
        self._online = False
        self._sent_online = None  # zuletzt gesendeter Status (nur Änderungen senden)
        self._state = None
        self._lock = threading.Lock()

    # -- Status für das Dashboard --
    def status(self) -> dict:
        return {
            "enabled": self.cfg is not None,
            "connected": self.connected,
            "error": self.error,
            "broker": f"{self.cfg['mqtt_host']}:{self.cfg['mqtt_port']}" if self.cfg else None,
        }

    # -- Konfiguration (blockiert kurz; aus asyncio per to_thread aufrufen) --
    def configure(self, settings: dict) -> None:
        new = None
        if settings.get("mqtt_enabled") and settings.get("mqtt_host"):
            new = {k: settings[k] for k in ("mqtt_host", "mqtt_port", "mqtt_user",
                                            "mqtt_password", "mqtt_prefix", "mqtt_language")}
        if new == self.cfg:
            return
        # Entitäten in Home Assistant entfernen, wenn MQTT abgeschaltet oder das Präfix geändert wird
        remove = self.cfg is not None and (new is None or new["mqtt_prefix"] != self.cfg["mqtt_prefix"])
        self.stop(remove_discovery=remove)
        if new is None:
            return
        self.cfg = new
        prefix = new["mqtt_prefix"]
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sungrow-dashboard-{prefix}")
        if new["mqtt_user"]:
            client.username_pw_set(new["mqtt_user"], new["mqtt_password"] or None)
        client.will_set(f"{prefix}/status", "offline", qos=1, retain=True)
        client.reconnect_delay_set(min_delay=2, max_delay=60)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_connect_fail = self._on_connect_fail
        self.client = client
        client.connect_async(new["mqtt_host"], new["mqtt_port"], keepalive=60)
        client.loop_start()

    def stop(self, remove_discovery: bool = False) -> None:
        client, cfg = self.client, self.cfg
        self.client, self.cfg, self.connected, self.error = None, None, False, None
        if client is None:
            return
        try:
            if client.is_connected():
                prefix = cfg["mqtt_prefix"]
                # beim Entfernen auch den gespeicherten Status löschen (leere Nachricht)
                infos = [client.publish(f"{prefix}/status", "" if remove_discovery else "offline",
                                        qos=1, retain=True)]
                if remove_discovery:
                    infos += [client.publish(t, "", qos=1, retain=True)
                              for t, _ in discovery_messages(prefix)]
                for info in infos:
                    info.wait_for_publish(timeout=2)
                if remove_discovery:
                    time.sleep(0.5)  # dem Broker Zeit geben, bevor die Verbindung endet
            client.disconnect()
        except Exception as exc:  # Aufräumen darf nie scheitern
            log.debug("MQTT-Trennen: %s", exc)
        client.loop_stop()

    # -- Veröffentlichen (threadsicher, blockiert nicht) --
    def publish(self, snapshot: dict) -> None:
        with self._lock:
            self._online = bool(snapshot.get("connected"))
            self._state = json.dumps(state_payload(snapshot.get("values")))
        self._send()

    def _send(self) -> None:
        client, cfg = self.client, self.cfg
        if client is None or not self.connected:
            return
        with self._lock:
            online, state = self._online, self._state
        prefix = cfg["mqtt_prefix"]
        if online != self._sent_online:
            client.publish(f"{prefix}/status", "online" if online else "offline", qos=1, retain=True)
            self._sent_online = online
        if online and state is not None:
            client.publish(f"{prefix}/state", state, qos=0, retain=False)

    # -- paho-Callbacks (laufen im paho-Thread) --
    def _on_connect(self, client, _userdata, _flags, reason_code, _props=None):
        if client is not self.client:
            return
        if reason_code.is_failure:
            self.connected = False
            self.error = ("mqtt_refused", str(reason_code))
            log.warning("MQTT: Broker lehnt ab: %s", reason_code)
            return
        self.connected, self.error = True, None
        self._sent_online = None
        log.info("MQTT verbunden mit %s:%s", self.cfg["mqtt_host"], self.cfg["mqtt_port"])
        for topic, payload in discovery_messages(self.cfg["mqtt_prefix"], self.cfg["mqtt_language"]):
            client.publish(topic, payload, qos=1, retain=True)
        self._send()

    def _on_disconnect(self, client, _userdata, _flags, reason_code, _props=None):
        if client is not self.client:
            return
        self.connected = False
        if reason_code.is_failure:
            self.error = ("mqtt_lost", str(reason_code))
            log.warning("MQTT: Verbindung getrennt: %s", reason_code)

    def _on_connect_fail(self, client, _userdata):
        if client is not self.client:
            return
        self.connected = False
        err = ("mqtt_unreachable", "")
        if self.error != err:
            log.warning("MQTT: Broker nicht erreichbar (%s:%s)", self.cfg["mqtt_host"], self.cfg["mqtt_port"])
        self.error = err
