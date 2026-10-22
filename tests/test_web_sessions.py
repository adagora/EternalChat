"""Independent sessions, single-use pairing, revocation and scope over real HTTP."""
import asyncio
import io
import json
import time
import zipfile
from unittest.mock import patch

from eternalchat.remote import Client, RemoteError
from eternalchat.web_sessions import Sessions
from tests.test_web import WebFixture


class SessionTests(WebFixture):
    async def pair(self, label='Laptop', credential='t'*43):
        status, result, _ = await self.request('POST', '/api/sessions/open', {'label':label}, token=credential)
        self.assertEqual(status, 200, result)
        return result

    async def test_independent_credentials_single_use_invitation_and_backup_privacy(self):
        await self.serve()
        first = await self.pair()
        status, invite, _ = await self.request('POST', '/api/sessions/invite', {}, token=first['token'])
        self.assertEqual(status, 200)
        self.assertEqual((await self.request('GET','/api/state',token=invite['token']))[0],403)
        second = await self.pair('Phone', invite['token'])
        self.assertNotEqual(first['token'], second['token'])
        self.assertEqual((await self.request('POST','/api/sessions/open',{'label':'Clone'},token=invite['token']))[0],403)
        state = json.dumps(self.chat.engine.web_auth())
        for secret in [first['token'], second['token'], invite['token']]:
            self.assertNotIn(secret, state)
        listing = (await self.request('GET','/api/sessions',token=second['token']))[1]
        self.assertEqual(sum(s['current'] for s in listing['sessions']),1)
        self.assertEqual(listing['current'],second['id'])
        self.assertNotIn('digest',json.dumps(listing))
        raw = (await self.request('GET','/api/backup',token=first['token']))[1]
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertNotIn('web-auth.json',archive.namelist())

    async def test_saved_session_and_revocation_survive_server_reopen(self):
        await self.serve()
        session = await self.pair()
        private_state = self.chat.engine.store.root / "web-auth.json"
        self.assertEqual(private_state.stat().st_mode & 0o777, 0o600)
        await self.chat.close()
        await self.serve()
        self.assertEqual((await self.request('GET','/api/state',token=session['token']))[0],200)
        self.assertEqual((await self.request('POST','/api/sessions/revoke',{'id':session['id']}))[0],200)
        await self.chat.close()
        await self.serve()
        self.assertEqual((await self.request('GET','/api/state',token=session['token']))[0],403)

    async def test_revocation_closes_sse_and_denies_future_requests_only_for_target(self):
        await self.serve()
        first, second = await self.pair(), await self.pair('Phone')
        client = Client(f'http://127.0.0.1:{self.port}/#{second["token"]}')
        events = client.events()
        self.assertEqual((await anext(events))['kind'],'snapshot')
        status, result, _ = await self.request('POST','/api/sessions/revoke',{'id':second['id']},token=first['token'])
        self.assertEqual((status,result),(200,{'ok':True}))
        async with asyncio.timeout(2):
            try:
                await anext(events)
            except (StopAsyncIteration, OSError, RemoteError, asyncio.IncompleteReadError):
                pass
        await events.aclose()
        self.assertEqual((await self.request('GET','/api/state',token=second['token']))[0],403)
        self.assertEqual((await self.request('GET','/api/state',token=first['token']))[0],200)
        self.assertEqual((await self.request('POST','/api/sessions/revoke',{'id':first['id']},token=first['token']))[0],200)

    async def test_restart_rotation_expiry_and_bounds(self):
        await self.serve()
        session = await self.pair()
        restored = Sessions(lambda:self.chat.engine, 't'*43)
        self.assertIsNotNone(restored.authenticate(session['token']))
        self.assertIsNone(Sessions(lambda:self.chat.engine,'another-master').authenticate(session['token']))
        with patch('eternalchat.web_sessions.time.time',return_value=time.time()+91*86400):
            self.assertIsNone(restored.authenticate(session['token']))
        for bad in ['', 'x'*81, 'bad\nname', [], None]:
            self.assertEqual((await self.request('POST','/api/sessions/open',{'label':bad}))[0],400)
        with patch('eternalchat.web_sessions.LIMIT',1):
            self.assertEqual((await self.request('POST','/api/sessions/open',{'label':'More'}))[0],400)
        invitation = (await self.request('POST','/api/sessions/invite',{}))[1]
        with patch('eternalchat.web_sessions.time.time',return_value=time.time()+601):
            self.assertEqual((await self.request('POST','/api/sessions/open',{'label':'Expired'},token=invitation['token']))[0],403)

    async def test_real_device_client_runs_with_scoped_credential_and_exits_after_revoke(self):
        from eternalchat.device_client import DeviceClient
        from eternalchat.devices import Devices
        from eternalchat.machine import Machine
        from tests.test_web import Quiet
        await self.serve()
        server = Machine(self.temp.name + '/server', name='server')
        self.chat.engine.machine = Devices(server)
        self.chat.engine.machine.restore_remotes(self.chat.remotes)
        credential = (await self.request('POST','/api/sessions/computer',{'label':'Scoped laptop'}))[1]
        laptop = Machine(self.temp.name + '/laptop', name='laptop')
        client = DeviceClient(f'http://127.0.0.1:{self.port}/#{credential["token"]}',laptop,Quiet())
        task = asyncio.create_task(client.run())
        async def cleanup():
            task.cancel(); await asyncio.gather(task, return_exceptions=True); server.close()
        self.addAsyncCleanup(cleanup)
        async def joined(): return 'laptop' in self.chat.remotes
        await self.until(joined)
        remote = self.chat.remotes['laptop']
        value, failed = await asyncio.wait_for(remote.run('shell',{'command':'printf scoped > proof.txt; cat proof.txt'},'main'),5)
        self.assertFalse(failed,value)
        self.assertIn('scoped',value)
        self.assertEqual((await self.request('POST','/api/sessions/revoke',{'id':credential['id']}))[0],200)
        with self.assertRaises(RemoteError): await asyncio.wait_for(task,5)
        self.assertTrue(remote.gone)

    async def test_computer_credential_cannot_read_chat_or_target_another_name(self):
        from eternalchat.devices import Devices
        from eternalchat.machine import Machine
        await self.serve()
        machine = Machine(self.temp.name)
        self.chat.engine.machine = Devices(machine)
        self.chat.engine.machine.restore_remotes(self.chat.remotes)
        status, credential, _ = await self.request('POST','/api/sessions/computer',{'label':'Remote laptop'})
        self.assertEqual(status,200)
        token = credential['token']
        for path in ['/api/state','/api/messages','/api/backup','/api/sessions']:
            self.assertEqual((await self.request('GET',path,token=token))[0],403)
        for malformed in [[], {'name':None}, {'name':4}, {'name':{}}, {'name':True}]:
            self.assertEqual((await self.request('POST','/api/devices/hello',malformed,token=token))[0],400)
        hello = {'name':'laptop','work':'/tmp/work','computer':False,'identity':'a'*32,'session':'s'*43}
        self.assertEqual((await self.request('POST','/api/devices/hello',hello,token=token))[0],200)
        self.assertEqual((await self.request('POST','/api/devices/hello',{**hello,'name':'other'},token=token))[0],403)
        self.assertEqual((await self.request('POST','/api/send',{'text':'no'},token=token))[0],403)
        status, _, _ = await self.request('POST','/api/sessions/revoke',{'id':credential['id']})
        self.assertEqual(status,200)
        self.assertEqual((await self.request('POST','/api/devices/hello',hello,token=token))[0],403)
        self.assertTrue(self.chat.remotes['laptop'].gone)
        machine.close()
