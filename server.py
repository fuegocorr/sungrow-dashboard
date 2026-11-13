"""Sungrow-SH15T-Dashboard: Modbus-TCP-Poller + FastAPI-Webserver.

Start:  python server.py            (lauscht auf 0.0.0.0:8080)
        python server.py --port 9000

Es wird ausschließlich gelesen (FC 04, Input-Register). Der Server enthält
keinen Code für Schreibzugriffe.
"""

import argparse
import asyncio
import inspect
import ipaddress
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from mqtt_ha import MqttPublisher
from registers import DEVICES, build_values, decode

BASE = Path(__file__).resolve().parent
# Pfad per Umgebungsvariable überschreibbar (z. B. für Tests)
SETTINGS_FILE = Path(os.environ.get("SUNGROW_SETTINGS") or BASE / "settings.json")
INDEX_FILE = BASE / "static" / "index.html"

PAUSE = 0.02  # 20 ms Pause zwischen zwei Abfragen
TIMEOUT = 3  # Sekunden pro Modbus-Anfrage
SETTLE = 0.5  # Wartezeit nach dem Trennen, bis das Gerät den Client-Platz freigibt
MQTT_DEFAULTS = {
    "mqtt_enabled": False,
    "mqtt_host": "",
    "mqtt_port": 1883,
    "mqtt_user": "",
    "mqtt_password": "",
    "mqtt_prefix": "sungrow",
    "mqtt_language": "de",  # Sprache der Sensornamen in Home Assistant
}
DEFAULTS = {
    "host": "", "port": 502, "device_type": "winet", "unit_id": 1, "interval": 10, **MQTT_DEFAULTS
}

log = logging.getLogger("sungrow")
# Verbindungsfehler meldet der Poller selbst (einmal pro Fehlerursache)
logging.getLogger("pymodbus").setLevel(logging.CRITICAL)

# pymodbus hat den Parameter für die Unit-ID mehrfach umbenannt
_params = inspect.signature(AsyncModbusTcpClient.read_input_registers).parameters
UNIT_KW = next((k for k in ("device_id", "slave", "unit") if k in _params), "slave")


# --- Meldungen (Deutsch / Englisch) -----------------------------------------
# Die Oberfläche fragt mit ?lang=de|en an; Logs bleiben deutsch.

LANGS = ("de", "en")
MESSAGES = {
    "body": ("JSON-Objekt erwartet", "JSON object expected"),
    "host": ("Gültige IPv4-Adresse oder Hostname erforderlich",
             "Valid IPv4 address or hostname required"),
    "device_type": ("Gerätetyp muss 'winet', 'ihm' oder 'combined' sein",
                    "Device type must be 'winet', 'ihm' or 'combined'"),
    "int_range": ("{name} muss eine ganze Zahl von {lo} bis {hi} sein",
                  "{name} must be an integer from {lo} to {hi}"),
    "bool": ("true oder false erwartet", "true or false expected"),
    "text200": ("Text mit höchstens 200 Zeichen erwartet", "Text of at most 200 characters expected"),
    "mqtt_host": ("Gültige IPv4-Adresse oder Hostname des Brokers erforderlich",
                  "Valid IPv4 address or hostname of the broker required"),
    "mqtt_prefix": ("Nur Buchstaben, Ziffern, _ und - (höchstens 40 Zeichen)",
                    "Letters, digits, _ and - only (at most 40 characters)"),
    "mqtt_language": ("Sprache muss 'de' oder 'en' sein", "Language must be 'de' or 'en'"),
    "not_writable": ("settings.json nicht schreibbar: {detail}", "settings.json is not writable: {detail}"),
    "connect_failed": ("Verbindung zu {host}:{port} fehlgeschlagen", "Connection to {host}:{port} failed"),
    "connect_failed_detail": ("Verbindung zu {host}:{port} fehlgeschlagen ({detail})",
                              "Connection to {host}:{port} failed ({detail})"),
    "no_response": ("Keine Antwort auf Register {address} ({detail})",
                    "No response for register {address} ({detail})"),
    "no_register": ("Gerät antwortet auf kein Register (Unit-ID {unit_id} / Gerätetyp prüfen)",
                    "Device answers no register (check unit ID {unit_id} / device type)"),
    "internal": ("Interner Fehler, siehe Server-Log", "Internal error, see server log"),
    "mqtt_refused": ("Broker lehnt ab: {detail}", "Broker refused: {detail}"),
    "mqtt_lost": ("Verbindung getrennt: {detail}", "Connection lost: {detail}"),
    "mqtt_unreachable": ("Broker nicht erreichbar", "Broker not reachable"),
}
# Feldnamen für "int_range"
FIELD_NAMES = {
    "port": ("Port", "Port"),
    "unit_id": ("Unit-ID", "Unit ID"),
    "interval": ("Abfrageintervall", "Polling interval"),
    "mqtt_port": ("MQTT-Port", "MQTT port"),
}


class Msg:
    """Meldung, die erst bei der Ausgabe in die gewünschte Sprache übersetzt wird."""

    def __init__(self, key: str, **params):
        self.key, self.params = key, params

    def text(self, lang: str = "de") -> str:
        i = LANGS.index(lang) if lang in LANGS else 0
        params = dict(self.params)
        if "field" in params:
            params["name"] = FIELD_NAMES[params.pop("field")][i]
        return MESSAGES[self.key][i].format(**params)

    def __eq__(self, other):
        return isinstance(other, Msg) and (self.key, self.params) == (other.key, other.params)

    def __str__(self):
        return self.text("de")

    __repr__ = __str__


def render_errors(errors: dict, lang: str) -> dict:
    return {k: m.text(lang) for k, m in errors.items()}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# --- Einstellungen ----------------------------------------------------------

_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)


def valid_host(host: str) -> bool:
    try:
        ipaddress.IPv4Address(host)
        return True
    except ValueError:
        pass
    # Sieht aus wie eine IP (nur Ziffern und Punkte), ist aber keine gültige
    if re.fullmatch(r"[0-9.]+", host):
        return False
    return bool(_HOSTNAME.match(host))


def _int(data, key, lo, hi, errors):
    val = data.get(key)
    if isinstance(val, bool) or not isinstance(val, int) or not lo <= val <= hi:
        errors[key] = Msg("int_range", field=key, lo=lo, hi=hi)
        return None
    return val


def validate_settings(data) -> tuple:
    """Gibt (bereinigte Einstellungen, Fehler-Dict mit Msg-Objekten) zurück."""
    errors = {}
    if not isinstance(data, dict):
        return None, {"body": Msg("body")}

    host = data.get("host")
    host = host.strip() if isinstance(host, str) else ""
    if not valid_host(host):
        errors["host"] = Msg("host")

    device_type = data.get("device_type")
    if device_type not in DEVICES:
        errors["device_type"] = Msg("device_type")

    data = dict(data)
    data.setdefault("port", DEFAULTS["port"])
    data.setdefault("interval", DEFAULTS["interval"])
    if data.get("unit_id") is None and device_type in DEVICES:
        data["unit_id"] = DEVICES[device_type]["unit_id"]

    port = _int(data, "port", 1, 65535, errors)
    unit_id = _int(data, "unit_id", 1, 247, errors)
    interval = _int(data, "interval", 2, 3600, errors)
    mqtt = _validate_mqtt(data, errors)
    if errors:
        return None, errors
    return {
        "host": host,
        "port": port,
        "device_type": device_type,
        "unit_id": unit_id,
        "interval": interval,
        **mqtt,
    }, {}


def _validate_mqtt(data: dict, errors: dict) -> dict:
    m = {k: data.get(k, default) for k, default in MQTT_DEFAULTS.items()}
    if not isinstance(m["mqtt_enabled"], bool):
        errors["mqtt_enabled"] = Msg("bool")
    for key in ("mqtt_host", "mqtt_user", "mqtt_password", "mqtt_prefix", "mqtt_language"):
        if not isinstance(m[key], str) or len(m[key]) > 200:
            errors[key] = Msg("text200")
            m[key] = ""
    m["mqtt_host"] = m["mqtt_host"].strip()
    m["mqtt_user"] = m["mqtt_user"].strip()
    m["mqtt_prefix"] = m["mqtt_prefix"].strip() or MQTT_DEFAULTS["mqtt_prefix"]
    m["mqtt_language"] = m["mqtt_language"] or MQTT_DEFAULTS["mqtt_language"]
    if (m["mqtt_enabled"] is True or m["mqtt_host"]) and not valid_host(m["mqtt_host"]):
        errors.setdefault("mqtt_host", Msg("mqtt_host"))
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", m["mqtt_prefix"]):
        errors.setdefault("mqtt_prefix", Msg("mqtt_prefix"))
    _int(m, "mqtt_port", 1, 65535, errors)
    if m["mqtt_language"] not in LANGS:
        errors["mqtt_language"] = Msg("mqtt_language")
    if not m["mqtt_user"]:
        m["mqtt_password"] = ""  # ohne Benutzer kein Passwort
    return m


def public_settings(settings: dict) -> dict:
    """Einstellungen für den Browser: das MQTT-Passwort verlässt den Server nicht."""
    out = {k: v for k, v in settings.items() if k != "mqtt_password"}
    out["mqtt_password_set"] = bool(settings.get("mqtt_password"))
    return out


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        clean, errors = validate_settings(data)
        if clean:
            return clean
        log.warning("settings.json ungültig, wird ignoriert: %s", errors)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        log.warning("settings.json nicht lesbar: %s", exc)
    return dict(DEFAULTS)


def save_settings(settings: dict) -> None:
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # enthält ggf. das MQTT-Passwort
    except OSError:
        pass
    os.replace(tmp, SETTINGS_FILE)


# --- Modbus -----------------------------------------------------------------


class LinkError(Exception):
    """Verbindung zum Gerät ist nicht (mehr) nutzbar."""

    def __init__(self, msg: Msg):
        super().__init__(str(msg))
        self.msg = msg


async def open_client(host: str, port: int) -> AsyncModbusTcpClient:
    # reconnect_delay=0: pymodbus verbindet nicht selbst neu, das macht der Poller
    client = AsyncModbusTcpClient(
        host, port=port, timeout=TIMEOUT, retries=1, reconnect_delay=0
    )
    try:
        ok = await client.connect()
    except Exception as exc:  # DNS-Fehler, Netz nicht erreichbar, ...
        await close_client(client)
        raise LinkError(Msg("connect_failed_detail", host=host, port=port, detail=str(exc))) from exc
    if not ok:
        await close_client(client)
        raise LinkError(Msg("connect_failed", host=host, port=port))
    return client


async def close_client(client) -> None:
    if client is None:
        return
    try:
        res = client.close()
        if inspect.isawaitable(res):
            await res
    except Exception:
        pass


async def read_words(client, address: int, count: int, unit_id: int):
    """Liest Input-Register (FC 04). None = Gerät lehnt das Register ab."""
    try:
        rr = await client.read_input_registers(address, count=count, **{UNIT_KW: unit_id})
    except (ModbusException, asyncio.TimeoutError, OSError) as exc:
        raise LinkError(Msg("no_response", address=address, detail=str(exc))) from exc
    finally:
        await asyncio.sleep(PAUSE)
    if rr.isError() or len(getattr(rr, "registers", [])) < count:
        return None
    return rr.registers


async def read_registers(client, device_type: str, unit_id: int) -> tuple:
    """Liest alle Register der Map. Gibt (dekodiert, Details) zurück."""
    regs = DEVICES[device_type]["registers"]
    blocks, decoded, details = {}, {}, []
    answered = 0
    for reg in regs:
        unit = reg.unit_id or unit_id
        if reg.block:
            bkey = (reg.block, unit)
            if bkey not in blocks:
                blocks[bkey] = await read_words(client, reg.block[0], reg.block[1], unit)
            data = blocks[bkey]
            off = reg.address - reg.block[0]
            words = None if data is None else data[off : off + reg.count]
        else:
            words = await read_words(client, reg.address, reg.count, unit)
        value = None
        if words is not None:
            answered += 1
            value = decode(reg, words)
        decoded[reg.key] = value
        details.append(
            {
                "key": reg.key,
                "label": reg.label,
                "address": reg.address,
                "doc_register": reg.address + 1,
                "unit_id": unit,
                "raw": words,
                "value": value,
                "unit": reg.unit,
            }
        )
    if answered == 0:
        raise LinkError(Msg("no_register", unit_id=unit_id))
    return decoded, details


class Poller:
    """Hält EINE dauerhafte Modbus-Verbindung und pollt im Hintergrund."""

    def __init__(self):
        self.settings = load_settings()
        self.client = None
        self.closed_at = 0.0
        self.lock = asyncio.Lock()  # schützt die eine Verbindung zum Gerät
        self.wake = asyncio.Event()
        self.connected = False
        self.error = None
        self.last_success = None
        self.result = build_values(self.settings["device_type"], {})
        self.mqtt = MqttPublisher()

    def snapshot(self, lang: str = "de") -> dict:
        s = self.settings
        mqtt = self.mqtt.status()
        if mqtt["error"]:
            key, detail = mqtt["error"]
            mqtt["error"] = Msg(key, detail=detail).text(lang)
        return {
            "timestamp": now_iso(),
            "configured": bool(s["host"]),
            "connected": self.connected,
            "error": self.error.text(lang) if self.error else None,
            "last_success": self.last_success,
            "host": s["host"],
            "port": s["port"],
            "device_type": s["device_type"],
            "device_label": DEVICES[s["device_type"]]["label"],
            "unit_id": s["unit_id"],
            "interval": s["interval"],
            "mqtt": mqtt,
            **self.result,
        }

    async def _drop(self, error=None):
        if self.client is not None:
            await close_client(self.client)
            self.closed_at = time.monotonic()
        self.client = None
        self.connected = False
        self.error = error
        self.result = build_values(self.settings["device_type"], {})

    async def _open(self, host, port):
        """Verbinden; vorher abwarten, bis das Gerät die alte Verbindung freigegeben hat."""
        wait = self.closed_at + SETTLE - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        return await open_client(host, port)

    async def apply(self, settings: dict):
        """Neue Einstellungen übernehmen: alte Verbindung trennen, neu verbinden."""
        async with self.lock:
            self.settings = settings
            self.last_success = None
            await self._drop()
        await asyncio.to_thread(self.mqtt.configure, settings)
        self.wake.set()

    async def test(self, settings: dict) -> dict:
        """Verbindungstest. Trennt vorher die Poller-Verbindung, weil Sungrow
        nur einen Client gleichzeitig verträgt; der Poller verbindet danach neu."""
        async with self.lock:
            await self._drop()
            client = None
            try:
                client = await self._open(settings["host"], settings["port"])
                decoded, details = await read_registers(
                    client, settings["device_type"], settings["unit_id"]
                )
                return {
                    "ok": True,
                    "registers": details,
                    **build_values(settings["device_type"], decoded),
                }
            except LinkError as exc:
                return {"ok": False, "error": exc.msg}
            finally:
                if client is not None:
                    await close_client(client)
                    self.closed_at = time.monotonic()
                self.wake.set()

    async def _cycle(self):
        s = self.settings
        async with self.lock:
            if s is not self.settings:  # währenddessen geändert
                return
            try:
                if self.client is None or not self.client.connected:
                    await self._drop(self.error)
                    self.client = await self._open(s["host"], s["port"])
                    log.info("Verbunden mit %s:%s", s["host"], s["port"])
                decoded, _ = await read_registers(self.client, s["device_type"], s["unit_id"])
                self.result = build_values(s["device_type"], decoded)
                self.connected = True
                self.error = None
                self.last_success = now_iso()
            except LinkError as exc:
                if self.error != exc.msg:
                    log.warning("%s", exc)
                await self._drop(exc.msg)

    async def run(self):
        await asyncio.to_thread(self.mqtt.configure, self.settings)
        while True:
            self.wake.clear()
            delay = None  # ohne Konfiguration: warten, bis gespeichert wird
            if self.settings["host"]:
                try:
                    await self._cycle()
                except Exception:  # Poller darf nie sterben
                    log.exception("Unerwarteter Fehler im Poller")
                    await self._drop(Msg("internal"))
                self.mqtt.publish(self.snapshot())
                delay = self.settings["interval"]
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def stop(self):
        async with self.lock:
            await self._drop()
        await asyncio.to_thread(self.mqtt.stop)


# --- Web --------------------------------------------------------------------

poller: Poller = None


@asynccontextmanager
async def lifespan(_app):
    global poller
    poller = Poller()
    task = asyncio.create_task(poller.run())
    yield
    task.cancel()
    await poller.stop()


app = FastAPI(
    title="Sungrow Dashboard", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
)


@app.middleware("http")
async def local_only(request: Request, call_next):
    """Nur Clients aus dem lokalen Netz zulassen (privat, link-local, loopback)."""
    try:
        ip = ipaddress.ip_address(request.client.host)
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        allowed = ip.is_private or ip.is_loopback or ip.is_link_local
    except (ValueError, AttributeError):
        allowed = False
    if not allowed:
        return JSONResponse(
            {"error": "Nur im lokalen Netz erreichbar / Local network only"}, status_code=403
        )
    return await call_next(request)


def _lang(request: Request) -> str:
    lang = request.query_params.get("lang", "de")
    return lang if lang in LANGS else "de"


async def _body(request: Request):
    try:
        return await request.json()
    except ValueError:
        return None


@app.get("/")
async def index():
    return FileResponse(INDEX_FILE, headers={"Cache-Control": "no-cache"})


@app.get("/api/values")
async def api_values(request: Request):
    return JSONResponse(poller.snapshot(_lang(request)), headers={"Cache-Control": "no-store"})


@app.get("/api/settings")
async def api_get_settings():
    return {
        "settings": public_settings(poller.settings),
        "configured": bool(poller.settings["host"]),
        "device_types": {k: {"label": d["label"], "unit_id": d["unit_id"]} for k, d in DEVICES.items()},
    }


@app.post("/api/settings")
async def api_post_settings(request: Request):
    body = await _body(request)
    if isinstance(body, dict) and not body.get("mqtt_password"):
        # leer/fehlend = gespeichertes Passwort behalten
        body = {**body, "mqtt_password": poller.settings.get("mqtt_password", "")}
    lang = _lang(request)
    settings, errors = validate_settings(body)
    if errors:
        return JSONResponse({"ok": False, "errors": render_errors(errors, lang)}, status_code=422)
    try:
        save_settings(settings)
    except OSError as exc:
        return JSONResponse(
            {"ok": False, "errors": {"body": Msg("not_writable", detail=str(exc)).text(lang)}},
            status_code=500,
        )
    await poller.apply(settings)
    return {"ok": True, "settings": public_settings(settings)}


@app.post("/api/test")
async def api_test(request: Request):
    lang = _lang(request)
    settings, errors = validate_settings(await _body(request))
    if errors:
        return JSONResponse({"ok": False, "errors": render_errors(errors, lang)}, status_code=422)
    result = await poller.test(settings)
    if not result["ok"]:
        result["error"] = result["error"].text(lang)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sungrow-Dashboard")
    parser.add_argument("--host", default="0.0.0.0", help="Bind-Adresse (Standard 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    uvicorn.run(app, host=args.host, port=args.port)
