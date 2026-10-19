# Golden Rig — RTL8762 serial AT backend (Protocol v1)

This package runs on the **Golden Rig PC**, not the DUT programming station. The station is the TLS client and sends `RF_TEST_REQUEST` with *slot_number* and *expected_mac* obtained from the station's eligible DUT records. The rig owns 4 or 8 pre-bound RTL8762 Golden Controller COM ports, connects each controller to the requested MAC, and returns `RF_TEST_RESPONSE` observations.

## Setup (Windows / Python 3.11)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy slots.example.json slots.json
# Edit slots.json with physical slot-to-COM mappings
python golden_rig_server.py --backend serial-at --slots-config slots.json --rig-id GOLDEN-RIG-01 --cert rig.pem --key rig.key --port 8762
```

Certificate CN must match `--rig-id`. Use the CA and SAN certificate procedure in `StationApp/docs/golden_rig_application.md`. Configure and approve the same rig certificate on each station. Do not reuse the legacy unsecured port 1111 wire protocol. The reference server can also run `--backend bleak` if intentionally selected.

## Stable LAN name (mDNS)

The rig can publish a stable `.local` hostname using multicast DNS, so stations on the same multicast-enabled LAN can connect without tracking its changing IP address. Install dependencies with `pip install -r requirements.txt`, then enable mDNS with `--mdns`:

```powershell
python golden_rig_server.py --backend serial-at --slots-config slots.json --rig-id GOLDEN-RIG-01 --cert rig.pem --key rig.key --port 8762 --mdns
```

By default, `GOLDEN-RIG-01` is advertised as `golden-rig-01.local`. Set `--mdns-name` to choose a different single-label hostname, for example `--mdns-name golden-rig`; `.local` may be included. The server also advertises the `_golden-rig._tcp.local.` service with the rig ID and protocol version. Configure each station's rig endpoint as `<hostname>.local:<port>`; mDNS does not update station configuration automatically.

mDNS requires multicast traffic to be allowed between the station and rig. It is generally limited to the local network and may not work across VLANs, VPNs, or networks that block multicast. The TLS certificate must also be trusted by the station and valid for the hostname it uses (include the `.local` hostname in the certificate SAN when hostname verification is enabled). Use managed LAN DNS instead when the stations are on networks where mDNS cannot resolve.

## Binding

- `slots.json`: each physical Golden controller is assigned to a fixed slot number and COM port. Optionally record its USB `usb_serial` (strongly recommended when available). On each request, the backend rejects missing/mismatched controllers as an infrastructure fault.
- TLS certificate: the Supervisor-approved **rig-to-station** binding is independent from the Golden controller **slot-to-COM** binding.
- DUT MAC: sent per slot by StationApp over TLS; it is not discovered by COM enumeration order or reassigned by the rig.

## AT sequence per eligible slot

`AT+CONN=<reversed MAC>` → verify `+CONNECTED>>0:<MAC>` → `AT+SYSMODE=AT` → `AT+RSSI` → `AT+DISC=<reversed MAC>` → require `+DISCONNECTED>>` → `AT+RESET`.

The old program treated bare `+CONNECTED` as success. This package treats that as `IDENTITY_UNCERTAIN` (HOLD) because no peer MAC was proven. This is intentionally fail-closed. A real device's exact response formats must be verified before release.

RSSI is recorded in `detail` because Protocol v1 has no `rssi` field; the station's Protocol v1 result classifier does **not** set an RSSI threshold. Updating the protocol to include RSSI requires coordinated station and rig changes.

## Known integration limitations / production gates

- This package is code-level integrated but **not hardware validated** with real RTL8762 controllers.
- The server's request lock serializes batches. Up to 8 serial workers run in parallel; overall station timeout is 30 seconds. Confirm the real UART/firmware commands reliably meet the budget.
- If a COM port is busy or unplugged mid-test, its error should be treated as infrastructure failure, not a bad DUT. Check the actual failure modes in station regression.
- With bare connection confirmation, the rig must not claim PASS. Do not relax without a separate trustworthy peer identity read.
- The currently supplied legacy firmware interface doesn't demonstrate a GATT identity characteristic. Add only if verified against hardware.
- Physical programming slot-to-DUT mapping and Wi-Fi DUT MAC receipt remain StationApp responsibilities.

## Unit smoke checks

`python -m unittest discover -s tests -v`
