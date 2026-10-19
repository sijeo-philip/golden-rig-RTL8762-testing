# Golden Rig Application — Build Specification (Protocol v1)

This document specifies the application that runs on the **Golden Module Test Jig ("Golden Rig")**. It covers what the rig must do, the exact network protocol the Station Application speaks, how the station judges each answer, and how to deploy and test the rig.

The station side already exists and is the reference for every rule here:

| Station component | File |
|---|---|
| Message format (strict JSON) | `src/stationapp/infrastructure/golden_rig/codec.py` |
| Framing and TLS transport | `src/stationapp/infrastructure/golden_rig/transport.py`, `secure_transport.py` |
| Result classification | `src/stationapp/domain/golden_rig.py` (`GoldenRigSlotResult.outcome`) |
| Error-code handling | `src/stationapp/services/golden_rig_service.py`, `golden_rig_result_processor.py` |
| Binding / identity probe | `src/stationapp/services/golden_rig_configuration.py` |

Where this document and the station code disagree, the station code wins. Report the discrepancy so the document can be fixed.

---

## 1. Role in the production workflow

The rig performs **README step 13, Stock RF Test**. After Stock programming and MAC readback, a station sends the rig the list of slots that passed, each with its expected Stock MAC. For every target, the rig must:

1. Connect to it over Bluetooth LE.
2. Confirm the connection was established with **that** device.
3. Disconnect cleanly.
4. Report, for each slot, what it actually observed.

The rig reports **observations**, not verdicts. The station decides PASS, FAIL or HOLD from them (section 6). The rig never sees the Pricol MAC; the Pricol stages are verified by MAC readback and the UART functional test on the station.

```text
Station                                   Golden Rig                        DUTs (in jig)
   │  TLS connect (cert pinned)               │                                  │
   │─────────────────────────────────────────▶│                                  │
   │  RF_TEST_REQUEST (≤8 slots + MACs)       │                                  │
   │─────────────────────────────────────────▶│  scan for expected addresses     │
   │                                          │─────────────────────────────────▶│
   │                                          │  connect / verify / disconnect   │
   │                                          │◀────────────────────────────────▶│
   │  RF_TEST_RESPONSE (one result per slot)  │                                  │
   │◀─────────────────────────────────────────│                                  │
   │  connection closed                       │                                  │
```

---

## 2. Platform requirements

The protocol is platform-neutral. The reference implementation (section 10) targets:

- **Hardware:** a Windows 10/11 or Linux PC (Raspberry Pi 4/5 is fine), or an embedded controller with a TCP/IP stack and TLS.
- **Bluetooth:** a BLE 4.2+ adapter positioned within reliable range of all 8 jig positions. One adapter is enough; see section 8 on concurrency.
- **Network:** wired Ethernet on the production LAN. Stations bind to the rig by **mDNS name** (recommended, e.g. `golden-rig-01.local`), DNS name or IP address, plus port. The rig must answer mDNS queries for its name (Avahi on Linux; built in on Windows 10+ and macOS), including one-shot queries sent from a non-5353 source port (RFC 6762 section 6.7), because stations fall back to such a query when the PC cannot resolve `.local` names itself. With an mDNS name, the rig's IP may change (DHCP) without re-binding.
- **Clock:** time synchronized by NTP, so logs line up with station records.
- **Software (reference):** Python 3.11+, the standard-library `asyncio` and `ssl` modules, and [`bleak`](https://github.com/hbldh/bleak) 0.22+ for BLE.

---

## 3. Network and identity

### 3.1 Listener

| Item | Requirement |
|---|---|
| Transport | TCP, server role |
| Default port | **8762** (stations may be configured for another port) |
| Security | TLS 1.2 or newer, server certificate required. Client certificates are not used in v1. |
| Messages per connection | **Exactly one request and one response.** The station closes the connection after reading the response. |

### 3.2 Certificate — this *is* the rig's identity

The station trusts the rig only if all of the following hold:

1. The certificate chains to the station's **Golden Rig CA** (`GOLDEN_RIG_CA_CERTIFICATE` on the station).
2. The certificate's **Subject Alternative Name** contains the exact IP address or DNS name the station connects to. Hostname checking is enabled.
3. The certificate's **Common Name (CN) is the rig ID**, for example `GOLDEN-RIG-01`. The same value must appear in every response's `rig_id`.
4. Its SHA-256 fingerprint (taken over the DER-encoded certificate) equals the fingerprint a Supervisor approved on the station's *Golden Rig binding* screen. The station pins this exact certificate.

Consequences:

- **Replacing or renewing the rig certificate breaks the binding.** A Supervisor must re-approve the rig on every station. Plan renewals; use a long validity period (e.g. 3 years).
- **The certificate must cover the name or address stations use.** Stations connect to the resolved IP but verify the certificate against the name the Supervisor bound, so a rig bound as `golden-rig-01.local` needs `DNS:golden-rig-01.local` in its SAN. When bound by mDNS name, the IP may change freely. Changing the **name** (or binding by an IP not in the SAN) requires a new certificate and re-approval.
- If stations restrict approved rigs with `GOLDEN_RIG_INVENTORY`, the CN must be listed there.

### 3.3 Creating the CA and rig certificate (OpenSSL)

Do this once, on a secured machine; the CA private key must not stay on the rig.

```bash
# 1. Golden Rig CA (keep golden_rig_ca.key offline)
openssl req -new -newkey rsa:3072 -sha256 -nodes \
  -keyout golden_rig_ca.key -out ca.csr -subj "/CN=Golden Rig CA"
cat > ca.ext <<'EOF'
basicConstraints=critical,CA:TRUE,pathlen:0
keyUsage=critical,keyCertSign,cRLSign
subjectKeyIdentifier=hash
EOF
openssl x509 -req -in ca.csr -signkey golden_rig_ca.key -days 3650 -sha256 \
  -extfile ca.ext -out golden_rig_ca.pem

# 2. Rig key + certificate request (CN = rig ID)
openssl req -new -newkey rsa:3072 -sha256 -nodes \
  -keyout rig.key -out rig.csr -subj "/CN=GOLDEN-RIG-01"

# 3. Sign it; SAN must list every address stations use
cat > rig.ext <<'EOF'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
subjectAltName=IP:192.168.10.50,DNS:golden-rig-01.local
EOF
openssl x509 -req -in rig.csr -CA golden_rig_ca.pem -CAkey golden_rig_ca.key \
  -CAcreateserial -days 1095 -sha256 -extfile rig.ext -out rig.pem

# 4. Check, then show the fingerprint the Supervisor will see on the station
openssl verify -CAfile golden_rig_ca.pem rig.pem
openssl x509 -in rig.pem -noout -fingerprint -sha256
```

Use extension files as shown. Do not use `req -x509 -addext` for the CA: on OpenSSL 1.1.1 it adds a second Basic Constraints extension, and the station then rejects every rig certificate with "unable to get local issuer certificate". On Git Bash for Windows, prefix the `-subj` commands with `MSYS_NO_PATHCONV=1`.

Then:
- Install `rig.pem` and `rig.key` on the rig, with the key readable only by the rig service account.
- Distribute `golden_rig_ca.pem` to each station at `config/certificates/golden_rig_ca.pem`.

### 3.4 Handshake-only connections

Stations open TLS connections that **send no request**. This happens when a Supervisor presses *Discover rigs* or *Request identity*, during approval, and at production-session start. The station completes the handshake, reads the certificate, and closes the connection.

The rig must treat a connection that closes, or sends no frame within 10 s of the handshake, as normal. It must not log these as errors or count them as failures.

---

## 4. Framing

Every message is one frame:

```text
┌────────────────────────────┬──────────────────────────────────┐
│ length: 4 bytes, unsigned, │ payload: `length` bytes,         │
│ big-endian (network order) │ UTF-8 JSON object                │
└────────────────────────────┴──────────────────────────────────┘
```

- `length` must satisfy **1 ≤ length ≤ 65 536** (64 KiB).
- The response must be exactly one frame. Send it, flush, then close the connection, or leave closing to the station.
- If the rig receives an invalid length, it must close the connection without responding.

---

## 5. Messages

The JSON is **strict**:
- Duplicate keys, `NaN`/`Infinity`, unknown fields and missing fields are all rejected.
- Field types are exact: booleans must be `true`/`false`, not `0`/`1`, and integers must not be floats.

### 5.1 Request — `RF_TEST_REQUEST` (station → rig)

```json
{
  "protocol_version": 1,
  "message_type": "RF_TEST_REQUEST",
  "request_id": "6f1c8e9a-2b4d-4c3e-9f1a-7d2b5c8e0a14",
  "batch_id": "b2a7…",
  "station_id": "STN-01",
  "jig_id": "JIG-01",
  "targets": [
    {"slot_number": 1, "expected_mac": "02:5A:00:A8:8E:A3"},
    {"slot_number": 4, "expected_mac": "02:5A:00:A8:8E:A6"}
  ]
}
```

| Field | Rules |
|---|---|
| `protocol_version` | Integer, always `1`. Reject any other value. |
| `request_id` | UUID string. Echo it unchanged. |
| `batch_id`, `station_id`, `jig_id` | Non-empty strings. Log them; they identify the work. |
| `targets` | Between 1 and 8 entries. Slot numbers are unique and positive; MACs are unique. |
| `expected_mac` | Uppercase, colon-separated, 6 bytes. This is the DUT's programmed Stock BD address. |

Slots that are empty, failed or on HOLD at the station are **not** included. The rig tests exactly the listed targets.

### 5.2 Response — `RF_TEST_RESPONSE` (rig → station)

```json
{
  "protocol_version": 1,
  "message_type": "RF_TEST_RESPONSE",
  "request_id": "6f1c8e9a-2b4d-4c3e-9f1a-7d2b5c8e0a14",
  "rig_id": "GOLDEN-RIG-01",
  "results": [
    {"slot_number": 1, "expected_mac": "02:5A:00:A8:8E:A3", "reported_mac": "02:5A:00:A8:8E:A3",
     "connected": true, "disconnected": true, "error_code": null, "detail": ""},
    {"slot_number": 4, "expected_mac": "02:5A:00:A8:8E:A6", "reported_mac": null,
     "connected": false, "disconnected": false, "error_code": "CONNECT_FAILED",
     "detail": "Device not found within 8.0 s scan"}
  ]
}
```

Every `results` entry must have **all seven fields**:

| Field | Type | Meaning |
|---|---|---|
| `slot_number` | integer | Copied from the target. |
| `expected_mac` | string | Copied from the target, byte-for-byte. |
| `reported_mac` | string or `null` | The Bluetooth address of the peer the rig was **actually connected to**, taken from the link layer. **Never copy it from the request.** Use `null` if no connection was made. |
| `connected` | boolean | `true` only if a BLE connection was established. |
| `disconnected` | boolean | `true` only if that connection was then closed cleanly. Must be `false` when `connected` is `false`. |
| `error_code` | string or `null` | `null` on success; otherwise a code from section 7. |
| `detail` | string | Human-readable explanation, shown to Supervisors. Use `""` (never `null`) when there is nothing to add. |

The station rejects the **entire** response, and asks for a retry, unless all of the following hold:
- `request_id` matches the request.
- `rig_id` equals the approved rig ID (the certificate CN).
- There is **exactly one result per target** with the same `slot_number` and `expected_mac`. Order does not matter; no slot may be missing or added.
- No result has `disconnected: true` with `connected: false`.
- No result reports success (`connected` and `disconnected` both `true`, `error_code` null) with a `reported_mac` that differs from `expected_mac`.
- Every `error_code` is a known code from section 7.

---

## 6. How the station grades each result

| Observation | Outcome on station | Effect |
|---|---|---|
| `connected` and `disconnected` both `true`, `reported_mac` equals `expected_mac`, `error_code` is `null` | **PASS** | Slot continues to Pricol programming. |
| `reported_mac` is present but differs from `expected_mac` | **HOLD** | Identity problem; Supervisor review. |
| `connected` is `true` but `disconnected` is `false` | **HOLD** | The link was not closed cleanly. |
| `error_code` is `ADDRESS_MISMATCH`, `IDENTITY_UNCERTAIN` or `TEST_INCOMPLETE` | **HOLD** | |
| Any other DUT error, e.g. `CONNECT_FAILED` | **FAIL** | Slot is rejected. |
| Any **infrastructure** error code on any slot | Whole response **rejected** | No slot changes; the station shows "Golden Rig unavailable, Supervisor intervention, RETRY". |

Report honestly. A wrong PASS sends an untested module on to permanent Pricol programming. When the rig is unsure, use `IDENTITY_UNCERTAIN` or `TEST_INCOMPLETE` (HOLD), not a guess.

---

## 7. Error codes

Only these codes are accepted. Any other code makes the station reject the whole response as a protocol error.

**DUT-level codes** (per slot; other slots stay valid):

| Code | Use when | Station outcome |
|---|---|---|
| `CONNECT_FAILED` | The device was not found in the scan, or the connection attempt failed or timed out. | FAIL |
| `DISCONNECT_FAILED` | Connected, but the disconnect did not complete. Set `connected: true`, `disconnected: false`. | HOLD (connected without disconnect) |
| `ADDRESS_MISMATCH` | The connected peer's address is not the expected MAC. | HOLD |
| `IDENTITY_UNCERTAIN` | The rig cannot prove which device it connected to. | HOLD |
| `TEST_INCOMPLETE` | The time budget ran out before this target was tested. | HOLD |

**Infrastructure codes** (the rig itself cannot test; the station rejects the response and retries later):

`SERVER_UNAVAILABLE`, `SERVER_INTERNAL_ERROR`, `RIG_BUSY`, `RIG_INTERNAL_ERROR`, `ADAPTER_UNAVAILABLE`, `ADAPTER_ERROR`, `BLUETOOTH_STACK_ERROR`, `TEST_ENGINE_ERROR`

There is no separate error message type in v1. To report an infrastructure fault, send a normal `RF_TEST_RESPONSE` with **one result per target**, each carrying the infrastructure code, `connected`/`disconnected` set to `false`, `reported_mac` set to `null`, and the reason in `detail`.

If the request itself cannot be parsed, close the connection without a response. The station treats that as a communication failure and allows a retry.

---

## 8. Timing and concurrency

- **The station waits at most 30 s in total** for connect, TLS handshake, request, test and response (`SecureGoldenRigTransport.operation_timeout`). The rig must respond within **25 s** of receiving the request for a full 8-slot jig. If more time is genuinely needed, the station timeout must be raised in code, so coordinate first.
- Suggested budget for 8 targets:
  - one shared scan of up to **8 s** that collects all expected addresses at once;
  - connect/disconnect with **4 s** per device and up to **4 connections in parallel**, if the adapter supports it (most Windows and BlueZ adapters handle 2–4);
  - stop at **23 s**; mark targets not yet tested `TEST_INCOMPLETE`.
- **One RF test at a time per Bluetooth adapter.** Several stations may share a rig, so serialize requests with a queue. If a request cannot *start* within about 5 s, answer immediately with `RIG_BUSY` for every target rather than letting the station's 30 s budget expire.
- Handshake-only connections (section 3.4) must never wait in the test queue.

---

## 9. Bluetooth test procedure (per target)

1. **Scan** for an advertiser whose address equals `expected_mac`, ignoring case and separators.
   - Use the address type the DUT firmware advertises. A MAC programmed with MPCLI is normally the **public** BD address. If the Stock firmware advertises a static random address, match that instead.
   - Not found within the scan budget: `CONNECT_FAILED`, with `detail` `"not advertising"`.
2. **Connect** to that advertiser, with a timeout of about 4 s.
   - Fails or times out: `CONNECT_FAILED`.
3. **Read the peer address of the established link** (the connection or device object, not the request) and put it in `reported_mac`.
   - If it differs from `expected_mac`: `ADDRESS_MISMATCH`.
4. *(Optional, recommended once the Stock firmware supports it)* read a GATT characteristic in which the DUT reports its own address or serial. This proves identity beyond the link layer.
5. **Disconnect** and wait for the disconnection to complete.
   - Confirmed: `disconnected: true`.
   - Not confirmed within about 3 s: `DISCONNECT_FAILED`, with `connected: true` and `disconnected: false`.
6. Never pair or bond, and never write to the DUT. The test must leave the module unchanged for Pricol programming.

---

## 10. Reference implementation (Python, asyncio + bleak)

This is a complete server that meets sections 3–9. The `BleakRfTester` class is the only part that touches hardware. Swap it for a vendor SDK if the rig uses an RTL8762 golden module as the BLE central.

Verified so far: this exact code was run against the station's real client (`SecureGoldenRigTransport`, `GoldenRigService`, and the binding screen's identity probe) with a simulated Bluetooth tester. The checks were: handshake-only probe, an 8-slot request with one absent DUT (7 PASS, 1 FAIL), `RIG_BUSY` under contention, and the server staying healthy afterwards. The certificate recipe in section 3.3 was also checked against the station's identity probe. The `BleakRfTester` class has **not** yet been run on a real jig.

```python
"""golden_rig_server.py — Golden Rig application, protocol v1.

Run:  python golden_rig_server.py --rig-id GOLDEN-RIG-01 \
          --cert rig.pem --key rig.key --port 8762
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import ssl
import struct
import time
import uuid
from dataclasses import dataclass, field

PROTOCOL_VERSION = 1
MAX_FRAME = 64 * 1024
REQUEST_WAIT_S = 10.0        # handshake-only connections close before this
QUEUE_WAIT_S = 5.0           # longer than this -> RIG_BUSY
TEST_BUDGET_S = 23.0         # station allows 30 s end to end

log = logging.getLogger("golden_rig")


class ProtocolError(ValueError):
    pass


@dataclass
class SlotResult:
    slot_number: int
    expected_mac: str
    reported_mac: str | None = None
    connected: bool = False
    disconnected: bool = False
    error_code: str | None = None
    detail: str = ""

    def to_json(self) -> dict:
        return {
            "slot_number": self.slot_number,
            "expected_mac": self.expected_mac,
            "reported_mac": self.reported_mac,
            "connected": self.connected,
            "disconnected": self.disconnected,
            "error_code": self.error_code,
            "detail": self.detail,
        }


# --------------------------------------------------------------------------
# Strict request parsing (mirrors the station codec)
# --------------------------------------------------------------------------

def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"duplicate key {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ProtocolError(f"non-standard constant {value}")


def normalise_mac(value: str) -> str:
    compact = value.replace(":", "").replace("-", "").upper()
    if len(compact) != 12 or any(c not in "0123456789ABCDEF" for c in compact):
        raise ProtocolError(f"invalid MAC {value!r}")
    return ":".join(compact[i:i + 2] for i in range(0, 12, 2))


def parse_request(payload: bytes) -> dict:
    data = json.loads(payload.decode("utf-8"), object_pairs_hook=_no_duplicates,
                      parse_constant=_reject_constant)
    expected = {"protocol_version", "message_type", "request_id", "batch_id",
                "station_id", "jig_id", "targets"}
    if not isinstance(data, dict) or set(data) != expected:
        raise ProtocolError("unexpected request fields")
    if type(data["protocol_version"]) is not int or data["protocol_version"] != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol version")
    if data["message_type"] != "RF_TEST_REQUEST":
        raise ProtocolError("unexpected message type")
    uuid.UUID(data["request_id"])
    for name in ("batch_id", "station_id", "jig_id"):
        if not isinstance(data[name], str) or not data[name].strip():
            raise ProtocolError(f"{name} required")
    targets = data["targets"]
    if not isinstance(targets, list) or not 1 <= len(targets) <= 8:
        raise ProtocolError("1-8 targets required")
    slots, macs = set(), set()
    for item in targets:
        if not isinstance(item, dict) or set(item) != {"slot_number", "expected_mac"}:
            raise ProtocolError("invalid target")
        if type(item["slot_number"]) is not int or item["slot_number"] <= 0:
            raise ProtocolError("invalid slot_number")
        mac = normalise_mac(item["expected_mac"])
        if item["slot_number"] in slots or mac in macs:
            raise ProtocolError("duplicate slot or MAC")
        slots.add(item["slot_number"])
        macs.add(mac)
    return data


def build_response(request: dict, rig_id: str, results: list[SlotResult]) -> bytes:
    body = json.dumps({
        "protocol_version": PROTOCOL_VERSION,
        "message_type": "RF_TEST_RESPONSE",
        "request_id": request["request_id"],
        "rig_id": rig_id,
        "results": [result.to_json() for result in results],
    }, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise ProtocolError("response too large")
    return struct.pack("!I", len(body)) + body


def infrastructure_results(request: dict, code: str, detail: str) -> list[SlotResult]:
    return [SlotResult(t["slot_number"], t["expected_mac"], error_code=code, detail=detail)
            for t in request["targets"]]


# --------------------------------------------------------------------------
# BLE tester (hardware-specific part)
# --------------------------------------------------------------------------

class BleakRfTester:
    """Scan once for all targets, then connect/verify/disconnect each."""

    def __init__(self, scan_s: float = 8.0, connect_s: float = 4.0,
                 disconnect_s: float = 3.0, parallel: int = 3) -> None:
        self.scan_s, self.connect_s, self.disconnect_s = scan_s, connect_s, disconnect_s
        self.parallel = parallel

    async def run(self, targets: list[dict], deadline: float) -> list[SlotResult]:
        from bleak import BleakScanner  # imported here so tests need no BLE stack

        wanted = {normalise_mac(t["expected_mac"]): t for t in targets}
        found: dict[str, object] = {}
        done = asyncio.Event()

        def on_advert(device, _adv):
            mac = device.address.upper()
            if mac in wanted and mac not in found:
                found[mac] = device
                if len(found) == len(wanted):
                    done.set()

        async with BleakScanner(detection_callback=on_advert):
            try:
                await asyncio.wait_for(done.wait(), timeout=min(self.scan_s, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                pass

        gate = asyncio.Semaphore(self.parallel)

        async def one(target: dict) -> SlotResult:
            expected = target["expected_mac"]
            result = SlotResult(target["slot_number"], expected)
            device = found.get(normalise_mac(expected))
            if device is None:
                result.error_code, result.detail = "CONNECT_FAILED", f"not advertising within {self.scan_s:.0f} s scan"
                return result
            async with gate:
                if time.monotonic() + self.connect_s + self.disconnect_s > deadline:
                    result.error_code, result.detail = "TEST_INCOMPLETE", "time budget exhausted"
                    return result
                await self._connect_cycle(device, result)
            return result

        return list(await asyncio.gather(*(one(t) for t in targets)))

    async def _connect_cycle(self, device, result: SlotResult) -> None:
        from bleak import BleakClient

        client = BleakClient(device, timeout=self.connect_s)
        try:
            await client.connect()
        except Exception as exc:  # noqa: BLE001 - any failure is a DUT connect failure
            result.error_code, result.detail = "CONNECT_FAILED", f"connect failed: {exc}"
            return
        result.connected = client.is_connected
        if not result.connected:
            result.error_code, result.detail = "CONNECT_FAILED", "connection not established"
            return
        # Address of the established link's peer (not the request).
        result.reported_mac = normalise_mac(client.address)
        if result.reported_mac != normalise_mac(result.expected_mac):
            result.error_code = "ADDRESS_MISMATCH"
            result.detail = f"connected to {result.reported_mac}"
        try:
            await asyncio.wait_for(client.disconnect(), timeout=self.disconnect_s)
            result.disconnected = not client.is_connected
        except Exception as exc:  # noqa: BLE001
            result.detail = f"disconnect failed: {exc}"
        if not result.disconnected and result.error_code is None:
            result.error_code = "DISCONNECT_FAILED"
            result.detail = result.detail or "disconnect not confirmed"


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------

@dataclass
class GoldenRigServer:
    rig_id: str
    tester: object
    adapter_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            try:
                header = await asyncio.wait_for(reader.readexactly(4), REQUEST_WAIT_S)
            except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
                log.debug("Identity probe or idle connection from %s", peer)
                return
            (length,) = struct.unpack("!I", header)
            if not 0 < length <= MAX_FRAME:
                log.warning("Invalid frame length %d from %s", length, peer)
                return
            payload = await asyncio.wait_for(reader.readexactly(length), REQUEST_WAIT_S)
            try:
                request = parse_request(payload)
            except (ProtocolError, ValueError, UnicodeDecodeError) as exc:
                log.warning("Rejected malformed request from %s: %s", peer, exc)
                return   # no response: station treats it as a communication failure

            log.info("RF test request %s | station=%s batch=%s slots=%s", request["request_id"],
                     request["station_id"], request["batch_id"],
                     [t["slot_number"] for t in request["targets"]])
            results = await self._test(request)
            writer.write(build_response(request, self.rig_id, results))
            await writer.drain()
            for r in results:
                log.info("  slot %s %s reported=%s conn=%s disc=%s err=%s %s", r.slot_number,
                         r.expected_mac, r.reported_mac, r.connected, r.disconnected,
                         r.error_code, r.detail)
        except Exception:  # noqa: BLE001 - one bad connection must not stop the server
            log.exception("Connection from %s failed", peer)
        finally:
            writer.close()

    async def _test(self, request: dict) -> list[SlotResult]:
        try:
            await asyncio.wait_for(self.adapter_lock.acquire(), QUEUE_WAIT_S)
        except asyncio.TimeoutError:
            return infrastructure_results(request, "RIG_BUSY", "rig is testing another jig")
        try:
            deadline = time.monotonic() + TEST_BUDGET_S
            return await self.tester.run(request["targets"], deadline)
        except Exception as exc:  # noqa: BLE001 - adapter or stack fault
            log.exception("Bluetooth test engine failure")
            return infrastructure_results(request, "BLUETOOTH_STACK_ERROR", str(exc)[:200])
        finally:
            self.adapter_lock.release()


def tls_context(cert: str, key: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    return context


async def serve(rig_id: str, cert: str, key: str, host: str, port: int, tester=None) -> None:
    server = GoldenRigServer(rig_id, tester or BleakRfTester())
    listener = await asyncio.start_server(server.handle, host, port, ssl=tls_context(cert, key))
    log.info("Golden Rig %s listening on %s:%d", rig_id, host, port)
    async with listener:
        await listener.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rig-id", required=True, help="must equal the certificate CN")
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8762)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(serve(args.rig_id, args.cert, args.key, args.host, args.port))
```

### Notes on the reference implementation

- **`rig_id` must equal the certificate CN.** Consider reading it from the certificate at startup so the two cannot drift apart.
- `client.address` in bleak is the address the link was opened to. On Windows and BlueZ that is the advertiser's address, so it is real link-layer evidence and not an echo of the request. For stronger evidence, add a GATT identity read (section 9, step 4).
- On Windows, `bleak` needs Windows 10 build 16299 or newer. On Linux it needs BlueZ 5.55+, and the service user must have Bluetooth permissions.

---

## 11. Deployment and operation

1. **Run as a service.**
   - Windows: NSSM or a scheduled task at boot.
   - Linux: a `systemd` unit with `Restart=always`.
   - Run under a dedicated account.
2. **Firewall:** allow inbound TCP 8762 from station subnets only.
3. **Logging:** keep at least 90 days of logs. For every request, log `request_id`, `station_id`, `batch_id`, and each slot's observations and error code. These logs complement the evidence the stations store and upload.
4. **Clock:** use NTP.
5. **Health:** an operator can check the rig without testing anything. A Supervisor's *Request identity* on the station's *Golden Rig binding* screen performs a handshake-only probe.
6. **Changes that require re-approval on every station:** a new certificate, a new IP or DNS name, a new port, or a new rig ID.

---

## 12. Acceptance tests

Run these before the rig is used in production. Tests 1–4 need no DUTs.

| # | Test | Expected |
|---|---|---|
| 1 | Station → Station configuration → Golden Rig binding → enter rig host and port → **Request identity** | Rig ID equals the CN; SHA-256 equals `openssl x509 -fingerprint -sha256`; certificate shown as trusted. |
| 2 | Point the station at the rig using a hostname that is not in the SAN | Station rejects the rig as "not trusted". |
| 3 | Approve the binding, then start a production session | Session starts. The rig logs a handshake-only connection without errors. |
| 4 | Stop the rig service, then start a session | The station refuses: "Cannot reach …". |
| 5 | Run a Stock-only batch with 1–8 good DUTs | All slots RF PASS. Rig log shows each slot with `conn=True disc=True err=None`. |
| 6 | Remove the antenna from one DUT, or power it off before the RF test | That slot FAILs with `CONNECT_FAILED`; the other slots PASS. |
| 7 | Two stations request a test at the same moment | One is served; the other gets `RIG_BUSY` (station shows RETRY), and succeeds on retry. |
| 8 | Disable the Bluetooth adapter during a test | Station shows "Golden Rig unavailable" with RETRY; no slot changes state. |
| 9 | Full 8-slot jig, timed | The response arrives in under 25 s. |

### Conformance check without hardware

The station repository can drive the rig protocol directly from Python:

```python
from stationapp.domain.golden_rig import GoldenRigTarget
from stationapp.domain.mac import MacAddress
from stationapp.services.golden_rig_service import GoldenRigService
from stationapp.infrastructure.golden_rig.secure_transport import SecureGoldenRigTransport

# binding = the approved GoldenRigBinding (rig_id, host, port, certificate_sha256 …)
transport = SecureGoldenRigTransport(binding=binding, ca_certificate="golden_rig_ca.pem")
provider = type("P", (), {"create_transport": lambda self: transport})()
report = GoldenRigService(provider).run_test(
    batch_id="CONFORMANCE-1", station_id=binding.station_id, jig_id="JIG-01",
    targets=(GoldenRigTarget(1, MacAddress.parse("02:5A:00:00:00:01")),),
)
print(report.response)
```

A conformant rig either returns a response that passes every station check, or answers with `CONNECT_FAILED` when no DUT is present.

---

## 13. Open points to agree with the station team

1. **DUT address type.** Is the Stock firmware's advertised address the programmed public BD address? This decides the scan filter.
2. **Identity characteristic.** Can the Stock firmware expose its own address or serial over GATT, so identity is proven beyond the link layer (section 9, step 4)?
3. **Time budget.** Is 25 s enough for a full jig in the real RF environment? If not, the station's 30 s `operation_timeout` must be raised in the same release.
4. **Shared rigs.** How many stations share one rig? That decides whether one adapter suffices or whether the rig needs one adapter per station.
