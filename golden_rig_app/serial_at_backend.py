"""RTL8762 Golden Controller AT backend for Golden Rig Protocol v1.

One permanently configured Golden Controller COM port per physical jig slot.
No COM-enumeration-order binding. No legacy unframed TCP messages.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from golden_rig_server import Result, mac

LOG = logging.getLogger('golden_rig.serial')
PEER = re.compile(r'\+CONNECTED\s*>>\s*\d+\s*:\s*([0-9a-fA-F:-]{12,17})', re.I)
DISCONNECTED = re.compile(r'\+DISCONNECTED\s*>>', re.I)
RSSI = re.compile(r'\+RSSI\s*[:=]\s*(-?\d+)', re.I)


def reverse_mac(address: str) -> str:
    parts = mac(address).split(':')
    return ''.join(reversed(parts))


@dataclass(frozen=True)
class SerialSlot:
    slot_number: int
    port: str
    usb_serial: str | None = None


def load_slots(config_path: str) -> dict[int, SerialSlot]:
    data = json.loads(Path(config_path).read_text(encoding='utf-8'))
    entries = data['slots']
    if type(entries) is not list or len(entries) not in (4, 8):
        raise ValueError('Configuration must bind exactly 4 or 8 Golden Controller slots')
    result: dict[int, SerialSlot] = {}
    ports: set[str] = set()
    serials: set[str] = set()
    for x in entries:
        slot = x['slot_number']
        port = x['port'].strip()
        serial = x.get('usb_serial') or None
        if type(slot) is not int or not 1 <= slot <= len(entries) or not port:
            raise ValueError('Invalid slot or port')
        if slot in result or port.casefold() in ports or (serial and serial in serials):
            raise ValueError('Duplicate slot, COM port, or USB serial')
        result[slot] = SerialSlot(slot, port, serial)
        ports.add(port.casefold())
        if serial:
            serials.add(serial)
    if set(result) != set(range(1, len(entries) + 1)):
        raise ValueError('Physical slot numbers must be contiguous from 1')
    return result


def validate_ports(slots: dict[int, SerialSlot]) -> None:
    """Fail closed if configured ports/USB serial identities have drifted."""
    from serial.tools.list_ports import comports
    available = {p.device.casefold(): p for p in comports()}
    for x in slots.values():
        port_info = available.get(x.port.casefold())
        if port_info is None:
            raise RuntimeError(f'Slot {x.slot_number}: {x.port} unavailable')
        if x.usb_serial and port_info.serial_number != x.usb_serial:
            raise RuntimeError(f'Slot {x.slot_number}: USB controller identity mismatch')


def read_until(ser, expires: float, matcher):
    while time.monotonic() < expires:
        line = ser.readline().decode('utf-8', errors='replace').strip()
        if line:
            LOG.debug('%s <- %s', ser.port, line)
            outcome = matcher(line)
            if outcome is not None:
                return outcome
    return None


def test_slot(slot: SerialSlot, target: dict, deadline: float, serial_factory=None) -> Result:
    """Blocking worker. Each worker owns its sole serial connection."""
    if serial_factory is None:
        import serial
        serial_factory = serial.Serial
    expected = mac(target['expected_mac'])
    result = Result(target['slot_number'], target['expected_mac'])
    if deadline - time.monotonic() < 9:
        result.error_code = 'TEST_INCOMPLETE'
        result.detail = 'Insufficient RF test time remaining'
        return result
    try:
        ser = serial_factory(slot.port, 115200, timeout=0.1, write_timeout=1)
    except Exception as exc:
        # Port open failure is rig infrastructure, not a defective DUT.
        result.error_code = 'ADAPTER_UNAVAILABLE'
        result.detail = f'{slot.port}: {exc}'[:180]
        return result
    try:
        def issue(cmd: str) -> None:
            LOG.debug('%s -> %s', slot.port, cmd)
            ser.write((cmd + '\r\n').encode('ascii'))
        ser.reset_input_buffer()
        issue('AT+CONN=' + reverse_mac(expected))
        connect_until = min(deadline - 5, time.monotonic() + 5)
        connected_line = read_until(ser, connect_until, lambda line:
            ('FAIL', None) if ('FAIL' in line.upper() or 'CONNECT TIME OUT' in line.upper())
            else ('CONNECTED', line) if '+CONNECTED' in line.upper() else None)
        if connected_line is None or connected_line[0] == 'FAIL':
            result.error_code = 'CONNECT_FAILED'
            result.detail = 'No confirmed connection' if connected_line is None else 'Controller reported connect failure'
            return result
        result.connected = True
        m = PEER.search(connected_line[1])
        if m:
            try:
                result.reported_mac = mac(m.group(1))
            except ValueError:
                pass
        if result.reported_mac is None:
            result.error_code = 'IDENTITY_UNCERTAIN'
            result.detail = 'Connected event did not contain a verifiable peer MAC'
        elif result.reported_mac != expected:
            result.error_code = 'ADDRESS_MISMATCH'
            result.detail = f'Connected peer {result.reported_mac} does not match expected MAC'
        # Always attempt clean disconnect when a link was established, even on mismatch.
        try:
            issue('AT+SYSMODE=AT')
            mode = read_until(ser, min(deadline - 4, time.monotonic() + 1.5),
                              lambda line: True if line.strip() == 'OK' else None)
            if mode:
                issue('AT+RSSI')
                rssi = read_until(ser, min(deadline - 3.5, time.monotonic() + 1.5),
                                  lambda line: RSSI.search(line).group(1) if RSSI.search(line) else None)
                if rssi is not None:
                    # Protocol v1 has no dedicated RSSI field; retain in detail.
                    result.detail = (result.detail + '; ' if result.detail else '') + f'RSSI={rssi} dBm'
            issue('AT+DISC=' + reverse_mac(expected))
            def disconnect_match(line):
                if DISCONNECTED.search(line):
                    return 'YES'
                if 'DISCONNECT TIME OUT' in line.upper():
                    return 'NO'
                return None
            disc = read_until(ser, min(deadline - .5, time.monotonic() + 2.5), disconnect_match)
            result.disconnected = disc == 'YES'
            if not result.disconnected and result.error_code is None:
                result.error_code = 'DISCONNECT_FAILED'
                result.detail = (result.detail + '; ' if result.detail else '') + 'Disconnect not confirmed'
        finally:
            try:
                issue('AT+RESET')
            except Exception:
                LOG.warning('Reset failed on %s', slot.port, exc_info=True)
        return result
    except Exception as exc:
        LOG.exception('AT cycle failed for slot %s', slot.slot_number)
        if result.connected:
            result.error_code = result.error_code or 'TEST_INCOMPLETE'
        else:
            result.error_code = 'ADAPTER_ERROR'
        result.detail = (result.detail + '; ' if result.detail else '') + str(exc)[:120]
        return result
    finally:
        try:
            ser.close()
        except Exception:
            pass


class SerialAtTester:
    def __init__(self, slots: dict[int, SerialSlot], serial_factory=None):
        self.slots = slots
        self.serial_factory = serial_factory

    async def run(self, targets: list[dict], deadline: float) -> list[Result]:
        # Verify ALL configured ports before touching any DUT.
        # Mock serial_factory skips physical enumeration for unit tests.
        if self.serial_factory is None:
            try:
                validate_ports(self.slots)
            except Exception as exc:
                return [Result(t['slot_number'], t['expected_mac'], error_code='ADAPTER_UNAVAILABLE', detail=str(exc)[:180]) for t in targets]
        for t in targets:
            if t['slot_number'] not in self.slots:
                return [Result(x['slot_number'], x['expected_mac'], error_code='RIG_INTERNAL_ERROR', detail='Slot not configured') for x in targets]
        tasks = [asyncio.to_thread(test_slot, self.slots[t['slot_number']], t, deadline, self.serial_factory) for t in targets]
        return list(await asyncio.gather(*tasks))
