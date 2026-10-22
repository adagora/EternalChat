"""Web Push endpoint validation, encrypted HTTPS transport and generic delivery."""
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import ssl
import time
import unittest
from unittest.mock import patch

from eternalchat import push
from tests.test_engine import EngineFixture
from tests.test_web import WebFixture

EXTRAS = bool(importlib.util.find_spec('pywebpush'))


def sample():
    return {'endpoint':'https://fcm.googleapis.com/wp/fixture',
            'keys':{'p256dh':push.b64(b'\x04'+b'a'*64),'auth':push.b64(b'b'*16)}}


class PushValidationTests(unittest.TestCase):
    def test_provider_allowlist_and_key_bounds(self):
        self.assertEqual(push.subscription(sample()),sample())
        for endpoint in ['http://fcm.googleapis.com/a','https://127.0.0.1/a',
                         'https://fcm.googleapis.com.evil.test/a','https://fcm.googleapis.com:8443/a',
                         'https://me@fcm.googleapis.com/a','https://fcm.googleapis.com/a#fragment']:
            with self.assertRaises(ValueError): push.subscription({**sample(),'endpoint':endpoint})
        for keys in [{}, {'p256dh':'wrong','auth':'wrong'}, {'p256dh':push.b64(b'\x03'+b'a'*64),'auth':push.b64(b'a'*16)}]:
            with self.assertRaises(ValueError): push.subscription({**sample(),'keys':keys})


class PushWebTests(WebFixture):
    async def test_subscribe_ownership_revoke_expiry_and_single_completion(self):
        await self.serve()
        one = (await self.request('POST','/api/sessions/open',{'label':'One'}))[1]
        two = (await self.request('POST','/api/sessions/open',{'label':'Two'}))[1]
        self.chat.args.demo = False
        state = {'configured':True,'public_key':sample()['keys']['p256dh']}
        with patch.object(self.chat.push,'status',return_value=state):
            self.assertEqual((await self.request('POST','/api/push/subscribe',sample()))[0],400)
            for session in [one,two]:
                self.assertEqual((await self.request('POST','/api/push/subscribe',sample(),token=session['token']))[0],200)
            self.assertEqual(self.chat.sessions.recipients(),[(two['id'],sample())])
            self.assertFalse((await self.request('GET','/api/push',token=one['token']))[1]['subscribed'])
            self.chat.args.demo = False  # deterministic provider still used, permits opt-in delivery fixture
            deliveries = []
            async def send(subscription, identity):
                deliveries.append((subscription, identity)); return 201
            with patch.object(self.chat.push,'send',send):
                await self.chat.engine.ask('Remember this private sentence.')
                await self.chat.engine.finish()
                await self.chat.push.worker
            self.assertEqual(len(deliveries),1)
            self.assertNotIn('private sentence',json.dumps(deliveries))
            self.assertEqual((await self.request('POST','/api/sessions/revoke',{'id':two['id']},token=one['token']))[0],200)
            self.assertEqual(self.chat.sessions.recipients(),[])

    async def test_no_page_needed_expired_subscription_removed_and_shutdown_cancels(self):
        await self.serve()
        session = (await self.request('POST','/api/sessions/open',{'label':'Closed page'}))[1]
        self.chat.sessions.subscription(session['id'],sample())
        self.chat.args.demo = False
        async def gone(*args): return 410
        with patch.object(self.chat.push,'status',return_value={'configured':True}), patch.object(self.chat.push,'send',gone):
            # No SSE/page connection exists. Engine completion schedules the push itself.
            await self.chat.engine.ask('hello')
            await self.chat.push.worker
        self.assertFalse(self.chat.sessions.recipients())
        self.chat.sessions.subscription(session['id'],sample())
        started, stopped = asyncio.Event(), asyncio.Event()
        async def blocked(*args):
            started.set()
            try: await asyncio.Future()
            finally: stopped.set()
        with patch.object(self.chat.push,'status',return_value={'configured':True}), patch.object(self.chat.push,'send',blocked):
            self.chat.push.submit('fixture')
            await started.wait()
            await self.chat.push.close()
            self.assertTrue(stopped.is_set())


@unittest.skipUnless(EXTRAS,'install eternalchat[push] for encryption/HTTPS acceptance')
class PushCryptoTests(EngineFixture):
    async def fixture(self, status=201, block=False):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, rsa
        from cryptography.x509.oid import NameOID
        root = Path(self.temp.name)/str(time.monotonic_ns()); root.mkdir()
        vapid = root/'vapid.pem'; push.generate(vapid)
        self.assertEqual(vapid.stat().st_mode & 0o777,0o600)
        with self.assertRaises(FileExistsError): push.generate(vapid)
        self.enterContext(patch.dict(os.environ,ETERNALCHAT_PUSH_KEY=str(vapid),ETERNALCHAT_PUSH_SUBJECT='mailto:owner@example.test'))
        serverkey = rsa.generate_private_key(public_exponent=65537,key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')])
        now = datetime.now(timezone.utc)
        cert = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(serverkey.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]),critical=False).sign(serverkey,hashes.SHA256())
        certpath, keypath = root/'cert.pem',root/'key.pem'
        certpath.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        keypath.write_bytes(serverkey.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); tls.load_cert_chain(certpath,keypath)
        self.enterContext(patch.dict(os.environ,CURL_CA_BUNDLE=str(certpath),NO_PROXY='localhost'))
        self.received, self.closed = asyncio.Event(),asyncio.Event()
        self.calls=[]
        async def handle(reader,writer):
            try:
                head = await reader.readuntil(b'\r\n\r\n')
                length = int(next(l.split(b':',1)[1] for l in head.split(b'\r\n') if l.lower().startswith(b'content-length:')))
                body = await reader.readexactly(length)
                self.calls.append((head,body)); self.received.set()
                if block: await reader.read()
                else:
                    writer.write(f'HTTP/1.1 {status} Fixture\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'.encode()); await writer.drain()
            except (ConnectionError,asyncio.IncompleteReadError): pass
            finally: writer.close(); self.closed.set()
        server = await asyncio.start_server(handle,'127.0.0.1',0,ssl=tls)
        self.addCleanup(server.close)
        private = ec.generate_private_key(ec.SECP256R1())
        public = private.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
        auth = os.urandom(16)
        sub = {'endpoint':f'https://localhost:{server.sockets[0].getsockname()[1]}/push','keys':{'p256dh':push.b64(public),'auth':push.b64(auth)}}
        self.enterContext(patch('eternalchat.push.subscription',side_effect=lambda value:value)) # local trusted TLS fixture only
        service = push.Push(lambda:[],lambda *_:None); self.addAsyncCleanup(service.close)
        return service,sub,private,auth

    async def test_real_tls_encryption_signature_and_generic_payload(self):
        import http_ece
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils
        service,sub,private,auth = await self.fixture()
        self.assertTrue(service.status()['configured'])
        self.assertEqual(await service.send(sub,'test-message-9'),201)
        head,body = self.calls[0]
        self.assertIn(b'Content-Encoding: aes128gcm',head)
        data = json.loads(http_ece.decrypt(body,private_key=private,auth_secret=auth,version='aes128gcm'))
        self.assertEqual(data,{'title':'EternalChat','body':'A reply is ready.','id':'test-message-9'})
        self.assertNotIn(b'A reply is ready.',body)
        authorization = next(l.split(b':',1)[1].strip().decode() for l in head.split(b'\r\n') if l.lower().startswith(b'authorization:'))
        fields = dict(v.split('=',1) for v in authorization.removeprefix('vapid ').split(','))
        token,public = fields['t'],fields['k']; encoded,claims,signature = token.split('.')
        decode = lambda s:base64.urlsafe_b64decode(s+'='*(-len(s)%4))
        raw = decode(signature)
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(),decode(public))
        key.verify(utils.encode_dss_signature(int.from_bytes(raw[:32]),int.from_bytes(raw[32:])),f'{encoded}.{claims}'.encode(),ec.ECDSA(hashes.SHA256()))
        self.assertEqual(json.loads(decode(claims))['sub'],'mailto:owner@example.test')

    async def test_timeout_cancel_and_http_status_without_retry(self):
        service,sub,*_ = await self.fixture(status=410)
        self.assertEqual(await service.send(sub,'gone'),410)
        self.assertEqual(len(self.calls),1)
        service,sub,*_ = await self.fixture(block=True)
        task = asyncio.create_task(service.send(sub,'cancel'))
        await asyncio.wait_for(self.received.wait(),3)
        await service.close()
        with self.assertRaises(asyncio.CancelledError): await task
        await asyncio.wait_for(self.closed.wait(),3)
        self.assertEqual(service.active,set())

class NativePushWebTests(WebFixture):
    async def test_native_tokens_are_independent_owned_and_revoked_with_session(self):
        await self.serve()
        self.chat.args.demo = False
        one = (await self.request('POST','/api/sessions/open',{'label':'iPhone'}))[1]
        two = (await self.request('POST','/api/sessions/open',{'label':'iPad'}))[1]
        with patch.object(self.chat.push,'native_status',return_value={'configured':True,'topic':'org.eternalchat.companion'}):
            for session,token in [(one,'a'*64),(two,'b'*64)]:
                self.assertEqual((await self.request('POST','/api/native-push/subscribe',{'token':token},token=session['token']))[0],200)
            self.assertEqual(len(self.chat.sessions.recipients()),2)
            self.assertEqual((await self.request('POST','/api/native-push/subscribe',{'token':'invalid'},token=one['token']))[0],400)
            await self.request('POST','/api/sessions/revoke',{'id':one['id']})
            self.assertEqual(self.chat.sessions.recipients(),[(two['id'],{'kind':'apns','token':'b'*64})])


@unittest.skipUnless(EXTRAS,'install eternalchat[push] for native push signature checks')
class NativePushCryptoTests(EngineFixture):
    async def test_apns_jwt_fixed_endpoint_headers_and_no_message_content(self):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, utils
        private = ec.generate_private_key(ec.SECP256R1())
        path = Path(self.temp.name,'apns.p8')
        path.write_bytes(private.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        with patch.dict(os.environ,ETERNALCHAT_APNS_KEY=str(path),ETERNALCHAT_APNS_TEAM='TEAM123456',ETERNALCHAT_APNS_KEY_ID='KEY1234567',ETERNALCHAT_APNS_TOPIC='org.eternalchat.companion',ETERNALCHAT_APNS_SANDBOX='1'):
            service = push.Push(lambda:[],lambda *_:None)
            self.assertTrue(service.native_status()['configured'])
            sent=[]
            async def post(endpoint,headers,payload,**options):
                sent.append((endpoint,headers,json.loads(payload),options)); return 200
            with patch('eternalchat.push.post',post):
                self.assertEqual(await service.send({'kind':'apns','token':'a'*64},'turn-1'),200)
                self.assertEqual(await service.send({'kind':'apns','token':'a'*64},'turn-2'),200)
            endpoint,headers,payload,options = sent[0]
            self.assertEqual(endpoint,'https://api.sandbox.push.apple.com/3/device/'+'a'*64)
            self.assertEqual(options,{'http2':True})
            self.assertEqual(headers['apns-topic'],'org.eternalchat.companion')
            self.assertEqual(headers['apns-push-type'],'alert')
            self.assertEqual(payload,{'aps':{'alert':{'title':'EternalChat','body':'A reply is ready.'},'sound':'default'}})
            token = headers['authorization'].removeprefix('bearer ')
            head,claims,signature = token.split('.')
            decode=lambda value:base64.urlsafe_b64decode(value+'='*(-len(value)%4))
            raw=decode(signature)
            private.public_key().verify(utils.encode_dss_signature(int.from_bytes(raw[:32]),int.from_bytes(raw[32:])),f'{head}.{claims}'.encode(),ec.ECDSA(hashes.SHA256()))
            self.assertEqual(json.loads(decode(head)),{'alg':'ES256','kid':'KEY1234567'})
            self.assertEqual(json.loads(decode(claims))['iss'],'TEAM123456')
            self.assertEqual(sent[0][1]['authorization'],sent[1][1]['authorization'])
            with self.assertRaises(ValueError): await service.send({'kind':'apns','token':'/evil'},'turn')
            await service.close()

    async def test_invalid_config_is_redacted(self):
        with patch.dict(os.environ,ETERNALCHAT_APNS_KEY='/missing-private-secret-key',ETERNALCHAT_APNS_TEAM='invalid'):
            service=push.Push(lambda:[],lambda *_:None)
            state=service.native_status()
            self.assertFalse(state['configured'])
            self.assertNotIn('missing-private-secret',json.dumps(state))
