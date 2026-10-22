// Execute the shipped worker with explicit Push/Notification fixtures; no vendor delivery claim.
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';
const handlers={}, notifications=[], opened=[];
const self={addEventListener:(name,handler)=>handlers[name]=handler,
  registration:{showNotification:async(title,options)=>notifications.push({title,...options})},
  clients:{matchAll:async()=>[],openWindow:async(url)=>opened.push(url)}};
vm.runInNewContext(await readFile(new URL('../eternalchat/static/sw.js',import.meta.url),'utf8'),{self,URL,location:{origin:'https://chat.example.test'},console});
let pending;
handlers.push({data:{json:()=>({id:'9',body:'must not display sensitive payload',url:'https://evil.test'})},waitUntil:promise=>pending=promise});
await pending;
assert.equal(notifications[0].body,'A reply is ready.');
assert.equal(notifications[0].tag,'eternalchat-reply');
assert.ok(!JSON.stringify(notifications).includes('sensitive'));
handlers.push({data:{json:()=>{throw new Error('malformed');}},waitUntil:promise=>pending=promise});
await pending;
assert.equal(notifications.length,2);
handlers.notificationclick({notification:{close(){}},waitUntil:promise=>pending=promise});
await pending;
assert.deepEqual(opened,['/']);
console.log('Worker fixtures passed: generic push, malformed payload fallback, fixed-origin notification click. No live delivery tested.');
