import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';

function findFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

async function waitForServer(baseUrl, child, timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`Server exited early with code ${child.exitCode}`);
    }
    try {
      const res = await fetch(`${baseUrl}/api/org`);
      if (res.ok) return;
    } catch {
      // Retry until ready
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error(`Timed out waiting for server at ${baseUrl}`);
}

async function main() {
  // 1. Validate client script inside public/web.html
  const html = fs.readFileSync('public/web.html', 'utf8');
  const scriptMatch = html.match(/<script>([\s\S]*)<\/script>/);
  assert.ok(scriptMatch, 'Expected inline <script> in public/web.html');
  new vm.Script(scriptMatch[1], { filename: 'public/web.html' });

  // 2. Boot GoraBot server against an isolated temp data file
  const port = await findFreePort();
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'gorabot-smoke-'));
  const dataFile = path.join(tempDir, 'state.json');
  const baseUrl = `http://127.0.0.1:${port}`;

  const child = spawn(process.execPath, ['server.js'], {
    env: {
      ...process.env,
      PORT: String(port),
      GORABOT_DATA_FILE: dataFile,
      ANTHROPIC_API_KEY: '',
      OPENAI_API_KEY: '',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  let logs = '';
  child.stdout.on('data', (chunk) => { logs += chunk.toString(); });
  child.stderr.on('data', (chunk) => { logs += chunk.toString(); });

  try {
    await waitForServer(baseUrl, child);

    // Phase 1: HTML & manifest
    const homeRes = await fetch(`${baseUrl}/`);
    assert.equal(homeRes.status, 200);
    const homeHtml = await homeRes.text();
    assert.ok(homeHtml.includes('GoraBot'), 'Landing HTML should include GoraBot');

    // Phase 2: Initial org state (Root only)
    const orgRes = await fetch(`${baseUrl}/api/org`);
    assert.equal(orgRes.status, 200);
    const org0 = await orgRes.json();
    assert.equal(org0.totals.uniis, 1);

    // Phase 3: Create child G & tracked watchlist item
    const createRes = await fetch(`${baseUrl}/api/org/unii`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ parentId: 'root', name: 'Calendar', charter: 'Schedule smoke test' }),
    });
    assert.equal(createRes.status, 200);
    const created = await createRes.json();
    assert.equal(created.unii.id, 'calendar');

    const itemRes = await fetch(`${baseUrl}/api/org/unii/calendar/items`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ title: 'Flight check-in', severity: 'action', needsAttention: true }),
    });
    assert.equal(itemRes.status, 200);

    // Phase 4: Pulse Root & check escalated alert visibility
    const pulseRes = await fetch(`${baseUrl}/api/org/pulse`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ targetId: 'root', force: true }),
    });
    assert.equal(pulseRes.status, 200);
    const pulse = await pulseRes.json();
    assert.equal(pulse.ok, true);
    assert.equal(pulse.awakened, 2);
    assert.ok(pulse.org.totals.openAttention >= 1);

    // Phase 5: Submit message to Calendar G & verify memory view
    const sendRes = await fetch(`${baseUrl}/api/send?unii=calendar`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ text: 'Check calendar status' }),
    });
    assert.equal(sendRes.status, 200);

    await new Promise((r) => setTimeout(r, 350));
    const viewRes = await fetch(`${baseUrl}/api/view?unii=calendar`);
    assert.equal(viewRes.status, 200);
    const viewData = await viewRes.json();
    assert.ok(Array.isArray(viewData.lines) && viewData.lines.length >= 2);

    console.log('GoraBot smoke check passed:', {
      port,
      uniis: pulse.org.totals.uniis,
      openAttention: pulse.org.totals.openAttention,
      calendarViewLines: viewData.lines.length,
    });
  } catch (err) {
    console.error('Smoke test failed. Server logs:\n', logs);
    throw err;
  } finally {
    child.kill('SIGTERM');
    await new Promise((r) => setTimeout(r, 200));
    if (child.exitCode === null) child.kill('SIGKILL');
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
