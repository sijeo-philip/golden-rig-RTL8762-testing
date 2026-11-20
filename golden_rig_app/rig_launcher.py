"""Golden Rig launcher: reads rig_config.json, checks certificate/mDNS/slots, starts the server.

Used by the installed "Golden Rig Server" shortcut. Configuration is looked up in
  --config PATH  >  %GOLDEN_RIG_CONFIG%  >  %ProgramData%\\GoldenRig\\rig_config.json
Relative paths inside the config are resolved against the config file's folder.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

import golden_rig_server
import rig_certtool

LOG = logging.getLogger('golden_rig.launcher')
DATA_DIR = Path(os.environ.get('ProgramData', r'C:\ProgramData')) / 'GoldenRig'
CONFIG_NAME = 'rig_config.json'
KNOWN_KEYS = {'rig_id', 'host', 'port', 'backend', 'slots_config', 'cert', 'key', 'ca_cert',
              'mdns', 'parallel', 'log_dir', 'log_retention_days'}


@dataclass
class RigConfig:
    path: Path
    rig_id: str
    host: str
    port: int
    backend: str
    slots_config: Path | None
    cert: Path
    key: Path
    ca_cert: Path | None
    mdns_enabled: bool
    mdns_name: str
    parallel: int
    log_dir: Path
    log_retention_days: int

    @property
    def mdns_fqdn(self) -> str:
        return f'{self.mdns_name}.local'


def default_config_path() -> Path:
    env = os.environ.get('GOLDEN_RIG_CONFIG')
    return Path(env) if env else DATA_DIR / CONFIG_NAME


def load_config(path: Path) -> RigConfig:
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if type(data) is not dict:
        raise ValueError('Configuration root must be a JSON object')
    unknown = set(data) - KNOWN_KEYS
    if unknown:
        raise ValueError(f'Unknown configuration keys: {", ".join(sorted(unknown))}')
    base = path.parent

    def p(value):
        return None if value in (None, '') else (base / value).resolve()

    rig_id = data.get('rig_id')
    if type(rig_id) is not str or not rig_id.strip():
        raise ValueError('rig_id must be a non-empty string (it must equal the certificate CN)')
    port = data.get('port', 8762)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('port must be an integer 1..65535')
    backend = data.get('backend', 'serial-at')
    if backend not in ('serial-at', 'bleak'):
        raise ValueError('backend must be "serial-at" or "bleak"')
    parallel = data.get('parallel', 3)
    if type(parallel) is not int or not 1 <= parallel <= 4:
        raise ValueError('parallel must be an integer 1..4')
    mdns = data.get('mdns', {}) or {}
    if type(mdns) is not dict:
        raise ValueError('mdns must be an object: {"enabled": true, "name": "golden-rig-01"}')
    retention = data.get('log_retention_days', 90)
    if type(retention) is not int or retention < 1:
        raise ValueError('log_retention_days must be a positive integer')
    for field in ('cert', 'key'):
        if not data.get(field):
            raise ValueError(f'{field} path is required')
    return RigConfig(
        path=path,
        rig_id=rig_id.strip(),
        host=data.get('host', '0.0.0.0'),
        port=port,
        backend=backend,
        slots_config=p(data.get('slots_config')),
        cert=p(data['cert']),
        key=p(data['key']),
        ca_cert=p(data.get('ca_cert')),
        mdns_enabled=bool(mdns.get('enabled', True)),
        mdns_name=golden_rig_server.mdns_hostname(rig_id.strip(), mdns.get('name') or None),
        parallel=parallel,
        log_dir=p(data.get('log_dir') or 'logs'),
        log_retention_days=retention,
    )


def computer_name() -> str:
    return os.environ.get('COMPUTERNAME') or socket.gethostname()


def preflight(cfg: RigConfig) -> tuple[list[str], list[str], str | None]:
    """Return (errors, warnings, fingerprint). Errors mean stations would reject the rig."""
    errors: list[str] = []
    warnings: list[str] = []
    fp = None
    for label, path in (('Certificate', cfg.cert), ('Private key', cfg.key)):
        if not path.is_file():
            errors.append(f'{label} not found: {path}')
    if cfg.ca_cert is not None and not cfg.ca_cert.is_file():
        errors.append(f'CA certificate not found: {cfg.ca_cert}')
    if not errors:
        try:
            cert = rig_certtool.load_cert(cfg.cert)
            fp = rig_certtool.fingerprint(cert)
        except Exception as exc:
            errors.append(f'Cannot read certificate {cfg.cert}: {exc}')
            cert = None
        try:
            key = rig_certtool.load_key(cfg.key)
        except TypeError:
            errors.append(f'Private key {cfg.key} is password protected; the rig needs an unencrypted key')
            key = None
        except Exception as exc:
            errors.append(f'Cannot read private key {cfg.key}: {exc}')
            key = None
        ca = None
        if cfg.ca_cert is not None:
            try:
                ca = rig_certtool.load_cert(cfg.ca_cert)
            except Exception as exc:
                errors.append(f'Cannot read CA certificate {cfg.ca_cert}: {exc}')
        if cert is not None:
            e, w = rig_certtool.check_certificate(
                cert, key, ca, cfg.rig_id, [cfg.mdns_fqdn] if cfg.mdns_enabled else [])
            errors += e
            warnings += w
    if cfg.backend == 'serial-at':
        if cfg.slots_config is None:
            errors.append('slots_config is required for the serial-at backend')
        else:
            try:
                from serial_at_backend import load_slots
                load_slots(str(cfg.slots_config))
            except FileNotFoundError:
                errors.append(f'Slots configuration not found: {cfg.slots_config}')
            except Exception as exc:
                errors.append(f'Slots configuration {cfg.slots_config} is invalid: {exc}')
    if cfg.mdns_enabled and computer_name().lower() != cfg.mdns_name:
        warnings.append(
            f'Computer name "{computer_name()}" differs from mDNS name "{cfg.mdns_name}". The rig still '
            f'advertises {cfg.mdns_fqdn}, but renaming the PC to "{cfg.mdns_name.upper()}" lets Windows answer '
            f'for it too (run "Configure Golden Rig").')
    return errors, warnings, fp


def setup_logging(cfg: RigConfig | None) -> None:
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s')
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    if cfg is None:
        return
    try:
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.TimedRotatingFileHandler(
            cfg.log_dir / 'golden_rig.log', when='midnight', backupCount=cfg.log_retention_days, encoding='utf-8')
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as exc:
        LOG.warning('File logging disabled (%s): %s', cfg.log_dir, exc)


def list_ports() -> int:
    from serial.tools.list_ports import comports
    ports = sorted(comports(), key=lambda p: p.device)
    if not ports:
        print('No COM ports found.')
    for port in ports:
        print(f'{port.device:8} usb_serial={port.serial_number or "-":24} {port.description}')
    print('\nCopy the COM port (and usb_serial when shown) of each Golden Controller into slots.json.')
    return 0


def server_args(cfg: RigConfig) -> argparse.Namespace:
    return argparse.Namespace(
        rig_id=cfg.rig_id, cert=str(cfg.cert), key=str(cfg.key), host=cfg.host, port=cfg.port,
        mdns=cfg.mdns_enabled, mdns_name=cfg.mdns_name, parallel=cfg.parallel, backend=cfg.backend,
        slots_config=str(cfg.slots_config) if cfg.slots_config else None)


def _wait_for_enter(force: bool) -> None:
    if force or (sys.stdin is not None and sys.stdin.isatty()):
        try:
            input('\nPress Enter to close...')
        except (EOFError, KeyboardInterrupt):
            pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog='GoldenRigServer', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config', type=Path, default=None, help='path to rig_config.json')
    ap.add_argument('--check', action='store_true', help='check configuration/certificates and exit')
    ap.add_argument('--list-ports', action='store_true', help='list COM ports and USB serial numbers')
    ap.add_argument('--pause', action='store_true', help='wait for Enter before closing')
    args = ap.parse_args(argv)

    if args.list_ports:
        code = list_ports()
        _wait_for_enter(args.pause)
        return code

    config_path = args.config or default_config_path()
    try:
        cfg = load_config(config_path)
    except FileNotFoundError:
        setup_logging(None)
        LOG.error('Configuration file not found: %s. Run "Configure Golden Rig" first.', config_path)
        _wait_for_enter(args.pause or not args.check)
        return 2
    except (ValueError, json.JSONDecodeError) as exc:
        setup_logging(None)
        LOG.error('Configuration file %s is invalid: %s', config_path, exc)
        _wait_for_enter(args.pause or not args.check)
        return 2

    setup_logging(None if args.check else cfg)
    LOG.info('Config %s: rig_id=%s port=%s backend=%s mDNS=%s', cfg.path, cfg.rig_id, cfg.port, cfg.backend,
             cfg.mdns_fqdn if cfg.mdns_enabled else 'disabled')
    errors, warnings, fp = preflight(cfg)
    if fp:
        LOG.info('Certificate SHA256 Fingerprint=%s', fp)
    for w in warnings:
        LOG.warning(w)
    for e in errors:
        LOG.error(e)
    if errors:
        LOG.error('Not starting: %d configuration error(s). See docs\\INSTALL.md.', len(errors))
        _wait_for_enter(True if not args.check else args.pause)
        return 1
    if args.check:
        LOG.info('Configuration OK. Stations should bind %s:%s and approve the fingerprint above.',
                 cfg.mdns_fqdn if cfg.mdns_enabled else '<rig address in SAN>', cfg.port)
        _wait_for_enter(args.pause)
        return 0
    try:
        asyncio.run(golden_rig_server.run(server_args(cfg)))
    except KeyboardInterrupt:
        LOG.info('Stopped by operator')
    except Exception:
        LOG.exception('Golden Rig server stopped with an error')
        _wait_for_enter(False)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
