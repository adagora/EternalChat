/**
 * Pure append-only message log + binary summary tree + view partitioning.
 * Zero I/O — every Unii owns one isolated instance of this store.
 */

export const VIEW_LIMIT = 128_000;

export function utf8Bytes(str) {
  return Buffer.byteLength(String(str || ''), 'utf8');
}

export function summarizeText(text, maxLen = 160) {
  const clean = String(text || '').replace(/\s+/g, ' ').trim();
  if (!clean) return '(empty)';
  if (clean.length <= maxLen) return clean;
  return clean.slice(0, maxLen - 1) + '…';
}

export function createMemoryStore() {
  return {
    messages: [],      // [{ i, kind, text, size, date, attachments?, meta? }]
    nodes: new Map(),  // `${l}:${i}` -> { l, i, text, size }
    view: [],          // [[l, i], ...]
    usage: {
      turns: { input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0 },
      summaries: { input_tokens: 0, output_tokens: 0 },
    },
  };
}

export function appendRecord(store, kind, text, { attachments = [], date, meta } = {}) {
  const i = store.messages.length;
  const iso = date || new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
  const cleanText = String(text ?? '');
  const msg = {
    i,
    kind,
    text: cleanText,
    size: utf8Bytes(cleanText),
    date: iso,
    ...(attachments && attachments.length ? { attachments } : {}),
    ...(meta ? { meta } : {}),
  };
  store.messages.push(msg);

  const leafText = `${kind}: ${summarizeText(cleanText, 220)}`;
  store.nodes.set(`0:${i}`, { l: 0, i, text: leafText, size: utf8Bytes(leafText) });
  store.view.push([0, i]);

  // Build binary tree parent nodes when pairs complete
  let level = 0;
  let idx = i;
  while (idx % 2 === 1) {
    const left = store.nodes.get(`${level}:${idx - 1}`);
    const right = store.nodes.get(`${level}:${idx}`);
    if (!left || !right) break;
    const parentIdx = Math.floor(idx / 2);
    const parentLevel = level + 1;
    const combined = summarizeText(`${left.text} / ${right.text}`, 320);
    store.nodes.set(`${parentLevel}:${parentIdx}`, {
      l: parentLevel,
      i: parentIdx,
      text: combined,
      size: utf8Bytes(combined),
    });
    level = parentLevel;
    idx = parentIdx;
  }

  // Keep view under a compact threshold when history grows: merge oldest sibling pairs
  compactViewIfNeeded(store, 64_000, VIEW_LIMIT);

  return msg;
}

export function compactViewIfNeeded(store, lowTarget = 64_000, highLimit = VIEW_LIMIT) {
  let bytes = computeViewBytes(store);
  if (bytes <= highLimit) return false;

  let merged = false;
  while (bytes > lowTarget && store.view.length > 2) {
    let bestIdx = -1;
    for (let k = 0; k < store.view.length - 1; k++) {
      const [l1, i1] = store.view[k];
      const [l2, i2] = store.view[k + 1];
      if (l1 === l2 && i1 % 2 === 0 && i2 === i1 + 1) {
        const parentKey = `${l1 + 1}:${Math.floor(i1 / 2)}`;
        if (store.nodes.has(parentKey)) {
          bestIdx = k;
          break;
        }
      }
    }
    if (bestIdx === -1) break;
    const [l1, i1] = store.view[bestIdx];
    store.view.splice(bestIdx, 2, [l1 + 1, Math.floor(i1 / 2)]);
    bytes = computeViewBytes(store);
    merged = true;
  }
  return merged;
}

export function forceFoldOldestPair(store) {
  for (let k = 0; k < store.view.length - 1; k++) {
    const [l1, i1] = store.view[k];
    const [l2, i2] = store.view[k + 1];
    if (l1 === l2 && i1 % 2 === 0 && i2 === i1 + 1) {
      const parentKey = `${l1 + 1}:${Math.floor(i1 / 2)}`;
      if (store.nodes.has(parentKey)) {
        store.view.splice(k, 2, [l1 + 1, Math.floor(i1 / 2)]);
        return true;
      }
    }
  }
  return false;
}

export function renderViewLines(store) {
  const out = [];
  for (const [l, idx] of store.view) {
    const node = store.nodes.get(`${l}:${idx}`);
    if (!node) continue;
    const n = 2 ** l;
    const startId = idx * n;
    out.push({ id: startId, n, text: node.text });
  }
  return out;
}

export function computeViewBytes(store) {
  let total = 0;
  for (const line of renderViewLines(store)) {
    total += utf8Bytes(`${line.id}+${line.n}|${line.text}\n`);
  }
  return total;
}

export function zoomRange(store, id, n = 1) {
  if (!Number.isFinite(id) || id < 0) return null;
  if (n > 1) {
    const level = Math.max(1, Math.round(Math.log2(n)));
    const half = 2 ** (level - 1);
    const spanSize = 2 ** level;
    const leftIdx = Math.floor(id / spanSize) * 2;
    const rightIdx = leftIdx + 1;
    const childLevel = level - 1;
    const left = store.nodes.get(`${childLevel}:${leftIdx}`);
    const right = store.nodes.get(`${childLevel}:${rightIdx}`);
    const lines = [];
    if (left) lines.push({ id: leftIdx * half, n: half, text: left.text });
    if (right) lines.push({ id: rightIdx * half, n: half, text: right.text });
    if (!lines.length) return null;
    const text = lines.map((l) => `${l.id}+${l.n}|${l.text}`).join('\n');
    return { text, lines, images: [] };
  }
  const msg = store.messages[id];
  if (!msg) return null;
  return { text: msg.text, lines: [], images: msg.attachments || [] };
}

export function computeStats(store) {
  const viewBytes = computeViewBytes(store);
  return {
    messages: store.messages.length,
    nodes: store.nodes.size,
    view_nodes: store.view.length,
    view_bytes: viewBytes,
    summarized_messages: store.messages.length,
    compaction_view_bytes: Math.min(viewBytes, 16_000),
    batch_pending: false,
    view_saved: true,
    queued: 0,
    active: 0,
    failed: 0,
    view_save_error: null,
    usage: store.usage,
  };
}
