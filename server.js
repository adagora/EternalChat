import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import express from 'express';
import { appendRecord, renderViewLines, zoomRange } from './lib/memory-tree.js';
import { OrgEngine, nowIso } from './lib/engine.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT || 3000);
const HOST = '0.0.0.0';

const app = express();
app.use(express.json({ limit: '16mb' }));

const engine = new OrgEngine({
  autoPulse: true,
  pulseIntervalMs: 45_000,
  dataFile: process.env.GORABOT_DATA_FILE || path.join(__dirname, '.data', 'uniibot-state.json'),
});

function uniiOf(req) {
  const raw = req.query?.unii || req.body?.unii || 'root';
  return String(raw || 'root').trim() || 'root';
}

// ---------------------------------------------------------------------------
// Static & Page Routes
// ---------------------------------------------------------------------------

const staticFiles = {
  '/manifest.webmanifest': 'application/manifest+json',
  '/sw.js': 'text/javascript; charset=utf-8',
  '/icon.svg': 'image/svg+xml',
  '/icon-192.png': 'image/png',
  '/icon-512.png': 'image/png',
  '/icon-maskable.png': 'image/png',
  '/apple-touch-icon.png': 'image/png',
};

app.get('/', (req, res) => {
  res.setHeader('Content-Type', 'text/html; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  res.sendFile(path.join(__dirname, 'public', 'web.html'));
});

for (const [route, contentType] of Object.entries(staticFiles)) {
  app.get(route, (req, res) => {
    const filePath = path.join(__dirname, 'public', 'static', route.slice(1));
    if (!fs.existsSync(filePath)) return res.status(404).end();
    res.setHeader('Content-Type', contentType);
    res.sendFile(filePath);
  });
}

// ---------------------------------------------------------------------------
// Sessions & Auth
// ---------------------------------------------------------------------------

function createSession(label = 'Browser') {
  const id = crypto.randomBytes(8).toString('hex');
  const token = `ecs.${crypto.randomBytes(24).toString('hex')}`;
  const session = {
    id,
    token,
    label,
    created: Math.floor(Date.now() / 1000),
    created_at: nowIso(),
    last_used: nowIso(),
    scope: 'browser',
    push: false,
  };
  engine.sessions.set(id, session);
  return session;
}

app.post('/api/sessions/open', (req, res) => {
  const label = typeof req.body?.label === 'string' ? req.body.label : 'Browser';
  const session = createSession(label);
  res.json({ id: session.id, token: session.token });
});

app.get('/api/sessions', (req, res) => {
  const token = req.headers['x-gorabot-token'] || req.headers['x-goramind-token'] || req.headers['x-eternalchat-token'];
  const list = Array.from(engine.sessions.values()).map((s) => ({
    id: s.id,
    label: s.label,
    created: s.created,
    created_at: s.created_at,
    last_used: s.last_used,
    scope: s.scope,
    push: s.push,
    current: s.token === token,
  }));
  const current = list.find((s) => s.current)?.id || list[0]?.id || 'local';
  res.json({ current, sessions: list });
});

app.post('/api/sessions/invite', (req, res) => {
  const token = crypto.randomBytes(16).toString('hex');
  res.json({ token, expires_in: 600 });
});

app.post('/api/sessions/computer', (req, res) => {
  const token = crypto.randomBytes(16).toString('hex');
  res.json({ token, label: req.body?.label || 'Computer' });
});

app.post('/api/sessions/revoke', (req, res) => {
  if (req.body?.id) engine.sessions.delete(req.body.id);
  res.json({ ok: true });
});

// ---------------------------------------------------------------------------
// Hierarchical Org & Heartbeat Endpoints
// ---------------------------------------------------------------------------

app.get('/api/org', (req, res) => {
  res.json(engine.orgSnapshot());
});

app.post('/api/org/pulse', async (req, res) => {
  try {
    const targetId = req.body?.targetId ? String(req.body.targetId) : 'root';
    const force = req.body?.force !== false;
    const result = await engine.pulseOrg({ reason: 'manual', force, targetId });
    res.json({ ...result, org: engine.orgSnapshot() });
  } catch (err) {
    res.status(400).json({ error: err.message });
  }
});

app.post('/api/org/heartbeat', (req, res) => {
  if (typeof req.body?.enabled === 'boolean') {
    engine.autoPulse.enabled = req.body.enabled;
  }
  if (typeof req.body?.intervalMs === 'number' && req.body.intervalMs >= 10_000) {
    engine.autoPulse.intervalMs = req.body.intervalMs;
  }
  engine._startTimer();
  engine._schedulePersist();
  engine.publish('org', engine.orgSnapshot(), 'root');
  res.json({ ok: true, heartbeat: engine.orgSnapshot().heartbeat });
});

app.post('/api/org/unii', (req, res) => {
  try {
    const parentId = typeof req.body?.parentId === 'string' && req.body.parentId.trim()
      ? req.body.parentId.trim()
      : 'root';
    const name = req.body?.name;
    const charter = req.body?.charter || '';
    const cadenceMs = Number(req.body?.cadenceMs) || 60_000;
    const node = engine.createSubUnii({ parentId, name, charter, cadenceMs });
    res.json({ ok: true, unii: node, org: engine.orgSnapshot() });
  } catch (err) {
    res.status(400).json({ error: err.message });
  }
});

app.patch('/api/org/unii/:id', (req, res) => {
  try {
    const node = engine.updateSubUnii(req.params.id, {
      name: req.body?.name,
      charter: req.body?.charter,
      cadenceMs: req.body?.cadenceMs !== undefined ? Number(req.body.cadenceMs) : undefined,
      domain: req.body?.domain,
    });
    res.json({ ok: true, unii: node, org: engine.orgSnapshot() });
  } catch (err) {
    res.status(400).json({ error: err.message });
  }
});

app.delete('/api/org/unii/:id', (req, res) => {
  try {
    const result = engine.deleteSubUnii(req.params.id);
    res.json({ ok: true, ...result, org: engine.orgSnapshot() });
  } catch (err) {
    res.status(400).json({ error: err.message });
  }
});

app.post('/api/org/unii/:id/items', (req, res) => {
  try {
    const item = engine.addDomainItem(req.params.id, {
      title: req.body?.title,
      severity: req.body?.severity,
      needsAttention: req.body?.needsAttention !== false,
    });
    res.json({ ok: true, item, org: engine.orgSnapshot() });
  } catch (err) {
    res.status(400).json({ error: err.message });
  }
});

app.delete('/api/org/unii/:id/items/:itemId', (req, res) => {
  try {
    const removed = engine.removeDomainItem(req.params.id, req.params.itemId);
    res.json({ ok: true, removed, org: engine.orgSnapshot() });
  } catch (err) {
    res.status(404).json({ error: err.message });
  }
});

app.post('/api/org/reset', (req, res) => {
  const org = engine.resetOrg();
  res.json({ ok: true, org });
});

app.post('/api/org/delegate', async (req, res) => {
  try {
    const fromId = String(req.body?.fromId || 'root');
    const toId = String(req.body?.toId || '');
    const text = String(req.body?.text || '').trim();
    if (!toId || !text) return res.status(400).json({ error: 'Provide target child G and message' });
    engine.sendParentChildMessage(fromId, toId, text);
    const awakened = await engine.awakenUnii(toId, { reason: 'parent_call', directive: text, forceChildren: true });
    engine._schedulePersist();
    res.json({ ok: true, awakened, org: engine.orgSnapshot() });
  } catch (err) {
    res.status(403).json({ error: err.message });
  }
});

app.post('/api/org/attention/:id/resolve', (req, res) => {
  try {
    const item = engine.resolveAttention(req.params.id);
    res.json({ ok: true, item, org: engine.orgSnapshot() });
  } catch (err) {
    res.status(404).json({ error: err.message });
  }
});

// ---------------------------------------------------------------------------
// Core Chat, Memory, Zoom, SSE, Agents, Settings APIs (scoped by ?unii=)
// ---------------------------------------------------------------------------

app.get('/api/events', (req, res) => {
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('Connection', 'keep-alive');
  res.flushHeaders?.();
  res.write(': connected\n\n');

  engine.sseClients.add(res);
  const ping = setInterval(() => {
    try {
      res.write(': ping\n\n');
    } catch {
      clearInterval(ping);
      engine.sseClients.delete(res);
    }
  }, 15_000);

  req.on('close', () => {
    clearInterval(ping);
    engine.sseClients.delete(res);
  });
});

app.get('/api/state', (req, res) => {
  try {
    res.json(engine.uniiState(uniiOf(req)));
  } catch (err) {
    res.status(404).json({ error: err.message });
  }
});

app.get('/api/messages', (req, res) => {
  try {
    const store = engine.getStore(uniiOf(req));
    const start = Math.max(0, Number.parseInt(req.query.from ?? '0', 10) || 0);
    const limit = Math.max(1, Math.min(Number.parseInt(req.query.limit ?? '200', 10) || 200, 500));
    res.json({ messages: store.messages.slice(start, start + limit) });
  } catch (err) {
    res.status(404).json({ error: err.message });
  }
});

app.get('/api/view', (req, res) => {
  try {
    const store = engine.getStore(uniiOf(req));
    res.json({ lines: renderViewLines(store) });
  } catch (err) {
    res.status(404).json({ error: err.message });
  }
});

app.get('/api/date', (req, res) => {
  try {
    const store = engine.getStore(uniiOf(req));
    const id = Number.parseInt(req.query.id ?? '0', 10);
    const msg = store.messages[id];
    if (!msg) return res.status(404).json({ error: `Message ${id} not found` });
    res.json({ text: `Message ${id} was logged at ${msg.date}` });
  } catch (err) {
    res.status(404).json({ error: err.message });
  }
});

app.get('/api/zoom', (req, res) => {
  try {
    const idParam = uniiOf(req);
    if (req.query.name) {
      const agent = engine.getAgentMap(idParam).get(String(req.query.name));
      if (!agent) return res.status(404).json({ error: `Agent ${req.query.name} not found` });
      const text = agent.records.map((m) => `[${m.kind}] ${m.text}`).join('\n');
      return res.json({ text, lines: [], images: [] });
    }
    const store = engine.getStore(idParam);
    const id = Number.parseInt(req.query.id ?? '0', 10) || 0;
    const n = Number.parseInt(req.query.n ?? '1', 10) || 1;
    const found = zoomRange(store, id, n);
    if (!found) return res.status(404).json({ error: `Message ${id} not found` });
    res.json(found);
  } catch (err) {
    res.status(404).json({ error: err.message });
  }
});

app.post('/api/send', async (req, res) => {
  try {
    const uniiId = uniiOf(req);
    const text = typeof req.body?.text === 'string' ? req.body.text : '';
    const attachments = Array.isArray(req.body?.attachments) ? req.body.attachments : [];
    if (!text.trim() && !attachments.length) {
      return res.status(400).json({ error: 'Type a message or attach an image' });
    }
    await engine.submitMessage(uniiId, text, attachments);
    res.json({ ok: true });
  } catch (err) {
    res.status(400).json({ error: err.message });
  }
});

app.post('/api/note', (req, res) => {
  try {
    const uniiId = uniiOf(req);
    const text = typeof req.body?.text === 'string' ? req.body.text : '';
    if (!text.trim()) return res.status(400).json({ error: 'Type the note to save' });
    const store = engine.getStore(uniiId);
    const msg = appendRecord(store, 'note', text);
    engine.publish('message', { i: msg.i, uniiId }, uniiId);
    res.json({ ids: [msg.i] });
  } catch (err) {
    res.status(400).json({ error: err.message });
  }
});

app.get('/api/agent', (req, res) => {
  try {
    const name = String(req.query.name || '');
    const agent = engine.getAgentMap(uniiOf(req)).get(name);
    if (!agent) return res.status(404).json({ error: `Agent ${name} not found` });
    res.json(agent);
  } catch (err) {
    res.status(404).json({ error: err.message });
  }
});

app.post('/api/agent', (req, res) => {
  try {
    const uniiId = uniiOf(req);
    engine.startAgent(uniiId, req.body?.name, req.body?.task);
    res.json({ ok: true });
  } catch (err) {
    res.status(400).json({ error: err.message });
  }
});

app.post('/api/stop', (req, res) => {
  try {
    const uniiId = uniiOf(req);
    engine.stopWork(uniiId, req.body?.name || null);
    res.json({ ok: true });
  } catch (err) {
    res.status(400).json({ error: err.message });
  }
});

app.post('/api/goal', async (req, res) => {
  try {
    const uniiId = uniiOf(req);
    const text = typeof req.body?.text === 'string' ? req.body.text.trim() : '';
    if (!text) return res.status(400).json({ error: 'A goal needs its text' });
    await engine.setGoal(uniiId, text);
    res.json({ ok: true });
  } catch (err) {
    res.status(400).json({ error: err.message });
  }
});

app.post('/api/halt', (req, res) => {
  try {
    engine.haltGoal(uniiOf(req));
    res.json({ ok: true });
  } catch (err) {
    res.status(400).json({ error: err.message });
  }
});

app.post('/api/compact', (req, res) => {
  try {
    res.json(engine.compact(uniiOf(req)));
  } catch (err) {
    res.status(400).json({ error: err.message });
  }
});

app.post('/api/settings', (req, res) => {
  try {
    const allowed = [
      'connection', 'cli', 'model', 'summary_model', 'agent_model',
      'reasoning_effort', 'tools', 'work', 'api_provider', 'api_speed',
      'service_tier', 'after_turn',
    ];
    for (const key of allowed) {
      if (typeof req.body?.[key] === 'string') {
        engine.settings[key] = req.body[key].trim();
      }
    }
    const uniiId = uniiOf(req);
    const snap = engine.uniiState(uniiId);
    engine.publish('settings', snap.settings, uniiId);
    res.json(snap);
  } catch (err) {
    res.status(404).json({ error: err.message });
  }
});

// Attachments
app.post('/api/attach', express.raw({ type: '*/*', limit: '6mb' }), (req, res) => {
  const buf = Buffer.isBuffer(req.body) ? req.body : Buffer.from(req.body || '');
  const mediaType = req.headers['content-type'] || 'image/png';
  const sha256 = crypto.createHash('sha256').update(buf).digest('hex');
  engine.images.set(sha256, { buf, media_type: mediaType });
  res.json({ ref: { sha256, media_type: mediaType, bytes: buf.length } });
});

app.get('/api/image/:sha', (req, res) => {
  const item = engine.images.get(req.params.sha);
  if (!item) return res.status(404).end();
  res.setHeader('Content-Type', item.media_type);
  res.send(item.buf);
});

// Push & Import stubs
app.get('/api/push', (req, res) => res.json({ configured: false, public_key: null, subscribed: false }));
app.get('/api/native-push', (req, res) => res.json({ configured: false }));
app.post('/api/push/subscribe', (req, res) => res.status(400).json({ error: 'Background push is not configured on this server' }));
app.post('/api/push/unsubscribe', (req, res) => res.json({ ok: true }));
app.post('/api/push/test', (req, res) => res.status(400).json({ error: 'Background push is not configured on this server' }));
app.post('/api/transcribe', (req, res) => res.status(400).json({ error: 'Recorded transcription is disabled in offline demo mode' }));
app.get('/api/import', (req, res) => res.json({ sessions: [] }));
app.post('/api/import', (req, res) => res.json({ imported: 0 }));
app.post('/api/import/batch', (req, res) => res.json({ id: 'demo', batch: 'demo', sessions: [] }));
app.get('/api/import/batch', (req, res) => res.json({ batch: req.query.batch || 'demo', status: 'done', imported: 0 }));
app.post('/api/import/upload', (req, res) => res.json({ sessions: [] }));
app.post('/api/import/start', (req, res) => res.json({ ok: true }));
app.post('/api/import/cancel', (req, res) => res.json({ ok: true }));
app.get('/api/backup', (req, res) => {
  try {
    const uniiId = uniiOf(req);
    const store = engine.getStore(uniiId);
    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Content-Disposition', `attachment; filename="gorabot-${uniiId}-backup.json"`);
    res.json({ uniiId, messages: store.messages, view: renderViewLines(store) });
  } catch (err) {
    res.status(404).json({ error: err.message });
  }
});

app.listen(PORT, HOST, () => {
  console.log(`GoraBot Hierarchical Memory server listening on http://${HOST}:${PORT}`);
});
