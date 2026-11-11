// Test server for the E2E suite: serves the real page and fakes the gate's upstreams.
//   /api/*    -> small in-memory BE (same verify rules as be/app/server.py, enough for the page)
//   /ai/chat  -> SSE written in chunks, scripted per test through /__test/ai
//   /__test/* -> control: reset, script the next AI replies, read the AI requests received
'use strict';
const http = require('http');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const PRODUCTS = {
  'HD-1001': { sku: 'HD-1001', barcode: '8935000100011', name: 'Tai nghe', category: 'Âm thanh', price: 890000, stock: 25 },
  'CH-2001': { sku: 'CH-2001', barcode: '8935000200018', name: 'Củ sạc', category: 'Phụ kiện', price: 250000, stock: 60 },
  'CB-3001': { sku: 'CB-3001', barcode: '8935000300015', name: 'Cáp USB-C', category: 'Phụ kiện', price: 90000, stock: 120 },
  'MS-4001': { sku: 'MS-4001', barcode: '8935000400012', name: 'Chuột', category: 'Máy tính', price: 320000, stock: 40 },
};
const ORDERS = {
  'SO-1': { order_id: 'SO-1', customer: 'Khách A', items: { 'CB-3001': 1, 'CH-2001': 1, 'HD-1001': 1 } },
  'SO-2': { order_id: 'SO-2', customer: 'Khách B', items: { 'MS-4001': 1 } },
};

let aiScript = [];   // [{ chunks: [string], delayMs, hang }]
let aiRequests = []; // bodies of POST /ai/chat
let aiResults = {};  // request_id -> answer, like the agent's retry cache

const skuOf = (code) => Object.values(PRODUCTS).find((p) => p.barcode === code || p.sku === code);
const json = (res, status, body) => { res.writeHead(status, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(body)); };
const sse = (event, data) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
const readBody = (req) => new Promise((ok) => { let b = ''; req.on('data', (c) => (b += c)); req.on('end', () => ok(b ? JSON.parse(b) : {})); });

function orderView(o) {
  return { order_id: o.order_id, customer: o.customer, status: 'pending',
    items: Object.entries(o.items).map(([sku, quantity]) => ({ sku, barcode: PRODUCTS[sku].barcode, name: PRODUCTS[sku].name, quantity })) };
}

function verify(o, codes) {
  const scanned = {}, unknown = new Set();
  for (const c of codes) { const p = skuOf(c); if (p) scanned[p.sku] = (scanned[p.sku] || 0) + 1; else unknown.add(c); }
  const missing = {}, extra = {};
  for (const [s, q] of Object.entries(o.items)) if ((scanned[s] || 0) < q) missing[s] = q - (scanned[s] || 0);
  for (const [s, n] of Object.entries(scanned)) if (n > (o.items[s] || 0)) extra[s] = n - (o.items[s] || 0);
  return { order_id: o.order_id, items: orderView(o).items.map((i) => ({ ...i, scanned: scanned[i.sku] || 0 })), missing, extra,
    unknown: [...unknown].sort(), complete: !Object.keys(missing).length && !Object.keys(extra).length && !unknown.size };
}

async function aiChat(req, res) {
  const body = await readBody(req);
  aiRequests.push(body);
  res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' });
  res.write(sse('meta', { thread_id: body.thread_id, request_id: body.request_id }));
  if (aiResults[body.request_id]) return res.end(sse('done', { thread_id: body.thread_id, answer: aiResults[body.request_id], tools: [] }));
  const step = aiScript.shift() || { chunks: [sse('done', { thread_id: body.thread_id, answer: 'ok', tools: [] })] };
  // finishedUnseen: the turn completed on the server (answer stored) but the stream died before "done" reached the page
  if (step.finishedUnseen) aiResults[body.request_id] = step.finishedUnseen;
  for (const c of step.chunks) {
    await new Promise((r) => setTimeout(r, step.delayMs || 30));
    res.write(c);
  }
  if (!step.hang) res.end();
}

http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x');
  const p = url.pathname;
  try {
    if (p === '/' || p === '/index.html' || p === '/core.js') {
      const file = p === '/core.js' ? 'core.js' : 'index.html';
      res.writeHead(200, { 'Content-Type': file.endsWith('.js') ? 'text/javascript' : 'text/html; charset=utf-8' });
      return res.end(fs.readFileSync(path.join(ROOT, file)));
    }
    if (p === '/__test/reset') { aiScript = []; aiRequests = []; aiResults = {}; return json(res, 200, {}); }
    if (p === '/__test/ai') { aiScript.push(...(await readBody(req)).steps); return json(res, 200, {}); }
    if (p === '/__test/requests') return json(res, 200, aiRequests);
    if (p === '/ai/chat' && req.method === 'POST') return aiChat(req, res);

    let m;
    if ((m = p.match(/^\/api\/products\/([^/]+)$/))) {
      const prod = skuOf(decodeURIComponent(m[1]));
      return prod ? json(res, 200, prod) : json(res, 404, { detail: 'unknown product' });
    }
    if ((m = p.match(/^\/api\/orders\/([^/]+)$/))) {
      const o = ORDERS[decodeURIComponent(m[1])];
      return o ? json(res, 200, orderView(o)) : json(res, 404, { detail: 'unknown order' });
    }
    if ((m = p.match(/^\/api\/orders\/([^/]+)\/verify$/)) && req.method === 'POST') {
      const o = ORDERS[decodeURIComponent(m[1])];
      return o ? json(res, 200, verify(o, (await readBody(req)).barcodes)) : json(res, 404, { detail: 'unknown order' });
    }
    json(res, 404, { detail: 'not found' });
  } catch (e) {
    json(res, 500, { detail: String(e) });
  }
}).listen(+process.env.PORT || 4173, '127.0.0.1');
