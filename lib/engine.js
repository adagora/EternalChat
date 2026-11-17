import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import {
  VIEW_LIMIT,
  appendRecord,
  computeStats,
  computeViewBytes,
  createMemoryStore,
  forceFoldOldestPair,
  renderViewLines,
  summarizeText,
  utf8Bytes,
  zoomRange,
} from './memory-tree.js';
import {
  addUnii,
  canCommunicate,
  computeFreshness,
  createOrgRegistry,
  deleteUnii,
  formatMessagePreview,
  getAncestors,
  getChildren,
  getDescendants,
  getParent,
  selectDueChildren,
  serializeTree,
  updateUnii,
} from './org-tree.js';

const VERSION = '1.0.0-goramind';

const ROOT_CHARTER =
  "Chief of Staff for Gora's personal hierarchical memory & assistant org. Talk to me directly, spin up specialized Uniis underneath me, and keep periodic heartbeats running so nothing goes stale.";

export function nowIso() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
}

export class OrgEngine {
  constructor(options = {}) {
    this.dataFile = options.dataFile || null;
    this.registry = createOrgRegistry();
    this.stores = new Map();       // uniiId -> memoryStore
    this.domainItems = new Map();  // uniiId -> array of domain items
    this.agents = new Map();       // uniiId -> Map(agentName -> agentObj)
    this.activeTurns = new Map();  // uniiId -> { abortController, replying }
    this.images = new Map();       // sha256 -> { buf, media_type }
    this.sessions = new Map();     // sessionId -> sessionObj
    this.attention = new Map();    // alertId -> attentionItem
    this.pulseHistory = [];        // recent heartbeat summaries
    this.sseClients = new Set();   // response objects
    this.agentTimers = new Set();  // active background agent timers
    this.persistTimer = null;
    this.pulseCount = 0;
    this.lastPulseAt = null;
    this.pulsing = false;
    this.autoPulse = {
      enabled: options.autoPulse ?? true,
      intervalMs: options.pulseIntervalMs ?? 45_000,
      timer: null,
    };

    this.settings = {
      connection: 'api',
      cli: 'codex',
      model: process.env.ETERNALCHAT_MODEL || '',
      summary_model: process.env.ETERNALCHAT_SUMMARY_MODEL || '',
      reasoning_effort: '',
      api_provider: process.env.OPENAI_API_KEY && !process.env.ANTHROPIC_API_KEY ? 'openai' : 'anthropic',
      agent_model: '',
      api_speed: '',
      service_tier: '',
      after_turn: '',
      tools: 'auto',
      work: '/tmp/goramind-work',
      computer: 'org-runtime',
      demo: !(process.env.ANTHROPIC_API_KEY || process.env.OPENAI_API_KEY),
      chat: 'hierarchical-org',
    };

    if (!this._loadFromDisk()) {
      this._seedRootOnly();
    }
    if (this.autoPulse.enabled && !options.skipTimer) {
      this._startTimer();
    }
  }

  _seedRootOnly() {
    const now = Date.now();
    this.registry = createOrgRegistry();
    this.stores.clear();
    this.domainItems.clear();
    this.agents.clear();
    this.attention.clear();
    this.pulseHistory = [];
    this.pulseCount = 0;
    this.lastPulseAt = null;

    const root = addUnii(this.registry, {
      id: 'root',
      name: 'Root',
      parentId: null,
      domain: 'chief-of-staff',
      cadenceMs: 45_000,
      charter: ROOT_CHARTER,
      now,
    });
    root.lastAwakenedAt = now;
    root.status = 'awake';
    root.wakeCount = 1;

    this.stores.set('root', createMemoryStore());
    this.domainItems.set('root', []);
    this.agents.set('root', new Map());
  }

  _serializeState() {
    const uniis = [];
    for (const node of this.registry.uniis.values()) {
      const store = this.stores.get(node.id);
      uniis.push({
        node: { ...node, childrenIds: [...node.childrenIds] },
        store: store
          ? {
              messages: store.messages,
              nodes: Array.from(store.nodes.entries()),
              view: store.view,
              usage: store.usage,
            }
          : null,
        domainItems: this.domainItems.get(node.id) || [],
      });
    }
    return {
      version: 1,
      pulseCount: this.pulseCount,
      lastPulseAt: this.lastPulseAt,
      autoPulseEnabled: this.autoPulse.enabled,
      pulseHistory: this.pulseHistory,
      attention: Array.from(this.attention.values()),
      uniis,
    };
  }

  _schedulePersist() {
    if (!this.dataFile) return;
    if (this.persistTimer) clearTimeout(this.persistTimer);
    this.persistTimer = setTimeout(() => {
      this.persistTimer = null;
      this._saveToDiskNow();
    }, 120);
    this.persistTimer.unref?.();
  }

  _saveToDiskNow() {
    if (!this.dataFile) return;
    try {
      fs.mkdirSync(path.dirname(this.dataFile), { recursive: true });
      const tmp = `${this.dataFile}.tmp`;
      fs.writeFileSync(tmp, JSON.stringify(this._serializeState()), 'utf8');
      fs.renameSync(tmp, this.dataFile);
    } catch {
      // Ignore write failures in read-only sandboxes
    }
  }

  _loadFromDisk() {
    if (!this.dataFile || !fs.existsSync(this.dataFile)) return false;
    try {
      const raw = JSON.parse(fs.readFileSync(this.dataFile, 'utf8'));
      if (!raw || !Array.isArray(raw.uniis) || !raw.uniis.length) return false;

      const reg = createOrgRegistry();
      const stores = new Map();
      const domainItems = new Map();
      const agents = new Map();

      for (const entry of raw.uniis) {
        const n = entry.node;
        if (!n || !n.id) continue;
        reg.uniis.set(n.id, {
          ...n,
          childrenIds: Array.isArray(n.childrenIds) ? [...n.childrenIds] : [],
          status: n.status === 'thinking' || n.status === 'delegating' ? 'awake' : n.status || 'awake',
        });
        const s = entry.store;
        const mem = createMemoryStore();
        if (s) {
          mem.messages = Array.isArray(s.messages) ? s.messages : [];
          mem.nodes = new Map(Array.isArray(s.nodes) ? s.nodes : []);
          mem.view = Array.isArray(s.view) ? s.view : [];
          if (s.usage) mem.usage = s.usage;
        }
        stores.set(n.id, mem);
        domainItems.set(n.id, Array.isArray(entry.domainItems) ? entry.domainItems : []);
        agents.set(n.id, new Map());
      }

      if (!reg.uniis.has('root')) return false;

      this.registry = reg;
      this.stores = stores;
      this.domainItems = domainItems;
      this.agents = agents;
      this.pulseCount = Number(raw.pulseCount) || 0;
      this.lastPulseAt = raw.lastPulseAt || null;
      if (typeof raw.autoPulseEnabled === 'boolean') this.autoPulse.enabled = raw.autoPulseEnabled;
      this.pulseHistory = Array.isArray(raw.pulseHistory) ? raw.pulseHistory : [];
      this.attention = new Map();
      for (const att of raw.attention || []) {
        if (att && att.id) this.attention.set(att.id, att);
      }
      return true;
    } catch {
      return false;
    }
  }

  resetOrg() {
    for (const turn of this.activeTurns.values()) {
      turn.abortController?.abort();
    }
    this.activeTurns.clear();
    this._seedRootOnly();
    this._saveToDiskNow();
    this.publish('org', this.orgSnapshot(), 'root');
    this.publish('message', { uniiId: 'root' }, 'root');
    return this.orgSnapshot();
  }

  _startTimer() {
    if (this.autoPulse.timer) clearInterval(this.autoPulse.timer);
    if (!this.autoPulse.enabled) return;
    this.autoPulse.timer = setInterval(() => {
      if (!this.pulsing) {
        this.pulseOrg({ reason: 'heartbeat', force: false }).catch(() => {});
      }
    }, this.autoPulse.intervalMs);
    this.autoPulse.timer.unref?.();
  }

  close() {
    if (this.persistTimer) {
      clearTimeout(this.persistTimer);
      this.persistTimer = null;
      this._saveToDiskNow();
    }
    if (this.autoPulse.timer) {
      clearInterval(this.autoPulse.timer);
      this.autoPulse.timer = null;
    }
    for (const t of this.agentTimers) {
      clearTimeout(t);
    }
    this.agentTimers.clear();
    for (const turn of this.activeTurns.values()) {
      turn.abortController?.abort();
    }
  }

  publish(kind, value, uniiId = 'root') {
    const payload = `data: ${JSON.stringify({ kind, uniiId, value })}\n\n`;
    for (const res of this.sseClients) {
      try {
        res.write(payload);
      } catch {
        this.sseClients.delete(res);
      }
    }
  }

  getNode(uniiId = 'root') {
    const node = this.registry.uniis.get(uniiId);
    if (!node) throw new Error(`Unknown Unii: ${uniiId}`);
    return node;
  }

  getStore(uniiId = 'root') {
    this.getNode(uniiId);
    return this.stores.get(uniiId);
  }

  getAgentMap(uniiId = 'root') {
    this.getNode(uniiId);
    return this.agents.get(uniiId);
  }

  // -------------------------------------------------------------------------
  // Attention Inbox (Escalations bubbled up from leaf -> parent -> root)
  // -------------------------------------------------------------------------

  _upsertAttention({ sourceId, title, severity = 'action', detail = '' }) {
    const sourceNode = this.getNode(sourceId);
    const ancestors = getAncestors(this.registry, sourceId).map((n) => n.id);
    const key = `${sourceId}:${title}`;
    for (const existing of this.attention.values()) {
      if (existing.key === key && !existing.resolved) {
        existing.updatedAt = nowIso();
        existing.severity = severity;
        return { item: existing, isNew: false };
      }
    }
    const id = `att-${crypto.randomBytes(4).toString('hex')}`;
    const item = {
      id,
      key,
      sourceId,
      sourceName: sourceNode.name,
      path: ancestors,
      title,
      detail,
      severity, // 'urgent' | 'action' | 'info'
      resolved: false,
      createdAt: nowIso(),
      updatedAt: nowIso(),
      resolvedAt: null,
    };
    this.attention.set(id, item);
    if (sourceNode.status !== 'thinking') {
      sourceNode.status = 'needs_attention';
    }
    return { item, isNew: true };
  }

  listAttention({ resolved = false, uniiId = null } = {}) {
    const out = [];
    for (const item of this.attention.values()) {
      if (resolved !== null && item.resolved !== resolved) continue;
      if (uniiId && uniiId !== 'root' && item.sourceId !== uniiId && !item.path.includes(uniiId)) continue;
      out.push(item);
    }
    const weight = { urgent: 0, action: 1, info: 2 };
    out.sort((a, b) => (weight[a.severity] ?? 9) - (weight[b.severity] ?? 9) || b.updatedAt.localeCompare(a.updatedAt));
    return out;
  }

  resolveAttention(alertId) {
    const item = this.attention.get(alertId);
    if (!item) throw new Error(`Attention item ${alertId} not found`);
    item.resolved = true;
    item.resolvedAt = nowIso();
    const src = this.registry.uniis.get(item.sourceId);
    if (src) {
      const stillOpen = this.listAttention({ resolved: false, uniiId: src.id }).some((a) => a.sourceId === src.id);
      if (!stillOpen && src.status === 'needs_attention') src.status = 'awake';
      const store = this.stores.get(src.id);
      if (store) appendRecord(store, 'note', `Resolved attention item: ${item.title}`);
    }
    this._schedulePersist();
    this.publish('org', this.orgSnapshot(), 'root');
    return item;
  }

  // -------------------------------------------------------------------------
  // Creating & Managing Sub-Uniis
  // -------------------------------------------------------------------------

  createSubUnii({ parentId = 'root', name, charter = '', cadenceMs = 60_000, domain = '' }) {
    if (!name || !String(name).trim()) throw new Error('A Unii needs a name');
    const node = addUnii(this.registry, {
      name: String(name).trim(),
      parentId,
      charter: charter || `Maintains ${name} autonomously and escalates anything needing attention.`,
      domain: domain || name.toLowerCase(),
      cadenceMs,
    });
    const store = createMemoryStore();
    this.stores.set(node.id, store);
    this.domainItems.set(node.id, []);
    this.agents.set(node.id, new Map());

    appendRecord(store, 'note', `${node.name} Unii created under ${this.getNode(parentId).name}. Charter: ${node.charter}`);
    const parentStore = this.stores.get(parentId);
    appendRecord(parentStore, 'note', `Created child Unii **${node.name}** (\`${node.id}\`) — ${node.charter}`);

    this._schedulePersist();
    this.publish('message', { uniiId: parentId }, parentId);
    this.publish('org', this.orgSnapshot(), 'root');
    return node;
  }

  updateSubUnii(uniiId, patch = {}) {
    const node = updateUnii(this.registry, uniiId, patch);
    for (const att of this.attention.values()) {
      if (att.sourceId === node.id) att.sourceName = node.name;
    }
    this._schedulePersist();
    this.publish('org', this.orgSnapshot(), 'root');
    return node;
  }

  deleteSubUnii(uniiId) {
    const target = this.getNode(uniiId);
    const parentId = target.parentId;
    const removedIds = deleteUnii(this.registry, uniiId);
    const removedSet = new Set(removedIds);

    for (const rid of removedIds) {
      const turn = this.activeTurns.get(rid);
      if (turn?.abortController) turn.abortController.abort();
      this.activeTurns.delete(rid);
      this.stores.delete(rid);
      this.domainItems.delete(rid);
      this.agents.delete(rid);
    }
    for (const [attId, item] of this.attention.entries()) {
      if (removedSet.has(item.sourceId)) this.attention.delete(attId);
    }

    if (parentId && this.stores.has(parentId)) {
      appendRecord(this.stores.get(parentId), 'note', `Removed child Unii **${target.name}** (${removedIds.length} node(s)).`);
      this.publish('message', { uniiId: parentId }, parentId);
    }
    this._schedulePersist();
    this.publish('org', this.orgSnapshot(), 'root');
    return { removedIds, parentId };
  }

  addDomainItem(uniiId, { title, severity = 'action', needsAttention = true } = {}) {
    const node = this.getNode(uniiId);
    const cleanTitle = String(title || '').trim();
    if (!cleanTitle) throw new Error('Item title is required');
    const items = this.domainItems.get(uniiId) || [];
    const item = {
      id: `itm-${crypto.randomBytes(3).toString('hex')}`,
      title: cleanTitle,
      severity: ['urgent', 'action', 'info'].includes(severity) ? severity : 'action',
      needsAttention: Boolean(needsAttention),
      updatedAt: nowIso(),
    };
    items.push(item);
    this.domainItems.set(uniiId, items);

    if (item.needsAttention) {
      this._upsertAttention({
        sourceId: uniiId,
        title: cleanTitle,
        severity: item.severity,
        detail: `Tracked in ${node.name}.`,
      });
    }
    appendRecord(this.stores.get(uniiId), 'note', `Tracking: ${cleanTitle}`);
    this._schedulePersist();
    this.publish('message', { uniiId }, uniiId);
    this.publish('org', this.orgSnapshot(), 'root');
    return item;
  }

  removeDomainItem(uniiId, itemId) {
    this.getNode(uniiId);
    const items = this.domainItems.get(uniiId) || [];
    const idx = items.findIndex((it) => it.id === itemId);
    if (idx === -1) throw new Error('Tracked item not found');
    const [removed] = items.splice(idx, 1);
    for (const att of this.attention.values()) {
      if (att.sourceId === uniiId && att.title === removed.title && !att.resolved) {
        att.resolved = true;
        att.resolvedAt = nowIso();
      }
    }
    const stillOpen = this.listAttention({ resolved: false, uniiId }).some((a) => a.sourceId === uniiId);
    const node = this.getNode(uniiId);
    if (!stillOpen && node.status === 'needs_attention') node.status = 'awake';
    this._schedulePersist();
    this.publish('org', this.orgSnapshot(), 'root');
    return removed;
  }

  // -------------------------------------------------------------------------
  // Strict Parent <-> Child Messaging & Recursive Awakening
  // -------------------------------------------------------------------------

  sendParentChildMessage(fromId, toId, text, { kind = 'directive' } = {}) {
    if (!canCommunicate(this.registry, fromId, toId)) {
      throw new Error(`Hierarchy violation: "${fromId}" cannot directly message "${toId}". Uniis may only talk to their parent or direct children.`);
    }
    const fromNode = fromId === 'human' ? { id: 'human', name: 'Human' } : this.getNode(fromId);
    const toNode = toId === 'human' ? { id: 'human', name: 'Human', parentId: null } : this.getNode(toId);
    const targetStoreId = toId === 'human' ? 'root' : toId;
    const toStore = this.stores.get(targetStoreId);
    const isDownward = fromId === 'human' || toNode.parentId === fromId;

    const recordText = isDownward
      ? `[From parent ${fromNode.name}] ${text}`
      : `[Report from child ${fromNode.name}] ${text}`;

    const msg = appendRecord(toStore, 'note', recordText, {
      meta: { fromId, toId, direction: isDownward ? 'down' : 'up', kind },
    });

    if (toId !== 'human') {
      if (isDownward) toNode.unreadFromParent += 1;
      else toNode.unreadFromChildren += 1;
    }

    this.publish('message', { i: msg.i, uniiId: targetStoreId }, targetStoreId);
    return msg;
  }

  /**
   * Awaken a Unii:
   * - Updates wake metadata & logs awakening trace
   * - Evaluates domain items & raises/syncs attention items
   * - Recursively awakens due (or forced) direct children
   * - Sends concise upward report to parent (if not root)
   */
  async awakenUnii(uniiId, { reason = 'parent_call', directive = '', forceChildren = false, visited = new Set(), depth = 0 } = {}) {
    if (visited.has(uniiId) || depth > 8) return { uniiId, skipped: true };
    visited.add(uniiId);

    const node = this.getNode(uniiId);
    const store = this.stores.get(uniiId);
    const now = Date.now();

    node.status = 'thinking';
    node.lastAwakenedAt = now;
    node.wakeCount += 1;
    node.unreadFromParent = 0;
    this.publish('org', this.orgSnapshot(), 'root');

    const logVerboseWake = reason === 'parent_call';
    if (logVerboseWake) {
      // Record tool trace when explicitly invoked by parent/user delegation
      const callJson = JSON.stringify({
        name: 'awaken_check',
        input: { unii: node.id, reason, directive: directive || undefined },
      });
      const tMsg = appendRecord(store, 'tool', callJson);
      this.publish('message', { i: tMsg.i, uniiId: node.id }, node.id);
    }

    const items = this.domainItems.get(node.id) || [];
    const flagged = items.filter((it) => it.needsAttention);
    const createdAlerts = [];
    let hasNewAlert = false;
    for (const item of flagged) {
      const { item: alert, isNew } = this._upsertAttention({
        sourceId: node.id,
        title: item.title,
        severity: item.severity || 'action',
        detail: `${node.name} surfaced this during ${reason} check.`,
      });
      if (isNew) hasNewAlert = true;
      createdAlerts.push(alert);
    }

    const childReports = [];
    const dueChildren = selectDueChildren(this.registry, node.id, now, { force: forceChildren });

    if (dueChildren.length > 0) {
      node.status = 'delegating';
      this.publish('org', this.orgSnapshot(), 'root');

      for (const child of dueChildren) {
        const wakePrompt = directive
          ? `Wake up (${reason}). Parent guidance: ${directive}`
          : `Heartbeat wake-up (${reason}): check ${child.domain} status, prevent staleness, and escalate anything needing attention.`;

        // Log parent -> child IPC tool call only on explicit parent_call delegation so heartbeats stay high-signal
        const logVerboseIpc = reason === 'parent_call';
        if (logVerboseIpc) {
          const ipcCall = appendRecord(
            store,
            'tool',
            JSON.stringify({ name: 'talk_to_child', input: { child: child.id, name: child.name, message: wakePrompt } })
          );
          this.publish('message', { i: ipcCall.i, uniiId: node.id }, node.id);
        }

        // Deliver message strictly across parent -> child edge
        if (logVerboseIpc) {
          this.sendParentChildMessage(node.id, child.id, wakePrompt);
        }

        // Recursively awaken child
        const childResult = await this.awakenUnii(child.id, {
          reason,
          directive,
          forceChildren,
          visited,
          depth: depth + 1,
        });
        childReports.push(childResult);

        if (logVerboseIpc) {
          const ipcEcho = appendRecord(
            store,
            'echo',
            `${child.name} awake (#${childResult.wakeCount}, ${childResult.openAlerts} open alerts): ${childResult.summary}`
          );
          this.publish('message', { i: ipcEcho.i, uniiId: node.id }, node.id);
        }
      }
    }

    const openForNode = this.listAttention({ resolved: false, uniiId: node.id });
    let summary;
    if (flagged.length) {
      summary = `${flagged.length} item(s) need attention (${flagged.map((f) => f.title).join('; ')})`;
    } else if (childReports.length) {
      const childHighlights = childReports
        .map((c) => `${c.name}: ${c.summary}`)
        .join(' | ');
      summary = `Awakened ${childReports.length} child Uniis (${openForNode.length} alert(s) in scope) — ${childHighlights}`;
    } else {
      summary = `${node.name} checked ${items.length} tracked item(s); everything fresh.`;
    }

    const anyNewAlert = hasNewAlert || childReports.some((c) => c.hasNewAlert);
    node.summaryNote = summary;
    if (logVerboseWake || hasNewAlert) {
      const echoMsg = appendRecord(store, 'echo', summary);
      this.publish('message', { i: echoMsg.i, uniiId: node.id }, node.id);
    }

    // Send upward report to parent when explicitly requested or when a brand-new escalation surfaced
    if (node.parentId && (hasNewAlert || reason === 'parent_call')) {
      this.sendParentChildMessage(node.id, node.parentId, summary, { kind: 'report' });
    }

    node.status = openForNode.some((a) => a.sourceId === node.id) ? 'needs_attention' : 'awake';
    node.lastCheckedAt = Date.now();
    this.publish('compaction', { nodes: store.nodes.size }, node.id);
    this.publish('org', this.orgSnapshot(), 'root');

    return {
      uniiId: node.id,
      name: node.name,
      wakeCount: node.wakeCount,
      openAlerts: openForNode.length,
      flaggedCount: flagged.length,
      childrenAwakened: childReports.length,
      hasNewAlert: anyNewAlert,
      summary,
    };
  }

  /**
   * Pulse Root: awakens root and cascades down through the hierarchy.
   */
  async pulseOrg({ reason = 'heartbeat', force = true, targetId = 'root' } = {}) {
    if (this.pulsing) return { ok: false, reason: 'already_pulsing' };
    this.pulsing = true;
    this.publish('pulse', { pulsing: true, targetId }, targetId);

    try {
      const startedAt = Date.now();
      const visited = new Set();
      const result = await this.awakenUnii(targetId, {
        reason,
        forceChildren: force,
        visited,
      });

      this.pulseCount += 1;
      this.lastPulseAt = nowIso();
      const openAlerts = this.listAttention({ resolved: false });
      const pulseEntry = {
        pulse: this.pulseCount,
        at: this.lastPulseAt,
        durationMs: Date.now() - startedAt,
        awakened: visited.size,
        openAlerts: openAlerts.length,
        targetId,
      };
      this.pulseHistory.unshift(pulseEntry);
      if (this.pulseHistory.length > 20) this.pulseHistory.length = 20;

      this._schedulePersist();
      this.publish('org', this.orgSnapshot(), 'root');
      return { ok: true, ...pulseEntry, result };
    } finally {
      this.pulsing = false;
      this.publish('pulse', { pulsing: false, targetId }, targetId);
    }
  }

  // -------------------------------------------------------------------------
  // Chat & Turn Generation (per-Unii)
  // -------------------------------------------------------------------------

  async submitMessage(uniiId, text, attachments = []) {
    const node = this.getNode(uniiId);
    const store = this.stores.get(uniiId);

    appendRecord(store, 'user', text, { attachments });
    this._schedulePersist();
    this.publish('message', { uniiId }, uniiId);

    // Run reply loop asynchronously so POST /api/send returns fast
    this._generateTurn(node.id, text).catch(() => {});
    return { ok: true };
  }

  async _generateTurn(uniiId, userText) {
    const node = this.getNode(uniiId);
    const store = this.stores.get(uniiId);

    const existing = this.activeTurns.get(uniiId);
    if (existing?.abortController) existing.abortController.abort();

    const controller = new AbortController();
    this.activeTurns.set(uniiId, { abortController: controller, replying: true });
    node.status = 'thinking';
    node.lastAwakenedAt = Date.now();
    node.wakeCount += 1;

    this.publish('status', { purpose: 'turn', message: `Awakening ${node.name}` }, uniiId);
    this.publish('org', this.orgSnapshot(), 'root');

    try {
      const thinking = `Reading ${node.name}'s ${store.view.length}-line memory view and checking ${getChildren(this.registry, uniiId).length} direct sub-Uniis.`;
      this.publish('thinking', thinking, uniiId);

      await new Promise((r) => setTimeout(r, 160));
      if (controller.signal.aborted) {
        appendRecord(store, 'unii', '[interrupted: this reply did not finish]');
        this.publish('message', { uniiId }, uniiId);
        return;
      }

      let replyText = '';
      const awakenedReports = await this._delegateMatchedChildren(node, store, userText, controller.signal);

      if (!this.settings.demo && this.settings.api_provider === 'anthropic' && process.env.ANTHROPIC_API_KEY) {
        try {
          replyText = await this._callAnthropic(node, store, userText, controller.signal, awakenedReports);
        } catch (apiErr) {
          if (apiErr.name === 'AbortError') throw apiErr;
          replyText = await this._runOrgAwareDemo(node, store, userText, controller.signal, awakenedReports);
        }
      } else if (!this.settings.demo && this.settings.api_provider === 'openai' && process.env.OPENAI_API_KEY) {
        try {
          replyText = await this._callOpenAI(node, store, userText, controller.signal, awakenedReports);
        } catch (apiErr) {
          if (apiErr.name === 'AbortError') throw apiErr;
          replyText = await this._runOrgAwareDemo(node, store, userText, controller.signal, awakenedReports);
        }
      } else {
        replyText = await this._runOrgAwareDemo(node, store, userText, controller.signal, awakenedReports);
      }

      const chunks = replyText.match(/.{1,42}/gs) || [replyText];
      for (const chunk of chunks) {
        if (controller.signal.aborted) {
          appendRecord(store, 'unii', `${replyText.slice(0, 64)} [interrupted: this reply did not finish]`);
          this.publish('message', { uniiId }, uniiId);
          return;
        }
        this.publish('text', chunk, uniiId);
        await new Promise((r) => setTimeout(r, 14));
      }

      appendRecord(store, 'unii', replyText);
      this._schedulePersist();
      this.publish('message', { uniiId }, uniiId);
      this.publish('compaction', { nodes: store.nodes.size }, uniiId);
    } catch (err) {
      if (err.name === 'AbortError') {
        appendRecord(store, 'unii', '[interrupted: this reply did not finish]');
        this._schedulePersist();
        this.publish('message', { uniiId }, uniiId);
      } else {
        this.publish('error', err.message || 'Reply failed', uniiId);
      }
    } finally {
      this.activeTurns.delete(uniiId);
      const hasOpen = this.listAttention({ resolved: false, uniiId: node.id }).some((a) => a.sourceId === node.id);
      node.status = hasOpen ? 'needs_attention' : 'awake';
      this.publish('status', { replying: false }, uniiId);
      this.publish('org', this.orgSnapshot(), 'root');
    }
  }

  _buildSystemPrompt(node, store, awakenedReports = []) {
    const parent = getParent(this.registry, node.id);
    const children = getChildren(this.registry, node.id);
    const viewLines = renderViewLines(store).map((l) => `${l.id}+${l.n}|${l.text}`).join('\n');
    const alerts = this.listAttention({ resolved: false, uniiId: node.id });
    const reportSection = awakenedReports.length
      ? `Fresh child reports gathered during this turn:\n${awakenedReports.map((r) => `- ${r.name} (wake #${r.wakeCount}): ${r.summary}`).join('\n')}`
      : '';
    return [
      `You are the "${node.name}" Unii (${node.id}) inside Gora's personal GoraMind hierarchical memory & assistant organization.`,
      `Charter: ${node.charter}`,
      `Parent: ${parent ? `${parent.name} (${parent.id})` : 'Human User'}`,
      `Direct Children: ${children.length ? children.map((c) => `${c.name} (${c.id}: ${c.summaryNote || c.charter})`).join(' | ') : 'None (leaf Unii)'}`,
      `Communication rule: You can ONLY talk to your parent and your direct children.`,
      `Open attention items in your scope:\n${alerts.length ? alerts.map((a) => `- [${a.severity}] ${a.sourceName}: ${a.title}`).join('\n') : '(none)'}`,
      reportSection,
      `Saved binary-tree memory view (${store.messages.length} total messages):\n${viewLines}`,
    ].filter(Boolean).join('\n\n');
  }

  _recentConversationMessages(store) {
    const raw = store.messages
      .filter((m) => m.kind === 'user' || m.kind === 'unii')
      .slice(-16)
      .map((m) => ({ role: m.kind === 'unii' ? 'assistant' : 'user', content: String(m.text || '') }));
    while (raw.length && raw[0].role !== 'user') raw.shift();
    const merged = [];
    for (const m of raw) {
      const prev = merged[merged.length - 1];
      if (prev && prev.role === m.role) prev.content += `\n\n${m.content}`;
      else merged.push({ ...m });
    }
    return merged;
  }

  async _callAnthropic(node, store, userText, signal, awakenedReports = []) {
    const model = this.settings.model || 'claude-sonnet-4-6';
    const timeoutSignal = AbortSignal.timeout(8000);
    const combinedSignal = AbortSignal.any ? AbortSignal.any([signal, timeoutSignal]) : signal;
    const messages = this._recentConversationMessages(store);
    if (!messages.length) messages.push({ role: 'user', content: String(userText || '') });
    const resp = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: {
        'x-api-key': process.env.ANTHROPIC_API_KEY,
        'anthropic-version': '2023-06-01',
        'content-type': 'application/json',
      },
      body: JSON.stringify({
        model,
        max_tokens: 1024,
        system: this._buildSystemPrompt(node, store, awakenedReports),
        messages,
      }),
      signal: combinedSignal,
    });
    if (!resp.ok) throw new Error(`Anthropic API HTTP ${resp.status}`);
    const json = await resp.json();
    const replyText = (json.content || []).map((c) => c.text || '').join('');
    if (json.usage) {
      store.usage.turns.input_tokens += json.usage.input_tokens || 0;
      store.usage.turns.output_tokens += json.usage.output_tokens || 0;
    }
    return replyText || '(empty reply)';
  }

  async _callOpenAI(node, store, userText, signal, awakenedReports = []) {
    const model = this.settings.model || 'gpt-4o';
    const timeoutSignal = AbortSignal.timeout(8000);
    const combinedSignal = AbortSignal.any ? AbortSignal.any([signal, timeoutSignal]) : signal;
    const messages = this._recentConversationMessages(store);
    if (!messages.length) messages.push({ role: 'user', content: String(userText || '') });
    const resp = await fetch('https://api.openai.com/v1/chat/completions', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${process.env.OPENAI_API_KEY}`,
        'content-type': 'application/json',
      },
      body: JSON.stringify({
        model,
        messages: [
          { role: 'system', content: this._buildSystemPrompt(node, store, awakenedReports) },
          ...messages,
        ],
      }),
      signal: combinedSignal,
    });
    if (!resp.ok) throw new Error(`OpenAI API HTTP ${resp.status}`);
    const json = await resp.json();
    const replyText = json.choices?.[0]?.message?.content || '';
    if (json.usage) {
      store.usage.turns.input_tokens += json.usage.prompt_tokens || 0;
      store.usage.turns.output_tokens += json.usage.completion_tokens || 0;
    }
    return replyText || '(empty reply)';
  }

  _mentionsNode(text, node) {
    const tokens = [node.name, node.id].filter(Boolean);
    return tokens.some((tok) => {
      const escaped = String(tok).trim().replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      if (!escaped) return false;
      return new RegExp(`(^|[^a-z0-9])${escaped}([^a-z0-9]|$)`, 'i').test(text);
    });
  }

  async _delegateMatchedChildren(node, store, userText, signal) {
    const lower = userText.toLowerCase();
    const children = getChildren(this.registry, node.id);

    const matchedChildren = [];
    for (const child of children) {
      if (this._mentionsNode(userText, child)) {
        matchedChildren.push(child);
      } else {
        const descendants = getDescendants(this.registry, child.id);
        if (descendants.some((desc) => this._mentionsNode(userText, desc))) {
          matchedChildren.push(child);
        }
      }
    }

    const wantsBroadSweep = node.id === 'root' && /\b(status|brief|morning|update|attention|wake|everything|org|stale)\b/.test(lower);
    const toWake = matchedChildren.length
      ? matchedChildren
      : wantsBroadSweep
        ? children
        : [];

    const awakenedReports = [];
    for (const child of toWake) {
      if (signal.aborted) break;
      const callRecord = appendRecord(
        store,
        'tool',
        JSON.stringify({ name: 'talk_to_child', input: { child: child.id, name: child.name, message: userText } })
      );
      this.publish('message', { i: callRecord.i, uniiId: node.id }, node.id);
      this.publish('status', { purpose: 'turn', message: `Talking to ${child.name}` }, node.id);

      this.sendParentChildMessage(node.id, child.id, userText);
      const report = await this.awakenUnii(child.id, {
        reason: 'parent_call',
        directive: userText,
        forceChildren: true,
      });
      awakenedReports.push(report);

      const echoRecord = appendRecord(
        store,
        'echo',
        `${child.name} responded (wake #${report.wakeCount}): ${report.summary}`
      );
      this.publish('message', { i: echoRecord.i, uniiId: node.id }, node.id);
    }

    // Also allow tracking reminders directly in a domain Unii
    const items = this.domainItems.get(node.id) || [];
    if (/^(track|add|remember|todo|remind|watch)\b/i.test(lower) && node.id !== 'root') {
      const cleanTitle = userText.replace(/^(track|add|remember|todo|remind|watch)\s*:?\s*/i, '').trim();
      if (cleanTitle) {
        const newItem = {
          id: `itm-${crypto.randomBytes(3).toString('hex')}`,
          title: cleanTitle,
          severity: 'action',
          needsAttention: true,
          updatedAt: nowIso(),
        };
        items.push(newItem);
        this.domainItems.set(node.id, items);
        this._upsertAttention({
          sourceId: node.id,
          title: cleanTitle,
          severity: 'action',
          detail: `Added directly in ${node.name} thread.`,
        });
      }
    }

    return awakenedReports;
  }

  async _runOrgAwareDemo(node, store, userText, signal, awakenedReports = []) {
    const children = getChildren(this.registry, node.id);
    const items = this.domainItems.get(node.id) || [];
    const openAlerts = this.listAttention({ resolved: false, uniiId: node.id });
    let replyText = '';

    if (awakenedReports.length) {
      const bullets = awakenedReports.map((r) => `- **${r.name}** (wake #${r.wakeCount}): ${r.summary}`).join('\n');
      replyText = `I awakened **${awakenedReports.map((r) => r.name).join(', ')}** and collected their live reports:\n\n${bullets}`;
      if (openAlerts.length) {
        replyText += `\n\n**Active attention items in scope (${openAlerts.length}):**\n` +
          openAlerts.slice(0, 4).map((a) => `- [${a.severity.toUpperCase()}] **${a.sourceName}**: ${a.title}`).join('\n');
      }
    } else if (node.id === 'root') {
      if (!children.length) {
        replyText = `Logged in **Root**'s memory (#${store.messages.length}, ${computeViewBytes(store)} bytes in view).\n\nYour organization currently starts with just **Root**. Click **+ New Unii** in the sidebar (or run \`/hire Name charter\`) to spin up dedicated domain Uniis like Calendar, Health, Projects, or Investments—each with its own isolated memory and heartbeat.`;
      } else {
        const childList = children.map((c) => `**${c.name}**`).join(', ');
        replyText = `Logged in **Root**'s memory (#${store.messages.length}, ${computeViewBytes(store)} bytes in view).\n\nI supervise ${children.length} direct Uniis (${childList}). Mention any domain to awaken only the relevant branches, or click **Pulse** to breathe life across your tree.`;
      }
    } else {
      const itemSummary = items.length
        ? items.map((it) => `- ${it.needsAttention ? '**[Needs Attention]** ' : ''}${it.title}`).join('\n')
        : '- No tracked domain items yet. Add one in the Tracked panel above or say *"track ... "* to keep it fresh.';
      replyText = `**${node.name}** received your update in its isolated memory (#${store.messages.length}, ${computeViewBytes(store)} bytes in view).\n\nCurrent domain state:\n${itemSummary}\n\n${node.parentId ? `I report upward only to **${this.getNode(node.parentId).name}**.` : ''}`;
    }

    store.usage.turns.input_tokens += Math.ceil(utf8Bytes(userText) / 4);
    store.usage.turns.output_tokens += Math.ceil(utf8Bytes(replyText) / 4);
    return replyText;
  }

  // -------------------------------------------------------------------------
  // Subagents (per-Unii) & Legacy Compatibility Methods
  // -------------------------------------------------------------------------

  startAgent(uniiId, name, task) {
    const cleanName = String(name || '').trim();
    const cleanTask = String(task || '').trim();
    if (!cleanName || !cleanTask) throw new Error('An agent needs a name and a task');
    const agentMap = this.getAgentMap(uniiId);
    const agent = {
      name: cleanName,
      task: cleanTask,
      status: 'running',
      role: { kind: 'agent' },
      records: [{ i: 0, kind: 'user', text: cleanTask, date: nowIso() }],
    };
    agentMap.set(cleanName, agent);
    this.publish('agent_event', { name: cleanName, event: 'started' }, uniiId);

    const timer = setTimeout(() => {
      this.agentTimers.delete(timer);
      if (agent.status !== 'running') return;
      const report = `Completed task inside ${this.getNode(uniiId).name}: "${cleanTask}".`;
      agent.records.push({ i: 1, kind: 'unii', text: report, date: nowIso() });
      agent.status = 'complete';
      const store = this.stores.get(uniiId);
      appendRecord(store, 'work', `[${cleanName}] ${report}`);
      this.publish('message', { uniiId }, uniiId);
      this.publish('agent_report', { name: cleanName, status: 'complete', text: report }, uniiId);
    }, 650);
    timer.unref?.();
    this.agentTimers.add(timer);

    return agent;
  }

  stopWork(uniiId, agentName = null) {
    if (agentName) {
      const agent = this.getAgentMap(uniiId).get(agentName);
      if (agent) agent.status = 'interrupted';
      this.publish('agent_event', { name: agentName, event: 'finished' }, uniiId);
      return;
    }
    const turn = this.activeTurns.get(uniiId);
    if (turn?.abortController) turn.abortController.abort();
  }

  setGoal(uniiId, text) {
    const node = this.getNode(uniiId);
    node.goal = text;
    this.publish('goal', { text }, uniiId);
    return this.submitMessage(uniiId, text);
  }

  haltGoal(uniiId) {
    const node = this.getNode(uniiId);
    node.goal = null;
    this.publish('goal', { text: null }, uniiId);
  }

  compact(uniiId) {
    const store = this.getStore(uniiId);
    forceFoldOldestPair(store);
    this.publish('compaction', { nodes: store.nodes.size }, uniiId);
    return { ok: true };
  }

  // -------------------------------------------------------------------------
  // Snapshots for Frontend & Tests
  // -------------------------------------------------------------------------

  orgSnapshot() {
    const now = Date.now();
    const uniis = [];
    let totalMessages = 0;
    let totalBytes = 0;
    let totalNodes = 0;
    let activeUniis = 0;
    for (const [id, node] of this.registry.uniis.entries()) {
      const store = this.stores.get(id);
      const viewBytes = computeViewBytes(store);
      totalMessages += store.messages.length;
      totalBytes += viewBytes;
      totalNodes += store.nodes.size;
      const openAlerts = this.listAttention({ resolved: false, uniiId: id }).filter((a) => a.sourceId === id).length;
      const domainItemsCount = (this.domainItems.get(id) || []).length;
      const lastMsg = store.messages.length ? store.messages[store.messages.length - 1] : null;
      const isActive = store.messages.length > 0
        || openAlerts > 0
        || domainItemsCount > 0
        || node.status === 'thinking'
        || node.status === 'delegating'
        || node.status === 'needs_attention';
      if (isActive) activeUniis += 1;
      uniis.push({
        id: node.id,
        name: node.name,
        parentId: node.parentId,
        childrenIds: [...node.childrenIds],
        charter: node.charter,
        domain: node.domain,
        cadenceMs: node.cadenceMs,
        status: node.status,
        freshness: computeFreshness(node, now),
        lastAwakenedAt: node.lastAwakenedAt,
        wakeCount: node.wakeCount,
        messages: store.messages.length,
        viewBytes,
        openAlerts,
        domainItemsCount,
        unreadCount: (node.unreadFromParent || 0) + (node.unreadFromChildren || 0),
        isActive,
        lastMessageAt: lastMsg?.date || (node.lastAwakenedAt ? new Date(node.lastAwakenedAt).toISOString() : null) || (node.createdAt ? new Date(node.createdAt).toISOString() : null),
        lastMessageRole: lastMsg?.kind || null,
        lastMessagePreview: lastMsg ? formatMessagePreview(lastMsg.text) : '',
        summaryNote: node.summaryNote,
      });
    }

    return {
      tree: serializeTree(this.registry, now),
      uniis,
      attention: this.listAttention({ resolved: false }),
      heartbeat: {
        enabled: this.autoPulse.enabled,
        intervalMs: this.autoPulse.intervalMs,
        pulsing: this.pulsing,
        pulseCount: this.pulseCount,
        lastPulseAt: this.lastPulseAt,
        history: this.pulseHistory.slice(0, 6),
      },
      totals: {
        uniis: this.registry.uniis.size,
        activeUniis,
        messages: totalMessages,
        viewBytes: totalBytes,
        nodes: totalNodes,
        openAttention: this.listAttention({ resolved: false }).length,
      },
    };
  }

  uniiState(uniiId = 'root') {
    const node = this.getNode(uniiId);
    const store = this.getStore(uniiId);
    const agentMap = this.getAgentMap(uniiId);
    const agentsObj = {};
    for (const [name, a] of agentMap.entries()) {
      agentsObj[name] = a.status === 'running';
    }
    const turn = this.activeTurns.get(uniiId);
    const stats = computeStats(store);
    const subscription = this.settings.connection === 'subscription';
    const vendor = this.settings.api_provider || 'anthropic';

    return {
      unii: {
        id: node.id,
        name: node.name,
        parentId: node.parentId,
        childrenIds: [...node.childrenIds],
        charter: node.charter,
        domain: node.domain,
        cadenceMs: node.cadenceMs,
        status: node.status,
        freshness: computeFreshness(node),
        lastAwakenedAt: node.lastAwakenedAt,
        wakeCount: node.wakeCount,
        ancestors: getAncestors(this.registry, node.id).map((n) => ({ id: n.id, name: n.name })),
        children: getChildren(this.registry, node.id).map((n) => ({ id: n.id, name: n.name, status: n.status })),
        domainItems: this.domainItems.get(node.id) || [],
      },
      org: this.orgSnapshot(),
      settings: {
        ...this.settings,
        model_default: subscription ? "the CLI's default" : vendor === 'openai' ? 'gpt-4o' : 'claude-sonnet-4-6',
        summary_default: subscription || vendor === 'openai' ? 'the same as replies' : 'claude-haiku-4-5-20251001',
      },
      status: {
        ...stats,
        replying: Boolean(turn?.replying),
        goal: node.goal,
        agents_running: Array.from(agentMap.values()).filter((a) => a.status === 'running').length,
      },
      version: VERSION,
      agents: agentsObj,
      view_limit: VIEW_LIMIT,
      computers: [],
      push: { configured: false, public_key: null },
      audio: {
        configured: false,
        provider: 'openai',
        model: 'gpt-transcribe',
        max_bytes: 24 * 1024 * 1024,
      },
    };
  }
}
