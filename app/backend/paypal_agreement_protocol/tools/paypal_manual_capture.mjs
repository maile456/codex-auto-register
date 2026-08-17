import fs from 'node:fs/promises';
import fssync from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { chromium } from 'file:///home/nonewhite/.npm/_npx/666793a7876f3860/node_modules/js-reverse-mcp/build/src/third_party/index.js';

const startUrl = process.argv[2] || 'about:blank';
const root = process.argv[3] || path.join(process.cwd(), 'captures', `paypal-${new Date().toISOString().replace(/[:.]/g, '-')}`);
const profileDir = path.join(root, 'chrome-profile');
const dirs = {
  root,
  html: path.join(root, 'html'),
  scripts: path.join(root, 'js'),
  bodies: path.join(root, 'network', 'bodies'),
  requests: path.join(root, 'network', 'requests'),
  storage: path.join(root, 'storage'),
  screenshots: path.join(root, 'screenshots'),
};
for (const d of Object.values(dirs)) await fs.mkdir(d, { recursive: true });

const files = {
  events: path.join(root, 'network', 'events.jsonl'),
  console: path.join(root, 'console.jsonl'),
  scripts: path.join(root, 'js', 'scripts.jsonl'),
  snapshots: path.join(root, 'html', 'snapshots.jsonl'),
  summary: path.join(root, 'summary.json'),
  meta: path.join(root, 'metadata.json'),
};

function now() { return new Date().toISOString(); }
function sha256(buf) { return crypto.createHash('sha256').update(buf).digest('hex'); }
function safeName(s, max = 96) {
  return String(s || '')
    .replace(/^https?:\/\//, '')
    .replace(/[^a-zA-Z0-9._-]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, max) || 'item';
}
function mimeToExt(mime = '', url = '') {
  const m = (mime || '').split(';')[0].trim().toLowerCase();
  if (m.includes('javascript') || m.includes('ecmascript')) return '.js';
  if (m === 'text/html' || m === 'application/xhtml+xml') return '.html';
  if (m.includes('json')) return '.json';
  if (m.startsWith('text/')) return '.txt';
  if (m === 'application/xml' || m.endsWith('+xml')) return '.xml';
  if (m === 'image/png') return '.png';
  if (m === 'image/jpeg') return '.jpg';
  if (m === 'image/gif') return '.gif';
  if (m === 'image/webp') return '.webp';
  if (m === 'font/woff2') return '.woff2';
  try {
    const ext = path.extname(new URL(url).pathname);
    if (ext && ext.length <= 8) return ext;
  } catch {}
  return '.bin';
}
function isTextMime(mime = '', url = '') {
  const m = (mime || '').toLowerCase();
  return m.startsWith('text/') || m.includes('json') || m.includes('javascript') || m.includes('xml') || m.includes('graphql') || /\.(js|mjs|json|html|css|txt|xml)(\?|$)/i.test(url);
}
async function appendJsonl(file, obj) {
  await fs.appendFile(file, JSON.stringify(obj) + '\n').catch(() => {});
}
async function writeJson(file, obj) {
  await fs.writeFile(file, JSON.stringify(obj, null, 2));
}
function withTimeout(p, ms, label) {
  return Promise.race([
    p,
    new Promise((_, reject) => setTimeout(() => reject(new Error(`timeout:${label}`)), ms)),
  ]);
}

const requestMap = new WeakMap();
const responseByReqId = new Map();
const requestRecords = [];
const pageLastHtmlHash = new Map();
const scriptHashes = new Set();
let reqSeq = 0;
let pageSeq = 0;
let scriptSeq = 0;
let stopping = false;

await writeJson(files.meta, {
  startedAt: now(),
  startUrl,
  root,
  profileDir,
  recorder: 'paypal_manual_capture.mjs',
  note: 'Visible Chrome capture: HTML snapshots, loaded JS sources, request/response metadata and available bodies.',
});

console.log(`[capture] output: ${root}`);
console.log(`[capture] launching visible Chrome...`);

const context = await chromium.launchPersistentContext(profileDir, {
  channel: 'chrome',
  headless: false,
  viewport: null,
  ignoreHTTPSErrors: true,
  args: [
    '--test-type',
    '--hide-crash-restore-bubble',
    '--start-maximized',
    '--disable-search-engine-choice-screen',
  ],
});

async function saveHtmlSnapshot(page, reason) {
  if (stopping && reason !== 'shutdown') return;
  try {
    const url = page.url();
    if (!url || url === 'about:blank') return;
    const html = await withTimeout(page.content(), 5000, 'page.content');
    const buf = Buffer.from(html, 'utf8');
    const hash = sha256(buf);
    const pageId = page.__capturePageId || 0;
    const last = pageLastHtmlHash.get(pageId);
    if (last === hash && reason !== 'shutdown') return;
    pageLastHtmlHash.set(pageId, hash);
    const name = `${String(Date.now()).slice(-8)}_p${pageId}_${safeName(url)}_${hash.slice(0, 10)}.html`;
    const out = path.join(dirs.html, name);
    await fs.writeFile(out, buf);
    await appendJsonl(files.snapshots, { time: now(), reason, pageId, url, path: out, bytes: buf.length, sha256: hash });
    console.log(`[html] ${reason} p${pageId} ${url} -> ${name}`);
  } catch (e) {
    await appendJsonl(files.snapshots, { time: now(), reason, error: String(e?.message || e) });
  }
}

async function attachPage(page) {
  if (page.__captureAttached) return;
  page.__captureAttached = true;
  page.__capturePageId = ++pageSeq;
  const pageId = page.__capturePageId;
  console.log(`[page] attach p${pageId}: ${page.url()}`);

  page.on('domcontentloaded', () => setTimeout(() => saveHtmlSnapshot(page, 'domcontentloaded'), 300));
  page.on('load', () => setTimeout(() => saveHtmlSnapshot(page, 'load'), 500));
  page.on('framenavigated', frame => {
    if (frame === page.mainFrame()) setTimeout(() => saveHtmlSnapshot(page, 'framenavigated'), 800);
  });
  page.on('console', async msg => {
    await appendJsonl(files.console, {
      time: now(), pageId, type: msg.type(), text: msg.text(), location: msg.location(), url: page.url(),
    });
  });
  page.on('pageerror', async err => {
    await appendJsonl(files.console, { time: now(), pageId, type: 'pageerror', text: String(err?.stack || err), url: page.url() });
  });

  // Capture script sources, including inline/eval/bundled sources that may not be directly visible as network JS files.
  try {
    const cdp = await context.newCDPSession(page);
    await cdp.send('Debugger.enable');
    cdp.on('Debugger.scriptParsed', async ev => {
      try {
        if (!ev.scriptId) return;
        const src = await withTimeout(cdp.send('Debugger.getScriptSource', { scriptId: ev.scriptId }), 4000, 'getScriptSource');
        const code = src?.scriptSource || '';
        if (!code) return;
        const buf = Buffer.from(code, 'utf8');
        const hash = sha256(buf);
        if (scriptHashes.has(hash)) return;
        scriptHashes.add(hash);
        const url = ev.url || `inline_p${pageId}_${ev.scriptId}`;
        const name = `${String(++scriptSeq).padStart(5, '0')}_p${pageId}_${safeName(url)}_${hash.slice(0, 10)}.js`;
        const out = path.join(dirs.scripts, name);
        await fs.writeFile(out, buf);
        await appendJsonl(files.scripts, {
          time: now(), pageId, source: 'Debugger.scriptParsed', url: ev.url || null, scriptId: ev.scriptId,
          startLine: ev.startLine, startColumn: ev.startColumn, endLine: ev.endLine, endColumn: ev.endColumn,
          path: out, bytes: buf.length, sha256: hash,
        });
      } catch (e) {
        await appendJsonl(files.scripts, { time: now(), pageId, source: 'Debugger.scriptParsed', scriptId: ev.scriptId, url: ev.url || null, error: String(e?.message || e) });
      }
    });
  } catch (e) {
    await appendJsonl(files.scripts, { time: now(), pageId, source: 'Debugger.attach', error: String(e?.message || e) });
  }

  setTimeout(() => saveHtmlSnapshot(page, 'attach'), 1000);
}

context.on('page', attachPage);
for (const p of context.pages()) await attachPage(p);

context.on('request', async request => {
  const id = ++reqSeq;
  requestMap.set(request, id);
  let pageId = null;
  try { pageId = request.frame()?.page()?.__capturePageId || null; } catch {}
  const headers = await request.allHeaders().catch(() => request.headers());
  const rec = {
    id, time: now(), type: 'request', pageId,
    method: request.method(), url: request.url(), resourceType: request.resourceType(),
    isNavigationRequest: request.isNavigationRequest(), headers,
  };
  const pd = request.postDataBuffer?.();
  if (pd && pd.length) {
    const ext = isTextMime(headers['content-type'] || '', request.url()) ? '.txt' : '.bin';
    const bodyPath = path.join(dirs.requests, `${String(id).padStart(5, '0')}_${request.method()}_${safeName(request.url())}${ext}`);
    await fs.writeFile(bodyPath, pd);
    rec.postData = { path: bodyPath, bytes: pd.length, sha256: sha256(pd), textPreview: isTextMime(headers['content-type'] || '', request.url()) ? pd.toString('utf8').slice(0, 2000) : undefined };
  } else {
    const post = request.postData?.();
    if (post) {
      const buf = Buffer.from(post, 'utf8');
      const bodyPath = path.join(dirs.requests, `${String(id).padStart(5, '0')}_${request.method()}_${safeName(request.url())}.txt`);
      await fs.writeFile(bodyPath, buf);
      rec.postData = { path: bodyPath, bytes: buf.length, sha256: sha256(buf), textPreview: post.slice(0, 2000) };
    }
  }
  requestRecords.push(rec);
  await appendJsonl(files.events, rec);
  console.log(`[req ${id}] ${request.method()} ${request.resourceType()} ${request.url()}`);
});

context.on('response', async response => {
  const request = response.request();
  const id = requestMap.get(request) || ++reqSeq;
  if (!requestMap.has(request)) requestMap.set(request, id);
  const headers = await response.allHeaders().catch(() => response.headers());
  let pageId = null;
  try { pageId = request.frame()?.page()?.__capturePageId || null; } catch {}
  const rec = {
    id, time: now(), type: 'response', pageId, url: response.url(), status: response.status(), statusText: response.statusText(),
    resourceType: request.resourceType(), method: request.method(), headers,
  };
  responseByReqId.set(id, { rec, response });
  await appendJsonl(files.events, rec);
});

context.on('requestfinished', async request => {
  const id = requestMap.get(request) || ++reqSeq;
  const response = await request.response().catch(() => null);
  const finishRec = { id, time: now(), type: 'requestfinished', url: request.url(), method: request.method(), resourceType: request.resourceType() };
  if (response) {
    try {
      const headers = await response.allHeaders().catch(() => response.headers());
      const ct = headers['content-type'] || headers['Content-Type'] || '';
      const body = await withTimeout(response.body(), 12000, 'response.body');
      if (body && body.length) {
        const ext = mimeToExt(ct, response.url());
        const prefix = request.resourceType() === 'script' ? 'script' : request.resourceType();
        const outDir = request.resourceType() === 'script' ? dirs.scripts : dirs.bodies;
        const out = path.join(outDir, `${String(id).padStart(5, '0')}_${prefix}_${response.status()}_${safeName(response.url())}_${sha256(body).slice(0, 10)}${ext}`);
        await fs.writeFile(out, body);
        finishRec.responseBody = { path: out, bytes: body.length, sha256: sha256(body), contentType: ct, base64: !isTextMime(ct, response.url()) };
        if (request.resourceType() === 'script') {
          await appendJsonl(files.scripts, { time: now(), pageId: null, source: 'network', url: response.url(), path: out, bytes: body.length, sha256: sha256(body), contentType: ct });
        }
      }
    } catch (e) {
      finishRec.responseBodyError = String(e?.message || e);
    }
  }
  await appendJsonl(files.events, finishRec);
});

context.on('requestfailed', async request => {
  const id = requestMap.get(request) || ++reqSeq;
  await appendJsonl(files.events, { id, time: now(), type: 'requestfailed', url: request.url(), method: request.method(), resourceType: request.resourceType(), failure: request.failure() });
});

const htmlInterval = setInterval(async () => {
  for (const p of context.pages()) await saveHtmlSnapshot(p, 'interval');
}, 5000);

async function finalScreenshot() {
  for (const p of context.pages()) {
    try {
      const pageId = p.__capturePageId || 0;
      const out = path.join(dirs.screenshots, `final_p${pageId}_${Date.now()}.png`);
      await p.screenshot({ path: out, fullPage: true, timeout: 10000 });
    } catch {}
  }
}

async function shutdown(reason = 'manual') {
  if (stopping) return;
  stopping = true;
  clearInterval(htmlInterval);
  console.log(`[capture] stopping (${reason})...`);
  for (const p of context.pages()) await saveHtmlSnapshot(p, 'shutdown');
  await finalScreenshot();
  try { await writeJson(path.join(dirs.storage, 'cookies.json'), await context.cookies()); } catch {}
  try {
    const pages = context.pages().map(p => ({ pageId: p.__capturePageId || 0, url: p.url() }));
    await writeJson(files.summary, { finishedAt: now(), reason, root, pages, requests: reqSeq, scripts: scriptHashes.size });
  } catch {}
  try { await context.close(); } catch {}
  console.log(`[capture] saved to ${root}`);
  process.exit(0);
}

process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('uncaughtException', async err => {
  console.error('[capture] uncaught', err);
  await appendJsonl(files.console, { time: now(), type: 'recorder-uncaught', text: String(err?.stack || err) });
});

let page = context.pages().find(p => p.url() === 'about:blank') || context.pages()[0] || await context.newPage();
await attachPage(page);
await page.goto(startUrl, { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(err => console.error('[capture] initial goto error:', err.message));
console.log('[capture] 已开始记录。请在弹出的 Chrome 里手动完成流程。完成后回到 Codex 告诉我“好了/完成”，我会停止并整理捕获文件。');
// Keep process alive.
await new Promise(() => {});
