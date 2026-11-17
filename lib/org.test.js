import test from 'node:test';
import assert from 'node:assert/strict';
import {
  createMemoryStore,
  appendRecord,
  renderViewLines,
  zoomRange,
  computeStats,
} from './memory-tree.js';
import {
  createOrgRegistry,
  addUnii,
  canCommunicate,
  computeFreshness,
  selectDueChildren,
  FRESHNESS,
} from './org-tree.js';
import { OrgEngine } from './engine.js';

test('strict parent-child communication boundaries', () => {
  const reg = createOrgRegistry();
  addUnii(reg, { id: 'root', name: 'Root', parentId: null });
  addUnii(reg, { id: 'projects', name: 'Projects', parentId: 'root' });
  addUnii(reg, { id: 'calendar', name: 'Calendar', parentId: 'root' });
  addUnii(reg, { id: 'bend', name: 'Bend', parentId: 'projects' });
  addUnii(reg, { id: 'hvm', name: 'HVM', parentId: 'projects' });

  // Human <-> Root allowed; Human <-> leaf blocked for org routing
  assert.equal(canCommunicate(reg, 'human', 'root'), true);
  assert.equal(canCommunicate(reg, 'root', 'human'), true);
  assert.equal(canCommunicate(reg, 'human', 'bend'), false);

  // Parent <-> direct child allowed
  assert.equal(canCommunicate(reg, 'root', 'projects'), true);
  assert.equal(canCommunicate(reg, 'projects', 'root'), true);
  assert.equal(canCommunicate(reg, 'projects', 'bend'), true);
  assert.equal(canCommunicate(reg, 'bend', 'projects'), true);

  // Siblings, grandparents, and cross-branch blocked
  assert.equal(canCommunicate(reg, 'projects', 'calendar'), false);
  assert.equal(canCommunicate(reg, 'bend', 'hvm'), false);
  assert.equal(canCommunicate(reg, 'root', 'bend'), false);
  assert.equal(canCommunicate(reg, 'calendar', 'bend'), false);
});

test('per-Unii binary summary tree builds parents and zooms cleanly', () => {
  const store = createMemoryStore();
  appendRecord(store, 'user', 'First message about calendar');
  appendRecord(store, 'unii', 'Second message confirming meeting');
  appendRecord(store, 'user', 'Third message about flights');
  appendRecord(store, 'unii', 'Fourth message with confirmation');

  assert.equal(store.messages.length, 4);
  // 4 leaves + 2 level-1 nodes + 1 level-2 root = 7 nodes
  assert.equal(store.nodes.size, 7);

  const stats = computeStats(store);
  assert.equal(stats.messages, 4);
  assert.equal(stats.nodes, 7);

  const zoomed4 = zoomRange(store, 0, 4);
  assert.equal(zoomed4.lines.length, 2);
  assert.equal(zoomed4.lines[0].id, 0);
  assert.equal(zoomed4.lines[0].n, 2);
  assert.equal(zoomed4.lines[1].id, 2);
  assert.equal(zoomed4.lines[1].n, 2);

  const leaf = zoomRange(store, 2, 1);
  assert.equal(leaf.text, 'Third message about flights');
  assert.equal(renderViewLines(store).length, 4);
});

test('freshness and due-children scheduling track staleness accurately', () => {
  const reg = createOrgRegistry();
  const now = 1_000_000;
  addUnii(reg, { id: 'root', name: 'Root', parentId: null, cadenceMs: 10_000, now });
  const freshChild = addUnii(reg, { id: 'fresh', name: 'Fresh', parentId: 'root', cadenceMs: 10_000, now });
  freshChild.lastAwakenedAt = now - 5_000;
  const staleChild = addUnii(reg, { id: 'stale', name: 'Stale', parentId: 'root', cadenceMs: 10_000, now });
  staleChild.lastAwakenedAt = now - 30_000;
  const dormantChild = addUnii(reg, { id: 'dormant', name: 'Dormant', parentId: 'root', cadenceMs: 10_000, now });

  assert.equal(computeFreshness(freshChild, now), FRESHNESS.FRESH);
  assert.equal(computeFreshness(staleChild, now), FRESHNESS.STALE);
  assert.equal(computeFreshness(dormantChild, now), FRESHNESS.DORMANT);

  const due = selectDueChildren(reg, 'root', now).map((c) => c.id);
  assert.deepEqual(due, ['stale', 'dormant']);
});

test('OrgEngine starts clean with only Root, supports CRUD/items, enforces parent-child IPC, and cascades recursive heartbeat', async () => {
  const engine = new OrgEngine({ autoPulse: false, skipTimer: true });

  // Clean boot: ONLY Root exists (no hardcoded sub-Uniis or fake alerts)
  assert.equal(engine.registry.uniis.size, 1);
  assert.ok(engine.getNode('root'));
  assert.equal(engine.listAttention({ resolved: false }).length, 0);

  // Dynamically build a multi-level org hierarchy
  const calendar = engine.createSubUnii({ parentId: 'root', name: 'Calendar', charter: 'Owns daily schedule.' });
  const fundraise = engine.createSubUnii({ parentId: 'root', name: 'Fundraise', charter: 'Investor pipeline.' });
  const projects = engine.createSubUnii({ parentId: 'root', name: 'Personal Projects', charter: 'Engineering lead.' });
  const bend = engine.createSubUnii({ parentId: projects.id, name: 'Bend', charter: 'Compiler maintenance.' });
  const hvm = engine.createSubUnii({ parentId: projects.id, name: 'HVM', charter: 'Runtime benchmarks.' });

  // Track a domain item inside Bend (bubbles attention up)
  const item = engine.addDomainItem(bend.id, { title: 'Review parallel fold PR', severity: 'action', needsAttention: true });
  assert.equal(engine.listAttention({ resolved: false }).length, 1);

  // Hierarchy violation throws
  assert.throws(
    () => engine.sendParentChildMessage(calendar.id, fundraise.id, 'Hey sibling'),
    /Hierarchy violation/
  );
  assert.throws(
    () => engine.sendParentChildMessage('root', bend.id, 'Skip projects'),
    /Hierarchy violation/
  );

  // Valid parent -> child IPC succeeds and stays isolated from unrelated Uniis
  const calBefore = engine.getStore(calendar.id).messages.length;
  const fndBefore = engine.getStore(fundraise.id).messages.length;
  engine.sendParentChildMessage('root', calendar.id, 'Check tomorrow schedule');
  assert.equal(engine.getStore(calendar.id).messages.length, calBefore + 1);
  assert.equal(engine.getStore(fundraise.id).messages.length, fndBefore);

  // Pulsing Root recursively awakens all 6 Uniis (root + 3 direct + 2 nested)
  // without polluting chat streams with noisy Heartbeat report cards or echoes
  const rootMsgsBefore = engine.getStore('root').messages.length;
  const fndMsgsBefore = engine.getStore(fundraise.id).messages.length;
  const pulse = await engine.pulseOrg({ reason: 'heartbeat', force: true });
  assert.equal(pulse.ok, true);
  assert.equal(pulse.awakened, 6);
  assert.ok(engine.getNode(bend.id).wakeCount >= 1);
  assert.ok(engine.getNode(hvm.id).wakeCount >= 1);
  assert.equal(engine.getStore('root').messages.length, rootMsgsBefore);
  assert.equal(engine.getStore(fundraise.id).messages.length, fndMsgsBefore);
  assert.equal(engine.agents.get('root').has('Heartbeat'), false);

  // Update a Unii's charter and cadence
  engine.updateSubUnii(calendar.id, { charter: 'Updated calendar charter', cadenceMs: 30_000 });
  assert.equal(engine.getNode(calendar.id).charter, 'Updated calendar charter');
  assert.equal(engine.getNode(calendar.id).cadenceMs, 30_000);

  // Deleting an intermediate parent cascades cleanly to descendants and alerts
  const del = engine.deleteSubUnii(projects.id);
  assert.deepEqual(del.removedIds.sort(), [bend.id, hvm.id, projects.id].sort());
  assert.equal(engine.registry.uniis.has(bend.id), false);
  assert.equal(engine.listAttention({ resolved: false }).length, 0);

  // Tracked item removal & attention resolution work cleanly
  const calItem = engine.addDomainItem(calendar.id, { title: 'Prep partner brief', severity: 'urgent', needsAttention: true });
  const alerts = engine.listAttention({ resolved: false });
  assert.equal(alerts.length, 1);
  engine.resolveAttention(alerts[0].id);
  assert.equal(engine.listAttention({ resolved: false }).length, 0);
  engine.removeDomainItem(calendar.id, calItem.id);

  // Reset restores clean Root-only org
  engine.resetOrg();
  assert.equal(engine.registry.uniis.size, 1);
  assert.ok(item.id);

  engine.close();
});

test('zoomRange handles unaligned boundary IDs and invalid ranges cleanly', () => {
  const store = createMemoryStore();
  for (let i = 0; i < 8; i++) appendRecord(store, 'user', `Entry ${i}`);

  // Aligned zoom at id=0, n=4 returns level-1 blocks [0+2, 2+2]
  const z0 = zoomRange(store, 0, 4);
  assert.equal(z0.lines.length, 2);
  assert.equal(z0.lines[0].id, 0);
  assert.equal(z0.lines[1].id, 2);

  // Unaligned id=2 inside 0..3 still aligns cleanly to block [0+2, 2+2]
  const z2 = zoomRange(store, 2, 4);
  assert.equal(z2.lines[0].id, 0);
  assert.equal(z2.lines[1].id, 2);

  // Second 4-msg block id=4, n=4 returns [4+2, 6+2]
  const z4 = zoomRange(store, 4, 4);
  assert.equal(z4.lines[0].id, 4);
  assert.equal(z4.lines[1].id, 6);

  // Out-of-bounds and negative IDs return null instead of clamping or returning empty blocks
  assert.equal(zoomRange(store, 99, 4), null);
  assert.equal(zoomRange(store, -1, 1), null);
  assert.equal(zoomRange(store, -2, 4), null);
});

test('addUnii rejects same-parent duplicate names, disambiguates cross-parent names, and supports non-ASCII', () => {
  const reg = createOrgRegistry();
  addUnii(reg, { id: 'root', name: 'Root', parentId: null });
  addUnii(reg, { id: 'bend', name: 'Bend', parentId: 'root' });
  addUnii(reg, { id: 'hvm', name: 'HVM', parentId: 'root' });

  // Disambiguates identical auto-generated IDs under different parents
  const n1 = addUnii(reg, { name: 'Notes', parentId: 'bend' });
  const n2 = addUnii(reg, { name: 'Notes', parentId: 'hvm' });
  assert.equal(n1.id, 'notes');
  assert.equal(n2.id, 'hvm-notes');

  // Rejects duplicate sibling name under the same parent
  assert.throws(
    () => addUnii(reg, { name: 'notes', parentId: 'bend' }),
    /already exists under Bend/
  );

  // Non-ASCII name falls back to safe sequential ID while preserving display name
  const jp = addUnii(reg, { name: '日本語', parentId: 'root' });
  assert.ok(/^unii-\d+$/.test(jp.id));
  assert.equal(jp.name, '日本語');
});

test('selectDueChildren wakes fresh intermediate parent when a nested grandchild is dormant or overdue', () => {
  const reg = createOrgRegistry();
  const now = 5_000_000;
  addUnii(reg, { id: 'root', name: 'Root', parentId: null, cadenceMs: 60_000, now });
  const parent = addUnii(reg, { id: 'projects', name: 'Projects', parentId: 'root', cadenceMs: 60_000, now });
  parent.lastAwakenedAt = now - 5_000; // fresh itself
  const child = addUnii(reg, { id: 'bend', name: 'Bend', parentId: 'projects', cadenceMs: 60_000, now });
  child.lastAwakenedAt = now - 5_000; // also fresh
  // Dormant grandchild added under Bend
  addUnii(reg, { id: 'parser', name: 'Parser', parentId: 'bend', cadenceMs: 60_000, now });

  const due = selectDueChildren(reg, 'root', now, { force: false }).map((c) => c.id);
  assert.deepEqual(due, ['projects']);
});

test('OrgEngine word-boundary delegation, human<->root IPC, and message role normalization', async () => {
  const engine = new OrgEngine({ autoPulse: false, skipTimer: true });
  engine.createSubUnii({ parentId: 'root', name: 'Art', charter: 'Curates art.' });
  const rootNode = engine.getNode('root');
  const rootStore = engine.getStore('root');

  // Human -> Root IPC allowed
  const ipcMsg = engine.sendParentChildMessage('human', 'root', 'Focus on art today');
  assert.ok(ipcMsg.text.includes('[From parent Human] Focus on art today'));

  // "start project" must NOT wake "Art"
  const noWake = await engine._delegateMatchedChildren(rootNode, rootStore, 'Let us start working on docs', new AbortController().signal);
  assert.equal(noWake.length, 0);
  assert.equal(engine.getNode('art').wakeCount, 0);

  // "check art" MUST wake "Art"
  const wakeArt = await engine._delegateMatchedChildren(rootNode, rootStore, 'Check art deadlines', new AbortController().signal);
  assert.equal(wakeArt.length, 1);
  assert.equal(wakeArt[0].uniiId, 'art');
  assert.equal(engine.getNode('art').wakeCount, 1);

  // Conversation normalization drops leading assistant turns and merges consecutive same-role turns
  const testStore = createMemoryStore();
  appendRecord(testStore, 'unii', 'Initial assistant greeting');
  appendRecord(testStore, 'user', 'First user question');
  appendRecord(testStore, 'user', 'Followup before reply');
  appendRecord(testStore, 'unii', 'Assistant reply');
  const norm = engine._recentConversationMessages(testStore);
  assert.equal(norm.length, 2);
  assert.equal(norm[0].role, 'user');
  assert.equal(norm[0].content, 'First user question\n\nFollowup before reply');
  assert.equal(norm[1].role, 'assistant');

  engine.close();
});

test('tree-view filtering, collapse flattening, and orgSnapshot chat previews work accurately', async () => {
  const { formatMessagePreview, branchMatchesFilter, flattenVisibleTree } = await import('./org-tree.js');

  assert.equal(formatMessagePreview('**Hello** `world`   \n\nnext line'), 'Hello world next line');
  assert.equal(formatMessagePreview('a'.repeat(90), 20).length, 20);

  const engine = new OrgEngine({ autoPulse: false, skipTimer: true });
  const projects = engine.createSubUnii({ parentId: 'root', name: 'Personal Projects', charter: 'Engineering builds.' });
  const bend = engine.createSubUnii({ parentId: projects.id, name: 'Bend', charter: 'Compiler work.' });
  const dormant = engine.createSubUnii({ parentId: 'root', name: 'Quiet Branch', charter: 'Idle.' });
  // Clear auto creation note on dormant to test true 0-msg inactive filtering
  engine.getStore(dormant.id).messages = [];

  engine.addDomainItem(bend.id, { title: 'Fix type inference', severity: 'action', needsAttention: true });

  const snap = engine.orgSnapshot();
  const byId = new Map(snap.uniis.map((u) => [u.id, u]));
  assert.ok(byId.get(bend.id).lastMessagePreview.includes('Fix type inference'));
  assert.equal(byId.get(bend.id).lastMessageRole, 'note');
  assert.ok(byId.get(bend.id).lastMessageAt);
  assert.equal(byId.get(dormant.id).isActive, false);
  assert.equal(byId.get(bend.id).isActive, true);
  assert.ok(snap.totals.activeUniis >= 2);

  // Active filter keeps Bend and its ancestor Personal Projects, excludes dormant Quiet Branch
  assert.equal(branchMatchesFilter(snap.tree.children.find((c) => c.id === projects.id), byId, { mode: 'active' }), true);
  assert.equal(branchMatchesFilter(snap.tree.children.find((c) => c.id === dormant.id), byId, { mode: 'active' }), false);

  // Attention filter keeps Personal Projects because descendant Bend has open alert
  assert.equal(branchMatchesFilter(snap.tree.children.find((c) => c.id === projects.id), byId, { mode: 'attention' }), true);
  assert.equal(branchMatchesFilter(snap.tree.children.find((c) => c.id === dormant.id), byId, { mode: 'attention' }), false);

  // Query matches name, charter, OR last chat history preview
  assert.equal(branchMatchesFilter(snap.tree, byId, { query: 'type inference' }), true);
  assert.equal(branchMatchesFilter(snap.tree, byId, { query: 'Compiler' }), true);

  // Collapse flattening skips children of collapsed nodes
  assert.deepEqual(flattenVisibleTree(snap.tree, new Set()), ['root', projects.id, bend.id, dormant.id]);
  assert.deepEqual(flattenVisibleTree(snap.tree, new Set([projects.id])), ['root', projects.id, dormant.id]);

  engine.close();
});
