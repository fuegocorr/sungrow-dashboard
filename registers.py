"""Registermaps für das Sungrow-Dashboard.

Alle Adressen sind Modbus-Adressen (0-basiert = Sungrow-Dokunummer - 1) und
wurden unverändert aus den folgenden Dateien übernommen:

  SH-Map:  github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant
           modbus_sungrow.yaml
  iHM-Map: github.com/evcc-io/evcc
           templates/definition/meter/sungrow-ihm.yaml

Alle Messwerte sind Input-Register (FC 04). 32-Bit-Werte haben vertauschte
Wortreihenfolge (niederwertiges Wort zuerst; "swap: word" bzw. "int32s").

Vorzeichen im Dashboard (einheitlich für beide Maps):
  battery_power  > 0 = Entladen,  < 0 = Laden
  grid_power     > 0 = Netzbezug, < 0 = Einspeisung
"""

from dataclasses import dataclass, replace
from typing import Optional, Tuple


@dataclass(frozen=True)
class Reg:
    key: str
    label: str
    address: int
    dtype: str  # "uint16" | "uint32" | "int32"  (32 Bit immer wortvertauscht)
    scale: float = 1.0
    unit: str = ""
    # Optionaler Blockzugriff (Startadresse, Anzahl), wie in der Quelle vorgegeben
    block: Optional[Tuple[int, int]] = None
    # Feste Unit-ID (nur im kombinierten Modus); None = Unit-ID aus den Einstellungen
    unit_id: Optional[int] = None

    @property
    def count(self) -> int:
        return 1 if self.dtype == "uint16" else 2


# --- SH-Map (WiNet-S / Wechselrichter direkt, Unit-ID 1) --------------------
SH_REGISTERS = [
    # "Sungrow device type code", address 4999 (reg 5000), uint16
    Reg("device_type_code", "Gerätetyp-Code", 4999, "uint16"),
    # "Total DC power", address 5016 (reg 5017), uint32, swap: word, W
    Reg("pv_power", "PV-Leistung", 5016, "uint32", 1, "W"),
    # "Battery power", address 5213 (reg 5214-5215), int32, swap: word, W
    # laut Quelle: positiv beim Entladen, negativ beim Laden
    Reg("battery_power", "Batterie-Leistung", 5213, "int32", 1, "W"),
    # "Running state raw", address 12999 (reg 13000), uint16
    Reg("status_code", "Status-Code", 12999, "uint16"),
    # "Daily PV generation", address 13001 (reg 13002), uint16, scale 0.1, kWh
    Reg("daily_yield", "Tagesertrag", 13001, "uint16", 0.1, "kWh"),
    # "Load power", address 13007 (reg 13008), int32, swap: word, nan 0x7FFFFFFF, W
    Reg("load_power", "Hausverbrauch", 13007, "int32", 1, "W"),
    # "Export power raw", address 13009 (reg 13010), int32, swap: word,
    # nan 0x7FFFFFFF, W - laut Quelle: positiv = Einspeisung, negativ = Bezug
    Reg("export_power", "Einspeiseleistung (roh)", 13009, "int32", 1, "W"),
    # "Battery level", address 13022 (reg 13023), uint16, scale 0.1, %
    Reg("battery_soc", "Batterie-SOC", 13022, "uint16", 0.1, "%"),
]

# --- iHM-Map (iHomeManager, Unit-ID 247) ------------------------------------
# evcc liest Register 8155-8163 als einen Block (register: 8154, count: 9).
_IHM_BLOCK = (8154, 9)
IHM_REGISTERS = [
    # usage pv: address 8154 (Register 8155, Total active power), int32s, scale 10
    # Am echten Gerät gemessen: das ist die AC-Ausgangsleistung des Hybrid-
    # Wechselrichters (PV + Batterie-Entladung - Verluste), nicht die PV-Leistung.
    Reg("ihm_ac_power", "WR-Ausgangsleistung (AC)", 8154, "int32", 10, "W", _IHM_BLOCK),
    # usage grid: address 8156 (Register 8157, Total active power), int32s, scale 10
    # evcc-Netzzähler: positiv = Bezug
    Reg("ihm_grid_power", "Netzleistung", 8156, "int32", 10, "W", _IHM_BLOCK),
    # usage battery: address 8160 (Register 8161, Battery power), int32s, scale 10
    # positiv = Entladen (am echten Gerät gegen SH-Register 5214 geprüft)
    Reg("ihm_battery_power", "Batterie-Leistung", 8160, "int32", 10, "W", _IHM_BLOCK),
    # usage battery: address 8162 (Register 8163, State of charge), uint16, scale 0.1
    Reg("ihm_battery_soc", "Batterie-SOC", 8162, "uint16", 0.1, "%", _IHM_BLOCK),
]

# --- Kombiniert: beide Maps über EINE Verbindung zum iHomeManager -----------
# Der iHM reicht Unit-ID 1 an den Wechselrichter durch. Netz- und Hausverbrauchs-
# register des Wechselrichters liefern dabei 0 (der Zähler hängt am iHM) und
# werden deshalb nicht gelesen; der Gerätetyp-Code (5000) kommt als 0xFFFF.
INVERTER_UNIT_ID = 1
_SKIP_COMBINED = {"device_type_code", "load_power", "export_power"}
COMBINED_REGISTERS = [
    replace(r, unit_id=INVERTER_UNIT_ID) for r in SH_REGISTERS if r.key not in _SKIP_COMBINED
] + IHM_REGISTERS

# "Running state raw" -> Text (Template "Sungrow inverter state" der SH-Map)
RUNNING_STATES = {
    0x0000: "Running",
    0x0001: "Stop",
    0x0002: "Key stop",
    0x0004: "Emergency Stop",
    0x0008: "Standby",
    0x0020: "Starting",
    0x0010: "Initial standby",
    0x0014: "Microgrid Operation",
    0x0040: "Running",
    0x0041: "Off-grid Charge",
    0x0080: "Derating Running",
    0x0100: "Fault",
    0x0200: "Update Failed",
    0x0400: "Running in maintain mode",
    0x0800: "Running in compulsory (forced) mode",
    0x1000: "Running (off-grid)",
    0x1111: "Uninitialized",
    0x1200: "Initial standby",
    0x1300: "Key stop",
    0x1400: "Standby",
    0x1500: "Emergency Stop",
    0x1600: "Starting",
    0x1700: "AFCI self-test shutdown",
    0x1800: "Intelligent Station Building Status",
    0x1900: "Safe Mode",
    0x2000: "Open loop",
    0x2500: "Communicate fault",
    0x2501: "Restarting",
    0x4000: "Running in External EMS mode",
    0x4001: "Emergency Charging Operation",
    0x8000: "Stop",
    0x5500: "Fault",
    0x8100: "Derating Running",
    0x8200: "Dispatch Running",
    0x9100: "Warn Running",
}

# "Sungrow device type code" -> Modell (Auszug T-Serie aus der SH-Map)
DEVICE_TYPES = {
    0x0E20: "SH5T",
    0x0E21: "SH6T",
    0x0E22: "SH8T",
    0x0E23: "SH10T",
    0x0E24: "SH12T",
    0x0E25: "SH15T",
    0x0E26: "SH20T",
    0x0E28: "SH25T",
}

DEVICES = {
    "winet": {
        "label": "WiNet-S / Wechselrichter",
        "unit_id": 1,
        "registers": SH_REGISTERS,
    },
    "ihm": {
        "label": "iHomeManager",
        "unit_id": 247,
        "registers": IHM_REGISTERS,
    },
    "combined": {
        "label": "iHomeManager + Wechselrichter (kombiniert)",
        "unit_id": 247,
        "registers": COMBINED_REGISTERS,
    },
}

# Werte, die das Dashboard anzeigt
VALUE_KEYS = [
    "pv_power",
    "daily_yield",
    "battery_soc",
    "battery_power",
    "grid_power",
    "load_power",
    "status",
]


def decode(reg: Reg, words) -> Optional[float]:
    """Rohregister -> skalierter Wert. "Nicht verfügbar" wird zu None."""
    if reg.dtype == "uint16":
        raw = words[0]
        if raw == 0xFFFF:
            return None
    else:
        raw = (words[1] << 16) | words[0]  # vertauschte Wortreihenfolge
        if raw in (0x7FFFFFFF, 0xFFFFFFFF):
            return None
        if reg.dtype == "int32" and raw & 0x80000000:
            raw -= 1 << 32
    value = raw * reg.scale
    return round(value, 1) if reg.scale < 1 else int(value)


def build_values(device_type: str, raw: dict) -> dict:
    """Aus den dekodierten Registern die Dashboard-Werte ableiten."""
    v = {k: None for k in VALUE_KEYS}
    derived, unsupported = [], []
    for k in ("pv_power", "daily_yield", "battery_soc", "battery_power", "load_power"):
        v[k] = raw.get(k)

    ac = raw.get("ihm_ac_power")
    if device_type in ("ihm", "combined"):
        grid = raw.get("ihm_grid_power")
        v["grid_power"] = grid
        if ac is not None and grid is not None:
            # Hausverbrauch = AC-Ausgang des Wechselrichters + Netzbezug
            v["load_power"] = ac + grid
            derived.append("load_power")

    if device_type == "combined":
        # PV, Tagesertrag, Status, SOC und Batterie vom Wechselrichter (feinere
        # Auflösung); fällt er aus, SOC/Batterie ersatzweise vom iHM.
        for k in ("battery_soc", "battery_power"):
            if v[k] is None:
                v[k] = raw.get("ihm_" + k)
        code = raw.get("status_code")
        if code is not None:
            v["status"] = RUNNING_STATES.get(code, f"Unbekannt (0x{code:04X})")
        v["status_code"] = code
    elif device_type == "winet":
        exp = raw.get("export_power")
        v["grid_power"] = None if exp is None else -exp
        code = raw.get("status_code")
        if code is not None:
            v["status"] = RUNNING_STATES.get(code, f"Unbekannt (0x{code:04X})")
        v["status_code"] = code
        tcode = raw.get("device_type_code")
        v["model"] = None if tcode is None else DEVICE_TYPES.get(tcode, f"0x{tcode:04X}")
    else:
        # Die iHM-Map enthält weder Tagesertrag noch Status noch PV-Leistung.
        unsupported += ["daily_yield", "status"]
        v["battery_soc"] = raw.get("ihm_battery_soc")
        v["battery_power"] = raw.get("ihm_battery_power")
        if ac is not None and v["battery_power"] is not None:
            # Näherung ohne Wandlungsverluste: AC-Ausgang - Batterie-Entladung
            v["pv_power"] = max(0, ac - v["battery_power"])
            derived.append("pv_power")
    return {"values": v, "derived": derived, "unsupported": unsupported}
