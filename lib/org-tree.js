/**
 * Pure hierarchical G organization model & strict parent-child routing rules.
 *
 * Rules:
 * - A root G (`root`) sits at the top. The Human user is root's parent.
 * - Every G has its own isolated chat, memory store, charter, and cadence.
 * - Gs can ONLY communicate directly with their parent or their direct children.
 * - Talking to a child G awakens it (which recursively awakens its due children).
 */

export const FRESHNESS = {
  FRESH: 'fresh',
  AGING: 'aging',
  STALE: 'stale',
  DORMANT: 'dormant',
};

export function sanitizeId(raw) {
  return String(raw || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 32);
}

export function createOrgRegistry() {
  return {
    uniis: new Map(), // id -> node metadata
    rootId: 'root',
  };
}

export function addUnii(registry, {
  id,
  name,
  parentId = 'root',
  charter = '',
  cadenceMs = 60_000,
  domain = 'general',
  now = Date.now(),
}) {
  const displayName = String(name || id || '').trim();
  if (!displayName && !id) throw new Error('G id or name is required');

  if (parentId !== null) {
    const parent = registry.uniis.get(parentId);
    if (!parent) throw new Error(`Parent G "${parentId}" does not exist`);
    // Prevent duplicate sibling names under the exact same parent
    const siblingExists = parent.childrenIds.some((cid) => {
      const sibling = registry.uniis.get(cid);
      return sibling && sibling.name.toLowerCase() === displayName.toLowerCase();
    });
    if (siblingExists) {
      throw new Error(`A child G named "${displayName}" already exists under ${parent.name}`);
    }
  }

  let cleanId = sanitizeId(id || displayName);
  if (!cleanId) {
    cleanId = `unii-${registry.uniis.size + 1}`;
  }
  if (registry.uniis.has(cleanId)) {
    if (id) throw new Error(`G "${cleanId}" already exists`);
    const base = parentId ? `${sanitizeId(parentId)}-${cleanId}`.slice(0, 32) : cleanId;
    let candidate = base;
    let suffix = 2;
    while (registry.uniis.has(candidate)) {
      candidate = `${base.slice(0, 28)}-${suffix++}`;
    }
    cleanId = candidate;
  }

  if (parentId !== null) {
    registry.uniis.get(parentId).childrenIds.push(cleanId);
  }

  const node = {
    id: cleanId,
    name: displayName || cleanId,
    parentId,
    childrenIds: [],
    charter: String(charter || '').trim(),
    domain: String(domain || cleanId),
    cadenceMs: Math.max(5_000, Number(cadenceMs) || 60_000),
    status: 'dormant',        // 'dormant' | 'awake' | 'thinking' | 'delegating' | 'needs_attention'
    createdAt: now,
    lastAwakenedAt: null,
    lastCheckedAt: null,
    wakeCount: 0,
    unreadFromParent: 0,
    unreadFromChildren: 0,
    goal: null,
    summaryNote: '',
  };
  registry.uniis.set(cleanId, node);
  return node;
}

export function updateUnii(registry, id, { name, charter, cadenceMs, domain } = {}) {
  const node = registry.uniis.get(id);
  if (!node) throw new Error(`Unknown G: ${id}`);

  if (name !== undefined) {
    const cleanName = String(name).trim();
    if (!cleanName) throw new Error('G name cannot be empty');
    if (node.parentId !== null) {
      const parent = registry.uniis.get(node.parentId);
      const conflict = parent?.childrenIds.some((cid) => {
        if (cid === id) return false;
        const sibling = registry.uniis.get(cid);
        return sibling && sibling.name.toLowerCase() === cleanName.toLowerCase();
      });
      if (conflict) {
        throw new Error(`A child G named "${cleanName}" already exists under ${parent.name}`);
      }
    }
    node.name = cleanName;
  }
  if (charter !== undefined) node.charter = String(charter).trim();
  if (domain !== undefined && String(domain).trim()) node.domain = String(domain).trim();
  if (cadenceMs !== undefined) node.cadenceMs = Math.max(5_000, Number(cadenceMs) || 60_000);
  return node;
}

export function deleteUnii(registry, id) {
  if (!id || id === registry.rootId) {
    throw new Error('Cannot delete the Root G');
  }
  const node = registry.uniis.get(id);
  if (!node) throw new Error(`Unknown G: ${id}`);

  const descendants = getDescendants(registry, id);
  const removedIds = [node.id, ...descendants.map((d) => d.id)];

  if (node.parentId !== null) {
    const parent = registry.uniis.get(node.parentId);
    if (parent) {
      parent.childrenIds = parent.childrenIds.filter((cid) => cid !== id);
    }
  }
  for (const rid of removedIds) {
    registry.uniis.delete(rid);
  }
  return removedIds;
}

export function getParent(registry, id) {
  const node = registry.uniis.get(id);
  if (!node || node.parentId === null) return null;
  return registry.uniis.get(node.parentId) || null;
}

export function getChildren(registry, id) {
  const node = registry.uniis.get(id);
  if (!node) return [];
  return node.childrenIds.map((cid) => registry.uniis.get(cid)).filter(Boolean);
}

export function getAncestors(registry, id) {
  const chain = [];
  let cur = registry.uniis.get(id);
  const seen = new Set();
  while (cur && !seen.has(cur.id)) {
    seen.add(cur.id);
    chain.unshift(cur);
    cur = cur.parentId ? registry.uniis.get(cur.parentId) : null;
  }
  return chain;
}

export function getDescendants(registry, id) {
  const out = [];
  const queue = [...getChildren(registry, id)];
  const seen = new Set([id]);
  while (queue.length) {
    const cur = queue.shift();
    if (!cur || seen.has(cur.id)) continue;
    seen.add(cur.id);
    out.push(cur);
    for (const child of getChildren(registry, cur.id)) queue.push(child);
  }
  return out;
}

/**
 * Strict hierarchical communication boundary:
 * - 'human' may talk directly to 'root' (its child) and inspect any Unii.
 * - Any Unii A may talk directly to Unii B iff A is B's parent OR B is A's parent.
 * - Siblings, cousins, and grandparent<->grandchild direct jumps are forbidden.
 */
export function canCommunicate(registry, fromId, toId) {
  if (!fromId || !toId || fromId === toId) return false;
  if (fromId === 'human') return toId === registry.rootId;
  if (toId === 'human') return fromId === registry.rootId;

  const from = registry.uniis.get(fromId);
  const to = registry.uniis.get(toId);
  if (!from || !to) return false;

  return from.parentId === to.id || to.parentId === from.id;
}

export function computeFreshness(node, now = Date.now()) {
  if (!node.lastAwakenedAt) return FRESHNESS.DORMANT;
  const elapsed = Math.max(0, now - node.lastAwakenedAt);
  if (elapsed <= node.cadenceMs * 1.2) return FRESHNESS.FRESH;
  if (elapsed <= node.cadenceMs * 2.5) return FRESHNESS.AGING;
  return FRESHNESS.STALE;
}

function isNodeOrDescendantDue(registry, node, now, seen = new Set()) {
  if (!node || seen.has(node.id)) return false;
  seen.add(node.id);
  if (!node.lastAwakenedAt) return true;
  if (now - node.lastAwakenedAt >= node.cadenceMs || node.unreadFromParent > 0) return true;
  return getChildren(registry, node.id).some((child) => isNodeOrDescendantDue(registry, child, now, seen));
}

export function selectDueChildren(registry, parentId, now = Date.now(), { force = false } = {}) {
  const children = getChildren(registry, parentId);
  if (force) return children;
  return children.filter((child) => isNodeOrDescendantDue(registry, child, now));
}

export function serializeTree(registry, now = Date.now()) {
  const build = (id, depth = 0) => {
    const node = registry.uniis.get(id);
    if (!node) return null;
    const freshness = computeFreshness(node, now);
    return {
      id: node.id,
      name: node.name,
      parentId: node.parentId,
      childrenIds: [...node.childrenIds],
      charter: node.charter,
      domain: node.domain,
      cadenceMs: node.cadenceMs,
      status: node.status,
      freshness,
      lastAwakenedAt: node.lastAwakenedAt,
      wakeCount: node.wakeCount,
      goal: node.goal,
      summaryNote: node.summaryNote,
      depth,
      children: node.childrenIds.map((cid) => build(cid, depth + 1)).filter(Boolean),
    };
  };
  return build(registry.rootId, 0);
}

export function formatMessagePreview(raw, maxLen = 76) {
  const clean = String(raw || '')
    .replace(/```[\s\S]*?```/g, '[code]')
    .replace(/[*_`>#]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  if (!clean) return '';
  if (clean.length <= maxLen) return clean;
  return `${clean.slice(0, maxLen - 1).trimEnd()}…`;
}

export function branchMatchesFilter(node, byId, { query = '', mode = 'all' } = {}) {
  if (!node) return false;
  const info = (byId instanceof Map ? byId.get(node.id) : byId?.[node.id]) || node;
  const q = String(query || '').trim().toLowerCase();

  let selfMatchesMode = true;
  if (mode === 'active') {
    selfMatchesMode = Boolean(info.isActive || (info.messages || 0) > 0 || (info.openAlerts || 0) > 0);
  } else if (mode === 'attention') {
    selfMatchesMode = (info.openAlerts || 0) > 0 || info.status === 'needs_attention' || info.freshness === 'stale';
  }

  const selfMatchesQuery = !q
    || String(node.name || '').toLowerCase().includes(q)
    || String(info.charter || '').toLowerCase().includes(q)
    || String(info.lastMessagePreview || '').toLowerCase().includes(q);

  const selfMatches = selfMatchesMode && selfMatchesQuery;
  if (selfMatches) return true;
  return (node.children || []).some((child) => branchMatchesFilter(child, byId, { query: q, mode }));
}

export function flattenVisibleTree(tree, collapsedIds = new Set()) {
  if (!tree) return [];
  const collapsed = collapsedIds instanceof Set ? collapsedIds : new Set(collapsedIds || []);
  const order = [];
  const walk = (node) => {
    if (!node) return;
    order.push(node.id);
    if (!collapsed.has(node.id) && Array.isArray(node.children)) {
      for (const child of node.children) walk(child);
    }
  };
  walk(tree);
  return order;
}

