"""Golden Rig Protocol v1 TLS server; Python 3.11+, bleak>=0.22."""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import re
import ssl
import struct
import time
import uuid
import ipaddress
from dataclasses import dataclass, asdict

LOG = logging.getLogger('golden_rig')
ERROR_CODES = {'CONNECT_FAILED','DISCONNECT_FAILED','ADDRESS_MISMATCH','IDENTITY_UNCERTAIN','TEST_INCOMPLETE', 'SERVER_UNAVAILABLE','SERVER_INTERNAL_ERROR','RIG_BUSY','RIG_INTERNAL_ERROR','ADAPTER_UNAVAILABLE','ADAPTER_ERROR','BLUETOOTH_STACK_ERROR','TEST_ENGINE_ERROR'}
MAC_RE = re.compile(r'^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$')
MAX_FRAME=65536
MDNS_SERVICE_TYPE='_golden-rig._tcp.local.'

def mdns_hostname(rig_id, requested_name=None):
    name=requested_name
    if name is None:
        name=re.sub(r'[^a-z0-9]+','-',rig_id.lower()).strip('-')
    name=name.lower().removesuffix('.local')
    if len(name)>63 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?',name):
        raise ValueError('mDNS name must be a valid DNS label (optionally ending in .local)')
    return name

def mdns_addresses():
    import ifaddr

    addresses=[]
    for adapter in ifaddr.get_adapters():
        for interface_address in adapter.ips:
            address=interface_address.ip
            if isinstance(address, tuple):
                address=address[0]
            parsed=ipaddress.ip_address(address)
            if not (parsed.is_loopback or parsed.is_link_local or parsed.is_unspecified or parsed.is_multicast):
                packed=parsed.packed
                if packed not in addresses:
                    addresses.append(packed)
    if not addresses:
        raise RuntimeError('No LAN addresses are available to advertise via mDNS')
    return addresses

def mdns_service_info(name, rig_id, port):
    from zeroconf import ServiceInfo

    return ServiceInfo(
        MDNS_SERVICE_TYPE,
        f'{name}.{MDNS_SERVICE_TYPE}',
        addresses=mdns_addresses(),
        port=port,
        properties={'rig-id': rig_id, 'protocol-version': '1'},
        server=f'{name}.local.',
    )

def mac(value):
    if not isinstance(value,str): raise ValueError('MAC is not a string')
    compact=value.upper().replace(':','').replace('-','')
    if not re.fullmatch(r'[0-9A-F]{12}',compact): raise ValueError('Invalid MAC')
    return ':'.join(compact[i:i+2] for i in range(0,12,2))

def unique_pairs(pairs):
    data={}
    for key,value in pairs:
        if key in data: raise ValueError('Duplicate JSON key')
        data[key]=value
    return data

def reject_constant(value): raise ValueError('Non-JSON constant')

def parse_request(raw):
    data=json.loads(raw.decode('utf-8'),object_pairs_hook=unique_pairs,parse_constant=reject_constant)
    if type(data) is not dict or set(data)!={'protocol_version','message_type','request_id','batch_id','station_id','jig_id','targets'}: raise ValueError('Invalid request fields')
    if type(data['protocol_version']) is not int or data['protocol_version']!=1 or data['message_type']!='RF_TEST_REQUEST': raise ValueError('Invalid protocol')
    if type(data['request_id']) is not str: raise ValueError('Invalid request ID')
    uuid.UUID(data['request_id'])
    for key in ('batch_id','station_id','jig_id'):
        if type(data[key]) is not str or not data[key].strip(): raise ValueError(f'Invalid {key}')
    targets=data['targets']
    if type(targets) is not list or not 1<=len(targets)<=8: raise ValueError('Invalid targets')
    slots=set(); macs=set()
    for target in targets:
        if type(target) is not dict or set(target)!={'slot_number','expected_mac'}: raise ValueError('Invalid target fields')
        slot=target['slot_number']; address=target['expected_mac']
        if type(slot) is not int or slot<1 or type(address) is not str or not MAC_RE.fullmatch(address): raise ValueError('Invalid slot or MAC')
        if slot in slots or address in macs: raise ValueError('Duplicate slot or MAC')
        slots.add(slot);macs.add(address)
    return data

@dataclass
class Result:
    slot_number:int
    expected_mac:str
    reported_mac:str|None=None
    connected:bool=False
    disconnected:bool=False
    error_code:str|None=None
    detail:str=''

def error_results(request,code,detail):
    assert code in ERROR_CODES
    return [Result(x['slot_number'],x['expected_mac'],error_code=code,detail=detail) for x in request['targets']]

class BleakTester:
    def __init__(self,scan_seconds=8,parallel=3):
        self.scan_seconds=scan_seconds; self.parallel=parallel
    async def run(self,targets,deadline):
        from bleak import BleakScanner, BleakClient
        wanted={x['expected_mac'] for x in targets}
        found={};complete=asyncio.Event()
        def on_adv(device,_adv):
            try: address=mac(device.address)
            except ValueError: return
            if address in wanted:
                found[address]=device
                if len(found)==len(wanted): complete.set()
        async with BleakScanner(detection_callback=on_adv):
            try: await asyncio.wait_for(complete.wait(),timeout=max(.01,min(self.scan_seconds,deadline-time.monotonic())))
            except asyncio.TimeoutError: pass
        gate=asyncio.Semaphore(self.parallel)
        async def test_one(target):
            result=Result(target['slot_number'],target['expected_mac'])
            device=found.get(result.expected_mac)
            if device is None:
                result.error_code='CONNECT_FAILED';result.detail='not advertising';return result
            async with gate:
                if deadline-time.monotonic()<7:
                    result.error_code='TEST_INCOMPLETE';result.detail='time budget exhausted';return result
                client=BleakClient(device,timeout=4)
                try:
                    await asyncio.wait_for(client.connect(),4)
                    result.connected=bool(client.is_connected)
                    if not result.connected:
                        result.error_code='CONNECT_FAILED';result.detail='connection not confirmed';return result
                    # On Bleak, client.address is the BLEDevice address used by the backend.
                    # If the backend cannot establish peer identity beyond this, configure a
                    # firmware GATT identity read before treating this as proof.
                    result.reported_mac=mac(client.address)
                    if result.reported_mac!=result.expected_mac:
                        result.error_code='ADDRESS_MISMATCH';result.detail='connected peer mismatch'
                except Exception as exc:
                    result.error_code='CONNECT_FAILED';result.detail=f'connect failed: {exc}'[:180];return result
                finally:
                    if result.connected:
                        try:
                            await asyncio.wait_for(client.disconnect(),3)
                            result.disconnected=not client.is_connected
                        except Exception as exc:
                            result.detail=f'disconnect failed: {exc}'[:180]
                        if not result.disconnected and result.error_code is None: result.error_code='DISCONNECT_FAILED'
                return result
        return list(await asyncio.gather(*(test_one(t) for t in targets)))

class RigServer:
    def __init__(self,rig_id,tester):
        self.rig_id=rig_id;self.tester=tester;self.lock=asyncio.Lock()
    async def handle(self,reader,writer):
        try:
            try: header=await asyncio.wait_for(reader.readexactly(4),10)
            except (asyncio.TimeoutError,asyncio.IncompleteReadError): return # TLS-only identity probe
            length=struct.unpack('!I',header)[0]
            if not 1<=length<=MAX_FRAME: return
            try:
                payload=await asyncio.wait_for(reader.readexactly(length),10)
                request=parse_request(payload)
            except (asyncio.TimeoutError,asyncio.IncompleteReadError,ValueError,UnicodeError): return
            try: await asyncio.wait_for(self.lock.acquire(),5)
            except asyncio.TimeoutError: results=error_results(request,'RIG_BUSY','adapter occupied')
            else:
                try:
                    deadline=time.monotonic()+23
                    results=await asyncio.wait_for(self.tester.run(request['targets'],deadline),timeout=23.5)
                except asyncio.TimeoutError: results=error_results(request,'TEST_ENGINE_ERROR','test engine exceeded deadline')
                except Exception as exc:
                    LOG.exception('BLE engine failed')
                    results=error_results(request,'BLUETOOTH_STACK_ERROR',str(exc)[:180])
                finally: self.lock.release()
            body=json.dumps({'protocol_version':1,'message_type':'RF_TEST_RESPONSE','request_id':request['request_id'],'rig_id':self.rig_id,'results':[asdict(x) for x in results]},separators=(',',':'),allow_nan=False).encode()
            if len(body)>MAX_FRAME: return
            writer.write(struct.pack('!I',len(body))+body)
            await writer.drain()
            LOG.info('station=%s batch=%s request=%s results=%s',request['station_id'],request['batch_id'],request['request_id'],body.decode())
        except Exception: LOG.exception('Connection handler failed')
        finally:
            writer.close()
            try: await writer.wait_closed()
            except Exception: pass

async def run(args):
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(args.cert,args.key)
    if args.backend == 'serial-at':
        from serial_at_backend import SerialAtTester, load_slots
        if not args.slots_config:
            raise SystemExit('--slots-config is required for serial-at backend')
        tester = SerialAtTester(load_slots(args.slots_config))
    else:
        tester = BleakTester(parallel=args.parallel)
    server=RigServer(args.rig_id,tester)
    listener=await asyncio.start_server(server.handle,args.host,args.port,ssl=context)
    LOG.info('%s listening on %s:%s',args.rig_id,args.host,args.port)
    zeroconf=None
    service_info=None
    try:
        if args.mdns:
            from zeroconf import InterfaceChoice, Zeroconf

            name=mdns_hostname(args.rig_id,args.mdns_name)
            zeroconf=Zeroconf(interfaces=InterfaceChoice.All)
            service_info=mdns_service_info(name,args.rig_id,args.port)
            zeroconf.register_service(service_info)
            LOG.info('mDNS advertised as %s.local (%s:%s)',name,args.host,args.port)
        async with listener:
            await listener.serve_forever()
    finally:
        listener.close()
        await listener.wait_closed()
        if zeroconf is not None:
            if service_info is not None:
                zeroconf.unregister_service(service_info)
            zeroconf.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for field in ('rig-id','cert','key'): parser.add_argument('--'+field,required=True)
    parser.add_argument('--host',default='0.0.0.0')
    parser.add_argument('--port',type=int,default=8762)
    parser.add_argument('--mdns',action='store_true',help='Advertise the rig on the LAN using mDNS')
    parser.add_argument('--mdns-name',help='mDNS hostname label; defaults to a normalized rig ID')
    parser.add_argument('--parallel',type=int,choices=range(1,5),default=3)
    parser.add_argument('--backend',choices=('serial-at','bleak'),default='serial-at')
    parser.add_argument('--slots-config')
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    asyncio.run(run(args))
