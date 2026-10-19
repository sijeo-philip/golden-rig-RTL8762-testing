import json
import sys
import time
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from serial_at_backend import SerialSlot, reverse_mac, test_slot as run_slot_test, load_slots

EXPECTED='02:5A:00:A8:8E:A3'

class FakeSerial:
    def __init__(self, port, baud, timeout, write_timeout, connected_line='+CONNECTED>>0:025A00A88EA3'):
        self.port=port; self.line=connected_line; self.responses=[]; self.commands=[]; self.closed=False
    def reset_input_buffer(self): self.responses.clear()
    def write(self, data):
        command=data.decode().strip(); self.commands.append(command)
        if command.startswith('AT+CONN='): self.responses.append(self.line)
        if command=='AT+SYSMODE=AT': self.responses.append('OK')
        if command=='AT+RSSI': self.responses.append('+RSSI=-47dBm')
        if command.startswith('AT+DISC='): self.responses.append('+DISCONNECTED>>0:025A00A88EA3')
    def readline(self): return (self.responses.pop(0)+'\r\n').encode() if self.responses else b''
    def close(self): self.closed=True

class Tests(unittest.TestCase):
    def run_slot(self, connected_line):
        instances=[]
        def factory(*args,**kwargs):
            x=FakeSerial(*args,**kwargs,connected_line=connected_line); instances.append(x); return x
        r=run_slot_test(SerialSlot(1,'COM5'), {'slot_number':1,'expected_mac':EXPECTED},time.monotonic()+20,factory)
        return r, instances[0]
    def test_reversal(self): self.assertEqual(reverse_mac(EXPECTED),'A38EA8005A02')
    def test_success(self):
        r, ser=self.run_slot('+CONNECTED>>0:025A00A88EA3')
        self.assertTrue(r.connected and r.disconnected)
        self.assertEqual(r.reported_mac,EXPECTED)
        self.assertIsNone(r.error_code)
        self.assertEqual(ser.commands, ['AT+CONN=A38EA8005A02','AT+SYSMODE=AT','AT+RSSI','AT+DISC=A38EA8005A02','AT+RESET'])
    def test_generic_connected_is_identity_uncertain(self):
        r,_=self.run_slot('+CONNECTED')
        self.assertEqual(r.error_code,'IDENTITY_UNCERTAIN')
        self.assertIsNone(r.reported_mac)
        self.assertTrue(r.disconnected)
    def test_wrong_address_hold(self):
        r,_=self.run_slot('+CONNECTED>>0:AAAAAAAAAAAA')
        self.assertEqual(r.error_code,'ADDRESS_MISMATCH')
    def test_config_validation(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as d:
            path=Path(d)/'slots.json'
            path.write_text(json.dumps({'slots':[{'slot_number':i,'port':f'COM{i+4}'} for i in range(1,5)]}))
            self.assertEqual(len(load_slots(str(path))),4)
            path.write_text(json.dumps({'slots':[{'slot_number':i,'port':'COM5'} for i in range(1,5)]}))
            with self.assertRaises(ValueError): load_slots(str(path))
if __name__=='__main__': unittest.main()
