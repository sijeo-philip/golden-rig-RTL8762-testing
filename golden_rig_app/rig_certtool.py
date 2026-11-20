"""Golden Rig certificate tool (no OpenSSL required).

Produces the same CA / rig certificate profile as the OpenSSL procedure in
golden_rig_application.md section 3.3, and prints the SHA-256 fingerprint
(over the DER certificate) that a Supervisor approves on each station.

Typical flow (CA private key never stays on the rig):

  On the rig PC:        rig_certtool request --rig-id GOLDEN-RIG-01 --out-dir certs
  On the CA machine:    rig_certtool sign --csr rig.csr --ca-cert golden_rig_ca.pem
                                      --ca-key golden_rig_ca.key --dns golden-rig-01.local
  Back on the rig PC:   copy rig.pem + golden_rig_ca.pem next to rig.key, then
                        rig_certtool show --cert rig.pem --key rig.key --ca-cert golden_rig_ca.pem
"""
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import sys
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from golden_rig_server import mdns_hostname

KEY_BITS = 3072
CA_DAYS = 3650
RIG_DAYS = 1095
EXPIRY_WARNING_DAYS = 60
CA_KEY_NAME, CA_CERT_NAME = 'golden_rig_ca.key', 'golden_rig_ca.pem'
RIG_KEY_NAME, RIG_CSR_NAME, RIG_CERT_NAME = 'rig.key', 'rig.csr', 'rig.pem'
FINGERPRINT_NAME = 'rig.sha256.txt'


# ---------------------------------------------------------------- helpers

def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def fingerprint(cert: x509.Certificate) -> str:
    """Same format as `openssl x509 -noout -fingerprint -sha256`."""
    return ':'.join(f'{b:02X}' for b in cert.fingerprint(hashes.SHA256()))


def common_name(name: x509.Name) -> str | None:
    values = name.get_attributes_for_oid(NameOID.COMMON_NAME)
    return str(values[0].value) if values else None


def san_entries(cert: x509.Certificate) -> tuple[list[str], list[str]]:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return [], []
    dns = [d.lower().rstrip('.') for d in san.get_values_for_type(x509.DNSName)]
    ips = [str(i) for i in san.get_values_for_type(x509.IPAddress)]
    return dns, ips


def default_dns_name(rig_id: str) -> str:
    return f'{mdns_hostname(rig_id)}.local'


def load_cert(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(Path(path).read_bytes())


def load_key(path: Path):
    return serialization.load_pem_private_key(Path(path).read_bytes(), password=None)


def _write_new(path: Path, data: bytes, force: bool) -> None:
    path = Path(path)
    if path.exists() and not force:
        raise SystemExit(f'Refusing to overwrite existing {path} (use --force)')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _key_pem(key) -> bytes:
    # Unencrypted, like `openssl req -nodes`: the server loads it unattended.
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _cert_pem(cert) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def write_fingerprint_file(cert: x509.Certificate, path: Path) -> None:
    Path(path).write_text(f'SHA256 Fingerprint={fingerprint(cert)}\n', encoding='utf-8')


def new_private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=KEY_BITS)


# ---------------------------------------------------------------- builders

def build_ca(key, cn: str = 'Golden Rig CA', days: int = CA_DAYS) -> x509.Certificate:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = _now()
    return (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False,
                                         key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, key_cert_sign=True, crl_sign=True,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))


def build_csr(key, rig_id: str) -> x509.CertificateSigningRequest:
    if not rig_id.strip():
        raise ValueError('Rig ID must not be empty')
    return (x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, rig_id.strip())]))
            .sign(key, hashes.SHA256()))


def sign_csr(csr: x509.CertificateSigningRequest, ca_cert: x509.Certificate, ca_key,
             dns_names: list[str], ip_addresses: list[str], days: int = RIG_DAYS) -> x509.Certificate:
    """Issue a rig server certificate. SAN comes from the CA operator, never from the CSR."""
    if not csr.is_signature_valid:
        raise ValueError('CSR signature is invalid')
    cn = common_name(csr.subject)
    if not cn:
        raise ValueError('CSR has no Common Name (the CN must be the rig ID)')
    try:
        bc = ca_cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound:
        bc = None
    if bc is None or not bc.ca:
        raise ValueError('The supplied CA certificate is not a CA (BasicConstraints CA:TRUE missing)')
    if ca_cert.public_key().public_numbers() != ca_key.public_key().public_numbers():
        raise ValueError('CA key does not match CA certificate')
    if not dns_names:
        dns_names = [default_dns_name(cn)]
    sans: list[x509.GeneralName] = [x509.DNSName(d.lower().rstrip('.')) for d in dns_names]
    sans += [x509.IPAddress(ipaddress.ip_address(i)) for i in ip_addresses]
    now = _now()
    not_after = now + dt.timedelta(days=days)
    if not_after > ca_cert.not_valid_after_utc:
        print(f'WARNING: validity truncated to CA expiry {ca_cert.not_valid_after_utc:%Y-%m-%d}', file=sys.stderr)
        not_after = ca_cert.not_valid_after_utc
    try:
        ca_ski = ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
        aki = x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski)
    except x509.ExtensionNotFound:
        aki = x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key())
    return (x509.CertificateBuilder()
            .subject_name(csr.subject).issuer_name(ca_cert.subject)
            .public_key(csr.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                         key_encipherment=True, data_encipherment=False,
                                         key_agreement=False, key_cert_sign=False, crl_sign=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(csr.public_key()), critical=False)
            .add_extension(aki, critical=False)
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .sign(ca_key, hashes.SHA256()))


# ---------------------------------------------------------------- checks

def check_certificate(cert: x509.Certificate, key=None, ca_cert: x509.Certificate | None = None,
                      rig_id: str | None = None, required_dns: list[str] = ()) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for the rig certificate as the station will judge it."""
    errors: list[str] = []
    warnings: list[str] = []
    cn = common_name(cert.subject)
    if rig_id is not None and cn != rig_id:
        errors.append(f'Certificate CN "{cn}" does not equal rig ID "{rig_id}"')
    dns, ips = san_entries(cert)
    if not dns and not ips:
        errors.append('Certificate has no Subject Alternative Name; stations verify the hostname')
    for name in required_dns:
        if name.lower().rstrip('.') not in dns:
            errors.append(f'Certificate SAN does not contain DNS:{name} (SAN has: {", ".join(dns + ips) or "nothing"})')
    now = _now()
    if now < cert.not_valid_before_utc:
        errors.append(f'Certificate is not valid until {cert.not_valid_before_utc:%Y-%m-%d %H:%M} UTC (check the PC clock)')
    if now > cert.not_valid_after_utc:
        errors.append(f'Certificate expired on {cert.not_valid_after_utc:%Y-%m-%d}')
    elif cert.not_valid_after_utc - now < dt.timedelta(days=EXPIRY_WARNING_DAYS):
        warnings.append(f'Certificate expires on {cert.not_valid_after_utc:%Y-%m-%d}; plan renewal and station re-approval')
    if key is not None:
        spki = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        if key.public_key().public_bytes(*spki) != cert.public_key().public_bytes(*spki):
            errors.append('Private key does not match the certificate')
    if ca_cert is None:
        warnings.append('No CA certificate configured; chain to the Golden Rig CA was not verified')
    else:
        try:
            cert.verify_directly_issued_by(ca_cert)
        except (ValueError, TypeError, InvalidSignature):
            errors.append(f'Certificate was not issued by the configured CA "{common_name(ca_cert.subject)}"')
    return errors, warnings


def describe(cert: x509.Certificate) -> str:
    dns, ips = san_entries(cert)
    return '\n'.join([
        f'  Subject CN (rig ID): {common_name(cert.subject)}',
        f'  Issuer CN          : {common_name(cert.issuer)}',
        f'  SAN DNS            : {", ".join(dns) or "-"}',
        f'  SAN IP             : {", ".join(ips) or "-"}',
        f'  Valid              : {cert.not_valid_before_utc:%Y-%m-%d} .. {cert.not_valid_after_utc:%Y-%m-%d}',
        f'  SHA256 Fingerprint={fingerprint(cert)}',
    ])


# ---------------------------------------------------------------- commands

def cmd_new_ca(a) -> int:
    out = Path(a.out_dir)
    key = new_private_key()
    cert = build_ca(key, a.cn, a.days)
    _write_new(out / CA_KEY_NAME, _key_pem(key), a.force)
    _write_new(out / CA_CERT_NAME, _cert_pem(cert), a.force)
    print(f'Created {out / CA_CERT_NAME} and {out / CA_KEY_NAME}')
    print('Keep golden_rig_ca.key OFFLINE. Copy golden_rig_ca.pem to every station and rig.')
    return 0


def cmd_request(a) -> int:
    out = Path(a.out_dir)
    key = new_private_key()
    csr = build_csr(key, a.rig_id)
    _write_new(out / RIG_KEY_NAME, _key_pem(key), a.force)
    _write_new(out / RIG_CSR_NAME, csr.public_bytes(serialization.Encoding.PEM), a.force)
    print(f'Created private key {out / RIG_KEY_NAME} (keep it on this PC) and request {out / RIG_CSR_NAME}')
    print(f'Send {RIG_CSR_NAME} to the CA operator; ask for SAN DNS:{default_dns_name(a.rig_id)}')
    return 0


def _issue_and_write(csr, a, out: Path) -> x509.Certificate:
    ca_cert = load_cert(a.ca_cert)
    ca_key = load_key(a.ca_key)
    cert = sign_csr(csr, ca_cert, ca_key, a.dns or [], a.ip or [], a.days)
    _write_new(out / RIG_CERT_NAME, _cert_pem(cert), a.force)
    write_fingerprint_file(cert, out / FINGERPRINT_NAME)
    errors, warnings = check_certificate(cert, ca_cert=ca_cert)
    for w in warnings:
        print('WARNING:', w)
    if errors:
        raise SystemExit('\n'.join(errors))
    print(f'Issued {out / RIG_CERT_NAME}\n{describe(cert)}')
    print(f'Fingerprint written to {out / FINGERPRINT_NAME}. A Supervisor approves this value on each station.')
    return cert


def cmd_sign(a) -> int:
    csr = x509.load_pem_x509_csr(Path(a.csr).read_bytes())
    _issue_and_write(csr, a, Path(a.out_dir))
    return 0


def cmd_issue(a) -> int:
    out = Path(a.out_dir)
    key = new_private_key()
    csr = build_csr(key, a.rig_id)
    _write_new(out / RIG_KEY_NAME, _key_pem(key), a.force)
    _issue_and_write(csr, a, out)
    print(f'Move {RIG_KEY_NAME} and {RIG_CERT_NAME} to the rig over a secure channel and delete them here.')
    return 0


def cmd_show(a) -> int:
    cert = load_cert(a.cert)
    key = load_key(a.key) if a.key else None
    ca_cert = load_cert(a.ca_cert) if a.ca_cert else None
    print(f'{a.cert}\n{describe(cert)}')
    errors, warnings = check_certificate(cert, key, ca_cert, a.rig_id, a.dns or [])
    for w in warnings:
        print('WARNING:', w)
    for e in errors:
        print('ERROR:', e)
    if a.fingerprint_out:
        write_fingerprint_file(cert, Path(a.fingerprint_out))
    if not errors:
        print('OK')
    return 1 if errors else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='rig_certtool', description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='command', required=True)

    s = sub.add_parser('new-ca', help='create the Golden Rig CA (once, on a secured machine)')
    s.add_argument('--out-dir', default='.')
    s.add_argument('--cn', default='Golden Rig CA')
    s.add_argument('--days', type=int, default=CA_DAYS)
    s.add_argument('--force', action='store_true')
    s.set_defaults(func=cmd_new_ca)

    s = sub.add_parser('request', help='create rig.key + rig.csr on the rig PC')
    s.add_argument('--rig-id', required=True, help='certificate CN, e.g. GOLDEN-RIG-01')
    s.add_argument('--out-dir', default='.')
    s.add_argument('--force', action='store_true')
    s.set_defaults(func=cmd_request)

    def ca_args(s):
        s.add_argument('--ca-cert', required=True)
        s.add_argument('--ca-key', required=True)
        s.add_argument('--dns', action='append', help='SAN DNS name, repeatable (default <rig-id>.local)')
        s.add_argument('--ip', action='append', help='SAN IP address, repeatable')
        s.add_argument('--days', type=int, default=RIG_DAYS)
        s.add_argument('--out-dir', default='.')
        s.add_argument('--force', action='store_true')

    s = sub.add_parser('sign', help='sign a rig CSR with the CA (on the CA machine)')
    s.add_argument('--csr', required=True)
    ca_args(s)
    s.set_defaults(func=cmd_sign)

    s = sub.add_parser('issue', help='create rig key + signed certificate in one step (on the CA machine)')
    s.add_argument('--rig-id', required=True)
    ca_args(s)
    s.set_defaults(func=cmd_issue)

    s = sub.add_parser('show', help='print CN/SAN/validity/fingerprint and check the certificate')
    s.add_argument('--cert', required=True)
    s.add_argument('--key')
    s.add_argument('--ca-cert')
    s.add_argument('--rig-id', help='require CN == rig ID')
    s.add_argument('--dns', action='append', help='require this SAN DNS name, repeatable')
    s.add_argument('--fingerprint-out', help='also write the fingerprint to this file')
    s.set_defaults(func=cmd_show)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, OSError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
