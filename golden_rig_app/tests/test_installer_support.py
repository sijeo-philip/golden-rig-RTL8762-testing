import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rig_certtool as ct
import rig_launcher as rl

SLOTS = {'slots': [{'slot_number': i, 'port': f'COM{i + 4}', 'usb_serial': None} for i in range(1, 5)]}


class CertToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ca_key = ct.new_private_key()
        cls.ca = ct.build_ca(cls.ca_key)
        cls.rig_key = ct.new_private_key()
        cls.csr = ct.build_csr(cls.rig_key, 'GOLDEN-RIG-01')

    def test_issued_certificate_matches_station_requirements(self):
        cert = ct.sign_csr(self.csr, self.ca, self.ca_key, [], ['192.168.10.50'])
        errors, warnings = ct.check_certificate(cert, self.rig_key, self.ca, 'GOLDEN-RIG-01', ['golden-rig-01.local'])
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])
        self.assertEqual(ct.san_entries(cert), (['golden-rig-01.local'], ['192.168.10.50']))
        self.assertRegex(ct.fingerprint(cert), r'^([0-9A-F]{2}:){31}[0-9A-F]{2}$')

    def test_detects_wrong_cn_missing_san_wrong_key_and_wrong_ca(self):
        cert = ct.sign_csr(self.csr, self.ca, self.ca_key, ['other.local'], [])
        other_ca_key = ct.new_private_key()
        other_ca = ct.build_ca(other_ca_key, 'Other CA')
        errors, _ = ct.check_certificate(cert, ct.new_private_key(), other_ca, 'GOLDEN-RIG-02', ['golden-rig-01.local'])
        text = '\n'.join(errors)
        for fragment in ('does not equal rig ID', 'DNS:golden-rig-01.local', 'does not match', 'not issued by'):
            self.assertIn(fragment, text)

    def test_rejects_non_ca_issuer(self):
        leaf = ct.sign_csr(self.csr, self.ca, self.ca_key, [], [])
        with self.assertRaises(ValueError):
            ct.sign_csr(self.csr, leaf, self.rig_key, [], [])

    def test_cli_request_sign_show_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            ca_dir, rig_dir = Path(d, 'ca'), Path(d, 'rig')
            self.assertEqual(ct.main(['new-ca', '--out-dir', str(ca_dir)]), 0)
            self.assertEqual(ct.main(['request', '--rig-id', 'GOLDEN-RIG-07', '--out-dir', str(rig_dir)]), 0)
            self.assertEqual(ct.main(['sign', '--csr', str(rig_dir / 'rig.csr'), '--ca-cert', str(ca_dir / ct.CA_CERT_NAME),
                                      '--ca-key', str(ca_dir / ct.CA_KEY_NAME), '--out-dir', str(rig_dir)]), 0)
            self.assertTrue((rig_dir / ct.FINGERPRINT_NAME).read_text().startswith('SHA256 Fingerprint='))
            show = ['show', '--cert', str(rig_dir / 'rig.pem'), '--key', str(rig_dir / 'rig.key'),
                    '--ca-cert', str(ca_dir / ct.CA_CERT_NAME), '--rig-id', 'GOLDEN-RIG-07']
            self.assertEqual(ct.main(show + ['--dns', 'golden-rig-07.local']), 0)
            self.assertEqual(ct.main(show + ['--dns', 'golden-rig-01.local']), 1)
            with self.assertRaises(SystemExit):  # never silently overwrite a private key
                ct.main(['request', '--rig-id', 'GOLDEN-RIG-07', '--out-dir', str(rig_dir)])


class LauncherTests(unittest.TestCase):
    def make_rig(self, d: Path, rig_id='GOLDEN-RIG-01', dns=('golden-rig-01.local',), mdns_name='golden-rig-01'):
        ca_key = ct.new_private_key()
        ca = ct.build_ca(ca_key)
        key = ct.new_private_key()
        cert = ct.sign_csr(ct.build_csr(key, rig_id), ca, ca_key, list(dns), [])
        (d / 'certs').mkdir()
        (d / 'certs/rig.key').write_bytes(ct._key_pem(key))
        (d / 'certs/rig.pem').write_bytes(ct._cert_pem(cert))
        (d / 'certs/golden_rig_ca.pem').write_bytes(ct._cert_pem(ca))
        (d / 'slots.json').write_text(json.dumps(SLOTS))
        config = json.loads(Path(__file__).resolve().parents[1].joinpath('installer/rig_config.example.json').read_text())
        config['rig_id'] = 'GOLDEN-RIG-01'
        config['mdns']['name'] = mdns_name
        # UTF-8 BOM, as written by some Windows editors, must be accepted
        (d / 'rig_config.json').write_text(json.dumps(config), encoding='utf-8-sig')
        return rl.load_config(d / 'rig_config.json')

    def test_valid_installation_passes_preflight(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(rl, 'computer_name', return_value='GOLDEN-RIG-01'):
            cfg = self.make_rig(Path(d))
            self.assertEqual(cfg.slots_config, Path(d, 'slots.json').resolve())
            errors, warnings, fp = rl.preflight(cfg)
            self.assertEqual((errors, warnings), ([], []))
            self.assertTrue(fp)
            args = rl.server_args(cfg)
            self.assertEqual((args.rig_id, args.mdns, args.mdns_name, args.port), ('GOLDEN-RIG-01', True, 'golden-rig-01', 8762))

    def test_mdns_name_missing_from_san_is_an_error_and_computer_name_mismatch_warns(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(rl, 'computer_name', return_value='DESKTOP-1234'):
            cfg = self.make_rig(Path(d), mdns_name='rig-bay-3')
            errors, warnings, _ = rl.preflight(cfg)
            self.assertTrue(any('DNS:rig-bay-3.local' in e for e in errors))
            self.assertTrue(any('RIG-BAY-3' in w for w in warnings))

    def test_cn_mismatch_and_missing_slots(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self.make_rig(Path(d), rig_id='GOLDEN-RIG-09')
            Path(d, 'slots.json').unlink()
            errors, _, _ = rl.preflight(cfg)
            self.assertTrue(any('does not equal rig ID' in e for e in errors))
            self.assertTrue(any('Slots configuration not found' in e for e in errors))

    def test_config_validation(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, 'rig_config.json')
            for bad in ({'rig_id': '', 'cert': 'a', 'key': 'b'}, {'rig_id': 'R', 'cert': 'a', 'key': 'b', 'port': 0},
                        {'rig_id': 'R', 'cert': 'a', 'key': 'b', 'typo': 1},
                        {'rig_id': 'R', 'cert': 'a', 'key': 'b', 'mdns': {'name': 'bad name'}}):
                with self.subTest(bad=bad):
                    path.write_text(json.dumps(bad))
                    with self.assertRaises(ValueError):
                        rl.load_config(path)


if __name__ == '__main__':
    unittest.main()
