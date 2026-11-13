"""Minimaler Modbus-TCP-Simulator zum Testen ohne Wechselrichter.

Beantwortet nur FC 04 (Input-Register) und nur EINEN Client gleichzeitig,
wie das echte Gerät. Enthält Beispielwerte für beide Registermaps.

Start:  python tools/sim_modbus.py [--port 5020]
"""

import argparse
import asyncio
import struct


def u32s(value: int):
    """32 Bit mit vertauschter Wortreihenfolge (niederwertiges Wort zuerst)."""
    value &= 0xFFFFFFFF
    return [value & 0xFFFF, value >> 16]


REGS = {}


def put(address, words):
    for i, w in enumerate(words):
        REGS[address + i] = w


# SH-Map (Unit-ID 1)
put(4999, [0x0E25])          # Gerätetyp SH15T
put(5016, u32s(6420))        # PV-Leistung 6420 W
put(5213, u32s(-1850))       # Batterie lädt mit 1850 W
put(12999, [0x0040])         # Running
put(13001, [237])            # Tagesertrag 23.7 kWh
put(13007, u32s(0x7FFFFFFF)) # Hausverbrauch "nicht verfügbar"
put(13009, u32s(3120))       # Einspeisung 3120 W
put(13022, [684])            # SOC 68.4 %

# iHM-Map (Unit-ID 247), Block 8154..8162
put(8154, u32s(642))         # PV 6420 W
put(8156, u32s(-312))        # Einspeisung 3120 W
put(8158, u32s(0))
put(8160, u32s(-185))        # Batterie lädt mit 1850 W
put(8162, [684])             # SOC 68.4 %

busy = False


async def handle(reader, writer):
    global busy
    if busy:  # nur ein Client gleichzeitig
        writer.close()
        return
    busy = True
    try:
        while True:
            head = await reader.readexactly(7)
            tid, _pid, length, unit = struct.unpack(">HHHB", head)
            pdu = await reader.readexactly(length - 1)
            fc = pdu[0]
            if fc != 4:
                resp = bytes([fc | 0x80, 1])  # Illegal Function
            else:
                addr, count = struct.unpack(">HH", pdu[1:5])
                if any(a not in REGS for a in range(addr, addr + count)):
                    resp = bytes([fc | 0x80, 2])  # Illegal Data Address
                else:
                    words = [REGS[a] for a in range(addr, addr + count)]
                    resp = bytes([fc, count * 2]) + struct.pack(f">{count}H", *words)
            writer.write(struct.pack(">HHHB", tid, 0, len(resp) + 1, unit) + resp)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        busy = False
        writer.close()


async def main(port):
    server = await asyncio.start_server(handle, "127.0.0.1", port)
    print(f"Modbus-Simulator auf 127.0.0.1:{port}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=5020)
    asyncio.run(main(p.parse_args().port))
