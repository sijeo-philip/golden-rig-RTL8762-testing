import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from golden_rig_server import mdns_hostname


class MdnsHostnameTests(unittest.TestCase):
    def test_defaults_to_normalized_rig_id(self):
        self.assertEqual(mdns_hostname('GOLDEN-RIG-01'), 'golden-rig-01')

    def test_accepts_local_suffix(self):
        self.assertEqual(mdns_hostname('RIG-01', 'Golden-Rig.local'), 'golden-rig')

    def test_rejects_invalid_dns_labels(self):
        for name in ('', '-rig', 'rig-', 'two.labels', 'a name', 'x' * 64):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    mdns_hostname('RIG-01', name)


if __name__ == '__main__':
    unittest.main()
