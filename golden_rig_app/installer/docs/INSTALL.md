# Golden Rig — Installation and Certificate Guide

This guide covers installing the Golden Rig server on the rig PC and creating its TLS certificate, private key and fingerprint. It also covers naming the PC so stations can reach it as `<name>.local` (mDNS), and approving the rig on each station.

Full protocol and acceptance-test specification: `docs\golden_rig_application.md`.

---

## 1. What gets installed

| Location | Contents |
|---|---|
| `C:\Program Files\Golden Rig\` | `GoldenRigServer.exe`, `rig_certtool.exe`, `Configure-GoldenRig.ps1`, this guide. Python is bundled; nothing else needs installing. |
| `C:\ProgramData\GoldenRig\rig_config.json` | Rig ID, port, mDNS name, file paths (see section 7) |
| `C:\ProgramData\GoldenRig\slots.json` | Slot → COM port binding of the Golden Controllers |
| `C:\ProgramData\GoldenRig\certs\` | `rig.pem`, `rig.key`, `golden_rig_ca.pem`, `rig.sha256.txt` (only Administrators, SYSTEM and the rig user can read it) |
| `C:\ProgramData\GoldenRig\logs\` | `golden_rig.log`, rotated daily, 90 days kept |

Start-menu → **Golden Rig**:

| Shortcut | Use |
|---|---|
| Golden Rig Server | Start the server (console window shows the log; close it or press Ctrl+C to stop) |
| Check Golden Rig configuration | Check the certificate, key, CA, mDNS name and slots without starting the server |
| Configure Golden Rig | Change the rig ID, mDNS / computer name or port, import certificates or generate a CSR, and update the firewall |
| List COM ports | Show COM ports and USB serial numbers for `slots.json` |
| Certificate tool | Command prompt with `rig_certtool` available |
| Golden Rig data folder | Open `C:\ProgramData\GoldenRig` |

Uninstalling removes the program and firewall rules but **keeps** `C:\ProgramData\GoldenRig` (certificates, config, logs).

---

## 2. Before you install — decide the identity

Three values must agree everywhere:

| Value | Example | Must match |
|---|---|---|
| **Rig ID** | `GOLDEN-RIG-01` | Certificate Common Name (CN); station `GOLDEN_RIG_INVENTORY` |
| **mDNS hostname** | `golden-rig-01` → `golden-rig-01.local` | Certificate SAN `DNS:golden-rig-01.local`; the host each station binds |
| **Windows computer name** (recommended) | `GOLDEN-RIG-01` | The mDNS hostname, upper-case. Max **15 characters**. |

The server advertises `<mdns>.local` itself (python-zeroconf). Windows 10/11 also answers mDNS for its **own computer name**. If the two differ, the PC answers for two names. If another PC on the LAN already uses the name, resolution becomes unreliable. Making the computer name equal the mDNS hostname avoids both problems. The installer and *Configure Golden Rig* can rename the PC for you, which needs a restart.

> Changing the rig ID or the mDNS hostname later requires a **new certificate** and **Supervisor re-approval on every station**.

---

## 3. Create the certificate, key and fingerprint

The station trusts the rig only if **all** of these hold:

1. The certificate is signed by the **Golden Rig CA** (`golden_rig_ca.pem` on the station).
2. **CN = rig ID.**
3. The **SAN** contains the name the station connects to (`DNS:golden-rig-01.local`, plus `IP:` entries if any station binds by IP).
4. Its **SHA-256 fingerprint** equals the one a Supervisor approved on the station.

`rig_certtool.exe` (in the install folder, or `python rig_certtool.py` from source) does everything below without OpenSSL. Equivalent OpenSSL commands are in section 3.5.

### 3.1 Create the Golden Rig CA (once per site, on a secured PC — not the rig)

```bat
rig_certtool new-ca --out-dir D:\GoldenRigCA
```

This creates `golden_rig_ca.pem` (public, give it to every station and rig) and `golden_rig_ca.key`. **Keep `golden_rig_ca.key` offline** (encrypted USB/safe). Skip this step if your site already has a Golden Rig CA. Use the same CA as the stations.

### 3.2 Option A (recommended): key generated on the rig, CA signs a request

The private key never leaves the rig PC.

1. **On the rig PC**, choose *Generate a new private key and certificate request* in the installer, or run *Configure Golden Rig* → option 2. Manual equivalent:
   ```bat
   rig_certtool request --rig-id GOLDEN-RIG-01 --out-dir C:\ProgramData\GoldenRig\certs
   ```
   Give `C:\ProgramData\GoldenRig\certs\rig.csr` (public, not secret) to the CA operator.
2. **On the CA PC**, sign it. `--dns` defaults to `<rig-id>.local`; add `--dns`/`--ip` for every name or address stations use:
   ```bat
   rig_certtool sign --csr rig.csr --ca-cert golden_rig_ca.pem --ca-key golden_rig_ca.key --dns golden-rig-01.local --out-dir out
   ```
   Output: `out\rig.pem` and `out\rig.sha256.txt` (the fingerprint). Return `rig.pem` and `golden_rig_ca.pem` to the rig.
3. **On the rig PC**, run *Configure Golden Rig* → option 1 and select `rig.pem` and `golden_rig_ca.pem`. Leave the key box empty: the key generated in step 1 is already installed.

### 3.3 Option B: CA PC issues key and certificate together

```bat
rig_certtool issue --rig-id GOLDEN-RIG-01 --dns golden-rig-01.local --ca-cert golden_rig_ca.pem --ca-key golden_rig_ca.key --out-dir GOLDEN-RIG-01
```

Move `rig.key`, `rig.pem` and `golden_rig_ca.pem` to the rig over a trusted channel, import them in the installer's certificate page (or *Configure Golden Rig* → option 1), then **delete the copies** from the CA PC and the transfer medium.

### 3.4 Show / check the fingerprint

```bat
rig_certtool show --cert C:\ProgramData\GoldenRig\certs\rig.pem --key C:\ProgramData\GoldenRig\certs\rig.key --ca-cert C:\ProgramData\GoldenRig\certs\golden_rig_ca.pem --rig-id GOLDEN-RIG-01 --dns golden-rig-01.local
```

This prints the CN, SAN, validity and `SHA256 Fingerprint=AA:BB:…`, and reports any problem the station would reject. The fingerprint is also saved in `certs\rig.sha256.txt` and printed in the server log at every start.

### 3.5 OpenSSL equivalent (optional)

Use these on a PC with OpenSSL. On Git Bash, prefix `-subj` commands with `MSYS_NO_PATHCONV=1`.

```bash
# CA (once)
openssl req -new -newkey rsa:3072 -sha256 -nodes -keyout golden_rig_ca.key -out ca.csr -subj "/CN=Golden Rig CA"
printf 'basicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n' > ca.ext
openssl x509 -req -in ca.csr -signkey golden_rig_ca.key -days 3650 -sha256 -extfile ca.ext -out golden_rig_ca.pem

# Rig key + CSR (CN = rig ID)
openssl req -new -newkey rsa:3072 -sha256 -nodes -keyout rig.key -out rig.csr -subj "/CN=GOLDEN-RIG-01"

# Sign (SAN = every name/address stations use)
cat > rig.ext <<'EOF'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
subjectAltName=DNS:golden-rig-01.local
EOF
openssl x509 -req -in rig.csr -CA golden_rig_ca.pem -CAkey golden_rig_ca.key -CAcreateserial -days 1095 -sha256 -extfile rig.ext -out rig.pem

# Verify + fingerprint
openssl verify -CAfile golden_rig_ca.pem -verify_hostname golden-rig-01.local rig.pem
openssl x509 -in rig.pem -noout -fingerprint -sha256
```

Do not create the CA with `req -x509 -addext`. On OpenSSL 1.1.1 it produces a duplicate Basic Constraints extension, and stations then reject every rig certificate.

---

## 4. Install

1. Run `GoldenRigSetup-<version>.exe` as an administrator.
2. **Tasks:** keep *Windows Firewall* selected. This allows inbound TCP on the rig port from the local subnet, and mDNS on UDP 5353.
3. **Rig identity:** enter the rig ID, the mDNS hostname (without `.local`) and the port (default 8762).
4. **Computer name:** choose *Rename this computer…* unless the PC already has that name. The installer asks to restart at the end.
5. **Rig certificate:** *Import* (section 3.3 or the return trip of 3.2), *Generate a key and CSR* (section 3.2), or *Configure later*.
6. A PowerShell window shows the configuration result and fingerprint. Read it, then press Enter.
7. **Edit `slots.json`:** run *List COM ports*, then edit `C:\ProgramData\GoldenRig\slots.json` so that each physical slot (1–4 or 1–8) has its Golden Controller COM port, plus `usb_serial` when it is shown:
   ```json
   {"slots": [{"slot_number": 1, "port": "COM7", "usb_serial": "A1B2C3"}, ...]}
   ```
8. Restart if the computer was renamed, run *Check Golden Rig configuration* until it says **Configuration OK**, then start **Golden Rig Server**.

Silent install (for IT):
```bat
GoldenRigSetup-1.0.0.exe /SILENT /TASKS="firewall"
```
Silent mode uses the defaults, or the values from the previous install. Configure afterwards with:
```bat
powershell -ExecutionPolicy Bypass -File "C:\Program Files\Golden Rig\Configure-GoldenRig.ps1" -RigId GOLDEN-RIG-01 -MdnsName golden-rig-01 -RenameComputer -CertFile rig.pem -KeyFile rig.key -CaCertFile golden_rig_ca.pem -ConfigureFirewall -NonInteractive
```

`-RigUser DOMAIN\operator` gives a different Windows account read access to the key, if the server will run under an account other than the one that installed it.

---

## 5. Approve the rig on each station

1. Station → Station configuration → **Golden Rig binding** → host `golden-rig-01.local`, port `8762` → **Request identity**.
2. Check: the rig ID equals the CN, the **SHA-256 equals `rig.sha256.txt`** (or the "Certificate SHA256 Fingerprint" line in the rig log), and the certificate shows as trusted.
3. Supervisor approves the binding. Each station also needs `golden_rig_ca.pem` at `config\certificates\golden_rig_ca.pem`.

Re-approval is needed after any change of certificate, key, rig ID, hostname or port.

---

## 6. Changing the computer name / mDNS name later

1. Get a new certificate whose SAN contains the new `<name>.local` (section 3). Use option A to keep a new key on the rig.
2. Run **Configure Golden Rig**, enter the new mDNS hostname, answer **Y** to rename the computer, and import the new certificate.
3. Restart Windows, run *Check Golden Rig configuration*, then re-approve on every station.

---

## 7. `rig_config.json` reference

```json
{
  "rig_id": "GOLDEN-RIG-01",
  "host": "0.0.0.0",
  "port": 8762,
  "backend": "serial-at",
  "slots_config": "slots.json",
  "cert": "certs/rig.pem",
  "key": "certs/rig.key",
  "ca_cert": "certs/golden_rig_ca.pem",
  "mdns": {"enabled": true, "name": "golden-rig-01"},
  "parallel": 3,
  "log_dir": "logs",
  "log_retention_days": 90
}
```

Relative paths are relative to the config file. The server refuses to start if the CN ≠ `rig_id`, if the key does not match the certificate, if the SAN lacks `<mdns.name>.local` (when mDNS is enabled), if the certificate is expired or not issued by `ca_cert`, or if `slots.json` is invalid. `ca_cert` may be removed (null) to skip the chain check; the server warns when it is missing.

Command line: `GoldenRigServer.exe [--config PATH] [--check] [--list-ports] [--pause]`. The config path can also be set with the `GOLDEN_RIG_CONFIG` environment variable.

---

## 8. Troubleshooting

| Symptom | Check |
|---|---|
| Server window closes / "Not starting" | Run *Check Golden Rig configuration*. The errors are listed with the file each one relates to. |
| Station: "Cannot reach …" | Is the server running? Run `ping golden-rig-01.local` from the station. Is the firewall rule present (`wf.msc` → group "Golden Rig")? Are the station and rig on the same subnet/VLAN with multicast allowed? Is the network profile not blocking discovery? |
| `.local` name does not resolve | mDNS does not cross VLANs, VPNs or routers. Use LAN DNS or bind by IP, and add that `IP:` to the certificate SAN. Check that no other PC uses the same name. |
| Station: "not trusted" | The station is using a name that is not in the SAN, the station has a different CA, or the PC clock is wrong. |
| Fingerprint changed | The certificate was replaced. A Supervisor must re-approve it on each station. |
| `ADAPTER_UNAVAILABLE` | A COM port in `slots.json` is missing or has the wrong `usb_serial`. Run *List COM ports*. |
