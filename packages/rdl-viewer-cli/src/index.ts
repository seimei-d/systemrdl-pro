#!/usr/bin/env bun
/**
 * rdl-viewer — standalone browser-served SystemRDL memory-map viewer.
 *
 * Usage:
 *   rdl-viewer <file.rdl> [--port 5173] [--no-open] [--python <path>]
 *
 * Architecture (Decision 1C: assets bundled, served from this process):
 *
 *   ┌──────────┐  fs.watch  ┌────────────────┐  spawn   ┌─────────────────┐
 *   │ user.rdl │ ─────────▶ │  rdl-viewer    │ ───────▶ │ python -m       │
 *   └──────────┘            │  (Bun HTTP)    │ ◀─JSON── │ systemrdl_lsp   │
 *                           └────────────────┘          │ .dump file.rdl  │
 *                                  │                    └─────────────────┘
 *                                  │ HTTP+SSE
 *                                  ▼
 *                           ┌────────────────┐
 *                           │ browser SPA    │
 *                           └────────────────┘
 *
 * The SPA polls /tree on connect and listens to /events (SSE) for pushes
 * triggered by file save. The renderer mirrors the VSCode webview's vanilla
 * DOM walking skeleton — same tree+detail-pane layout, no Svelte until W5.
 */

import { spawnSync, spawn } from 'node:child_process';
// `spawnSync` is still used for the synchronous python/module probes at startup —
// those run once, the output is tiny, and we can block the event loop for a
// few hundred ms. The recompile path uses async `spawn` to dodge maxBuffer.
import { existsSync, watch } from 'node:fs';
import path from 'node:path';
import process from 'node:process';

type CliArgs = {
  file: string;
  port: number;
  open: boolean;
  python: string | undefined;
};

function parseArgs(argv: string[]): CliArgs {
  const args: CliArgs = { file: '', port: 5173, open: true, python: undefined };
  const rest: string[] = [];
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--port' || a === '-p') {
      args.port = Number(argv[++i]);
    } else if (a === '--no-open') {
      args.open = false;
    } else if (a === '--python') {
      args.python = argv[++i];
    } else if (a === '--help' || a === '-h') {
      printHelp();
      process.exit(0);
    } else if (a === '--version' || a === '-v') {
      console.log('rdl-viewer 0.1.0');
      process.exit(0);
    } else if (a.startsWith('-')) {
      console.error(`unknown flag: ${a}`);
      process.exit(2);
    } else {
      rest.push(a);
    }
  }
  if (rest.length !== 1) {
    printHelp();
    process.exit(2);
  }
  args.file = path.resolve(rest[0]);
  return args;
}

function printHelp(): void {
  console.log(`rdl-viewer — standalone SystemRDL memory-map viewer

Usage:
  rdl-viewer <file.rdl> [--port 5173] [--no-open] [--python <path>]

Options:
  --port, -p <n>     HTTP port (default: 5173)
  --no-open          Don't auto-open the browser
  --python <path>    Python interpreter with systemrdl-lsp installed
  --help, -h         Show this help
  --version, -v      Show version

Requires \`systemrdl-lsp\` available to the chosen Python (pip install systemrdl-lsp).
`);
}

function resolvePython(explicit: string | undefined): string {
  // Resolution order:
  //   1. --python flag (explicit win)
  //   2. $VIRTUAL_ENV/bin/python — picks up `uv run rdl-viewer …` and the
  //      familiar ``source .venv/bin/activate`` workflow without an extra flag
  //   3. python3, python on PATH (last resort; system python rarely has
  //      systemrdl-lsp installed but we still try)
  const venv = process.env.VIRTUAL_ENV;
  const venvPy = venv ? path.join(venv, 'bin', process.platform === 'win32' ? 'python.exe' : 'python') : null;
  const candidates = explicit
    ? [explicit]
    : [venvPy, 'python3', 'python'].filter((p): p is string => !!p);
  for (const c of candidates) {
    const r = spawnSync(c, ['--version'], { stdio: 'pipe' });
    if (r.status === 0) return c;
  }
  console.error(
    'rdl-viewer: could not find python. Pass --python /path/to/python with systemrdl-lsp installed.',
  );
  process.exit(2);
}

function checkLspModule(python: string): boolean {
  const r = spawnSync(python, ['-c', 'import systemrdl_lsp; print(systemrdl_lsp.__version__)'], {
    stdio: 'pipe',
  });
  return r.status === 0;
}

type DumpResult = { ok: boolean; tree: unknown | null; stderr: string };

const DUMP_TIMEOUT_MS = 15_000;

/**
 * Spawn ``python -m systemrdl_lsp.dump`` and collect stdout/stderr without the
 * 1 MB ``spawnSync`` ``maxBuffer`` ceiling — a 1000-register file emits ~8 MB
 * of JSON and ``spawnSync`` would silently kill the child mid-stream. We use
 * the async ``spawn`` + chunk concatenation, with a wall-clock timeout that
 * matches the LSP's own ``ELABORATION_TIMEOUT_SECONDS``.
 */
function runDump(python: string, file: string): Promise<DumpResult> {
  return new Promise((resolve) => {
    const child = spawn(python, ['-m', 'systemrdl_lsp.dump', file], { stdio: 'pipe' });
    const outChunks: Buffer[] = [];
    const errChunks: Buffer[] = [];
    let settled = false;

    const finish = (r: DumpResult) => {
      if (settled) return;
      settled = true;
      resolve(r);
    };

    const killTimer = setTimeout(() => {
      child.kill('SIGTERM');
      finish({ ok: false, tree: null, stderr: `dump exceeded ${DUMP_TIMEOUT_MS / 1000}s wall-clock` });
    }, DUMP_TIMEOUT_MS);

    child.stdout?.on('data', (c: Buffer) => outChunks.push(c));
    child.stderr?.on('data', (c: Buffer) => errChunks.push(c));
    child.on('error', err => {
      clearTimeout(killTimer);
      finish({ ok: false, tree: null, stderr: `dump spawn failed: ${err}` });
    });
    child.on('close', (code) => {
      clearTimeout(killTimer);
      const stdout = Buffer.concat(outChunks).toString('utf8');
      const stderr = Buffer.concat(errChunks).toString('utf8');
      let tree: unknown = null;
      if (stdout.trim().length > 0) {
        try {
          tree = JSON.parse(stdout);
        } catch (e) {
          finish({
            ok: false,
            tree: null,
            stderr: `dump JSON parse failed: ${e}\n${stdout.slice(0, 200)}\n${stderr}`,
          });
          return;
        }
      }
      // exit 0 → ok; 1 → library file (envelope still valid); 2 → parse errors
      // (envelope marked stale=true, still useful as last-good).
      finish({ ok: code === 0, tree, stderr });
    });
  });
}

// ---------------------------------------------------------------------------
// SSE broker
// ---------------------------------------------------------------------------

const sseClients = new Set<ReadableStreamDefaultController<string>>();

function broadcast(message: string): void {
  const dead: ReadableStreamDefaultController<string>[] = [];
  for (const c of sseClients) {
    try {
      c.enqueue(`data: ${message}\n\n`);
    } catch {
      dead.push(c);
    }
  }
  for (const c of dead) sseClients.delete(c);
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

const args = parseArgs(process.argv);
if (!existsSync(args.file)) {
  console.error(`rdl-viewer: file not found: ${args.file}`);
  process.exit(2);
}

const python = resolvePython(args.python);
if (!checkLspModule(python)) {
  console.error(
    `rdl-viewer: 'systemrdl-lsp' not installed in ${python}.\n` +
    `Install: ${python} -m pip install systemrdl-lsp`,
  );
  process.exit(2);
}

let latestTree: unknown = null;
let latestStderr = '';

// Debounce concurrent refreshes — fs.watch can fire 2–3 times for an atomic
// save (write + rename), and large files make each dump take seconds. We
// queue at most one follow-up: if a save lands while the previous dump is
// still running, we re-run once it finishes. Subsequent saves coalesce.
let refreshInFlight = false;
let refreshQueued = false;

async function refresh(): Promise<void> {
  if (refreshInFlight) {
    refreshQueued = true;
    return;
  }
  refreshInFlight = true;
  try {
    const r = await runDump(python, args.file);
    latestTree = r.tree;
    latestStderr = r.stderr;
    if (r.tree !== null) broadcast(JSON.stringify(r.tree));
    if (r.stderr.trim().length > 0) {
      process.stderr.write(`[${new Date().toISOString()}] systemrdl-lsp:\n${r.stderr}`);
    }
  } finally {
    refreshInFlight = false;
    if (refreshQueued) {
      refreshQueued = false;
      void refresh();
    }
  }
}

void refresh();

let watchTimer: ReturnType<typeof setTimeout> | undefined;
const watcher = watch(args.file, () => {
  // fs.watch fires twice for atomic-save (rename+create on macOS) — debounce.
  if (watchTimer) clearTimeout(watchTimer);
  watchTimer = setTimeout(() => refresh(), 120);
});

process.on('SIGINT', () => {
  watcher.close();
  process.exit(0);
});

const server = Bun.serve({
  port: args.port,
  async fetch(req) {
    const url = new URL(req.url);
    if (url.pathname === '/' || url.pathname === '/index.html') {
      return new Response(renderHtml(path.basename(args.file)), {
        headers: { 'content-type': 'text/html; charset=utf-8' },
      });
    }
    if (url.pathname === '/tree') {
      return new Response(JSON.stringify(latestTree ?? { schemaVersion: '0.1.0', roots: [] }), {
        headers: { 'content-type': 'application/json; charset=utf-8' },
      });
    }
    if (url.pathname === '/diagnostics') {
      return new Response(latestStderr, {
        headers: { 'content-type': 'text/plain; charset=utf-8' },
      });
    }
    if (url.pathname === '/events') {
      const stream = new ReadableStream<string>({
        start(controller) {
          sseClients.add(controller);
          controller.enqueue(`event: ready\ndata: 1\n\n`);
          if (latestTree !== null) {
            controller.enqueue(`data: ${JSON.stringify(latestTree)}\n\n`);
          }
        },
        cancel(controller) {
          sseClients.delete(controller);
        },
      });
      return new Response(stream as unknown as ReadableStream<Uint8Array>, {
        headers: {
          'content-type': 'text/event-stream',
          'cache-control': 'no-cache',
          connection: 'keep-alive',
        },
      });
    }
    return new Response('not found', { status: 404 });
  },
});

const url = `http://localhost:${server.port}/`;
console.log(`rdl-viewer: serving ${args.file}`);
console.log(`  → ${url}`);
console.log(`  Ctrl-C to stop.`);

if (args.open) {
  const opener =
    process.platform === 'darwin' ? 'open' :
    process.platform === 'win32' ? 'cmd' :
    'xdg-open';
  const openArgs = process.platform === 'win32' ? ['/c', 'start', url] : [url];
  // Headless boxes (WSL2 without WSLg, CI, ssh -X off) don't have ``xdg-open``.
  // ``child.on('error')`` swallows the async ENOENT that would otherwise crash
  // Bun via an unhandled error event — the synchronous try/catch alone doesn't
  // catch it. Falling back to "user opens the printed URL manually" is fine.
  try {
    const child = spawn(opener, openArgs, { stdio: 'ignore', detached: true });
    child.on('error', () => {
      console.error(`rdl-viewer: could not auto-open browser (${opener}). Open ${url} yourself.`);
    });
    child.unref();
  } catch {
    console.error(`rdl-viewer: could not auto-open browser. Open ${url} yourself.`);
  }
}

// ---------------------------------------------------------------------------
// Inline SPA — defined before renderHtml so the template literal sees it.
// Mirrors the VSCode webview's vanilla DOM walking skeleton minus VSCode-specific
// bits (no acquireVsCodeApi, no postMessage 'reveal'). Once rdl-viewer-core lands,
// both surfaces import it; until then this is a separate copy.
// ---------------------------------------------------------------------------

const RENDER_JS = `
let state = { roots: [], activeRootIndex: 0, selectedRegKey: null, filter: '', collapsedKeys: new Set() };

function setConn(cls, text) {
  const el = document.getElementById('conn');
  el.className = 'conn ' + cls;
  el.textContent = text;
}

function load() {
  fetch('/tree').then(r => r.json()).then(applyTree).catch(err => {
    setConn('err', 'fetch failed');
    console.error(err);
  });
}

function connectEvents() {
  const es = new EventSource('/events');
  es.addEventListener('ready', () => setConn('ok', 'live'));
  es.onmessage = ev => {
    try { applyTree(JSON.parse(ev.data)); }
    catch (e) { console.error('bad SSE frame', e); }
  };
  es.onerror = () => setConn('err', 'disconnected — retrying');
}

document.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && (e.key === 'f' || e.key === 'F')) {
    const bar = document.getElementById('filter-bar');
    const input = document.getElementById('filter-input');
    bar.classList.add('shown');
    input.focus();
    input.select();
    e.preventDefault();
  } else if (e.key === 'Escape') {
    const input = document.getElementById('filter-input');
    if (document.activeElement === input || state.filter) {
      input.value = '';
      state.filter = '';
      document.getElementById('filter-bar').classList.remove('shown');
      input.blur();
      renderTree();
      e.preventDefault();
    }
  }
});

document.getElementById('filter-input').addEventListener('input', (e) => {
  state.filter = e.target.value.toLowerCase();
  renderTree();
});

function applyTree(tree) {
  state.roots = tree.roots || [];
  if (state.activeRootIndex >= state.roots.length) state.activeRootIndex = 0;
  document.getElementById('stale-bar').classList.toggle('shown', !!tree.stale);
  document.getElementById('stale-text').textContent = tree.stale
    ? 'Showing last good elaboration · current parse failed'
    : 'Showing last good elaboration';
  if (!state.roots.length) { showEmpty(); return; }
  const root = state.roots[state.activeRootIndex];
  if (!state.selectedRegKey || !findRegByKey(root, state.selectedRegKey)) {
    const firstPath = findFirstRegPath(root, [root.name]);
    state.selectedRegKey = firstPath ? firstPath.key : null;
  }
  renderTabs();
  renderTree();
  renderDetail();
}

function renderTabs() {
  const host = document.getElementById('tabs');
  host.innerHTML = '';
  state.roots.forEach((r, i) => {
    const t = document.createElement('div');
    t.className = 'tab' + (i === state.activeRootIndex ? ' active' : '');
    t.textContent = r.name;
    t.title = (r.type ? r.type + ' · ' : '') + r.address;
    t.addEventListener('click', () => {
      if (i === state.activeRootIndex) return;
      state.activeRootIndex = i;
      const first = findFirstRegPath(state.roots[i], [state.roots[i].name]);
      state.selectedRegKey = first ? first.key : null;
      renderTabs(); renderTree(); renderDetail();
    });
    host.appendChild(t);
  });
}

function renderTree() {
  const root = state.roots[state.activeRootIndex];
  const host = document.getElementById('tree-host');
  host.innerHTML = '';
  const tree = document.createElement('div');
  tree.className = 'tree';
  // Render the root itself as the topmost row so the user can fold the entire
  // tab content with one click on its caret.
  walk(root, tree, 0, []);
  host.appendChild(tree);
  const hint = document.getElementById('filter-hint');
  if (state.filter) {
    const visible = host.querySelectorAll('.row:not(.container)').length;
    hint.textContent = visible + ' match' + (visible === 1 ? '' : 'es');
  } else { hint.textContent = ''; }
  const sel = host.querySelector('.row.selected');
  if (sel) sel.scrollIntoView({ block: 'nearest' });
}

function looksLikeHex(s) {
  if (!s) return false;
  return /^(0x)?[0-9a-f_]+$/i.test(s);
}
function normalizeAddr(s) {
  return String(s || '').toLowerCase().replace(/^0x/, '').replace(/_/g, '');
}
function subtreeMatches(node, filter) {
  if (!filter) return true;
  const lower = filter.toLowerCase();
  const hexFilter = looksLikeHex(filter) ? normalizeAddr(filter) : null;
  if (node.kind === 'reg') {
    if (node.name.toLowerCase().includes(lower)) return true;
    if (hexFilter && normalizeAddr(node.address).includes(hexFilter)) return true;
    return (node.fields || []).some(f =>
      f.name.toLowerCase().includes(lower) ||
      (f.access && f.access.toLowerCase().includes(lower))
    );
  }
  if (node.name && node.name.toLowerCase().includes(lower)) return true;
  if (hexFilter && normalizeAddr(node.address).includes(hexFilter)) return true;
  return (node.children || []).some(c => subtreeMatches(c, filter));
}

function walkChildren(parent, host, depth, segs) {
  (parent.children || []).forEach(child => walk(child, host, depth, segs));
}

function walk(node, host, depth, segs) {
  const indent = 'indent-' + Math.min(depth, 3);
  if (state.filter && !subtreeMatches(node, state.filter)) return;
  if (node.kind === 'addrmap' || node.kind === 'regfile') {
    const containerKey = segs.concat([node.name]).join('.');
    const isCollapsed = !state.filter && state.collapsedKeys.has(containerKey);
    const caretChar = isCollapsed ? '▶' : '▼';
    const row = document.createElement('div');
    row.className = 'row container ' + indent;
    const kindLabel = node.kind + (node.type ? ' (' + node.type + ')' : '');
    row.innerHTML = '<span class="caret caret-toggle" title="' +
      (isCollapsed ? 'Click to expand' : 'Click to collapse') + '">' + caretChar + '</span>' +
      '<span class="addr">' + node.address + '</span>' +
      '<span class="name">' + escapeHtml(node.name) + '</span>' +
      '<span class="access">' + escapeHtml(kindLabel) + '</span>';
    const caretEl = row.querySelector('.caret-toggle');
    if (caretEl) {
      caretEl.addEventListener('click', (e) => {
        e.stopPropagation();
        if (state.collapsedKeys.has(containerKey)) state.collapsedKeys.delete(containerKey);
        else state.collapsedKeys.add(containerKey);
        renderTree();
      });
    }
    row.title = 'Click caret to fold';
    host.appendChild(row);
    if (!isCollapsed) {
      walkChildren(node, host, depth + 1, segs.concat([node.name]));
    }
    return;
  }
  if (node.kind === 'reg') {
    const path = segs.concat([node.name]);
    const key = path.join('.');
    const selected = state.selectedRegKey === key;
    const row = document.createElement('div');
    row.className = 'row ' + indent + (selected ? ' selected' : '');
    row.innerHTML = '<span class="caret"> </span>' +
      '<span class="addr">' + node.address + '</span>' +
      '<span class="name">' + escapeHtml(node.name) + '</span>' +
      '<span class="access">' + (node.accessSummary || '') + '</span>';
    row.addEventListener('click', () => {
      state.selectedRegKey = key;
      renderTree(); renderDetail();
    });
    host.appendChild(row);
  }
}

function findFirstRegPath(node, segs) {
  if (node.kind === 'reg') return { reg: node, path: segs, key: segs.join('.') };
  for (const c of node.children || []) {
    const r = findFirstRegPath(c, segs.concat([c.name]));
    if (r) return r;
  }
  return null;
}

function findRegByKey(rootNode, key) {
  function walk(node, segs) {
    if (node.kind === 'reg') {
      const k = segs.join('.');
      return k === key ? { reg: node, path: segs } : null;
    }
    for (const c of node.children || []) {
      const r = walk(c, segs.concat([c.name]));
      if (r) return r;
    }
    return null;
  }
  return walk(rootNode, [rootNode.name]);
}

function renderDetail() {
  const host = document.getElementById('detail');
  if (!state.selectedRegKey) {
    host.innerHTML = '<div class="placeholder">Select a register to see details.</div>';
    return;
  }
  const found = findRegByKey(state.roots[state.activeRootIndex], state.selectedRegKey);
  if (!found) {
    host.innerHTML = '<div class="placeholder">Selected register no longer exists.</div>';
    return;
  }
  const reg = found.reg;
  const path = found.path.join('.');
  let html = '';
  html += '<h2>' + escapeHtml(reg.name) + '</h2>';
  if (reg.displayName && reg.displayName !== reg.name) {
    html += '<div class="display-name">' + escapeHtml(reg.displayName) + '</div>';
  }
  html += '<div class="breadcrumb">' + escapeHtml(path) + '</div>';
  html += '<div class="meta">';
  html += '<span class="k">Address</span><span class="v">' + reg.address + '</span>';
  html += '<span class="k">Width</span><span class="v">' + reg.width + '</span>';
  html += '<span class="k">Reset</span><span class="v">' + (reg.reset !== undefined ? reg.reset : '—') + '</span>';
  html += '<span class="k">Access</span><span class="v">' + (reg.accessSummary || '—') + '</span>';
  html += '</div>';
  if (reg.desc) html += '<div class="desc">' + escapeHtml(reg.desc) + '</div>';
  html += '<div class="fields-title">Bit fields</div>';
  (reg.fields || []).forEach(f => {
    const acc = (f.access || 'na').toLowerCase();
    const blurb = f.desc || (f.displayName !== f.name ? f.displayName : '') || '';
    html += '<div class="field">' +
      '<b>[' + f.msb + ':' + f.lsb + ']</b>' +
      '<b>' + escapeHtml(f.name) + '</b>' +
      '<span class="pill ' + acc + '">' + acc.toUpperCase() + '</span>' +
      '<span>' + (f.reset || '—') + '</span>' +
      '<span class="desc">' + escapeHtml(blurb) + '</span>' +
      '</div>';
  });
  if (reg.source) {
    const fileName = (reg.source.uri || '').split('/').pop() || reg.source.uri;
    html += '<div class="src-link">→ ' + escapeHtml(fileName) + ':' + ((reg.source.line || 0) + 1) + '</div>';
  }
  host.innerHTML = html;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}

function showEmpty() {
  document.getElementById('tabs').innerHTML = '';
  document.getElementById('tree-host').innerHTML =
    '<div class="empty"><h2>No top-level addrmap found</h2>' +
    '<p>The file has no top-level <code>addrmap</code>, or the latest compile failed. ' +
    'See terminal for diagnostics.</p></div>';
  document.getElementById('detail').innerHTML = '<div class="placeholder">No selection.</div>';
}

load();
connectEvents();
`;

function renderHtml(filename: string): string {
  return /* html */ `<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SystemRDL — ${escapeAttr(filename)}</title>
<style>
  :root {
    color-scheme: dark light;
    --rdl-bg: #1e1e1e;
    --rdl-panel: #252526;
    --rdl-chrome: #2d2d30;
    --rdl-border: #3c3c3c;
    --rdl-fg: #d4d4d4;
    --rdl-dim: #858585;
    --rdl-selected: #213c5a;
    --rdl-accent: #4a9eff;
    --rdl-warning: #d7a85a;
    --rdl-acc-ro:  #8aa6b8;
    --rdl-acc-rw:  #6fb98f;
    --rdl-acc-w1c: #d7a85a;
    --rdl-acc-wo:  #7a87b8;
    --rdl-acc-rsv: #5a3a3a;
    --rdl-font-chrome: 'Inter', system-ui, sans-serif;
    --rdl-font-mono: 'JetBrains Mono', 'SF Mono', Consolas, monospace;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --rdl-bg: #ffffff;
      --rdl-panel: #f5f5f5;
      --rdl-chrome: #ececec;
      --rdl-border: #d4d4d4;
      --rdl-fg: #1a1a1a;
      --rdl-dim: #6b6b6b;
      --rdl-selected: #d6e7fa;
      --rdl-accent: #0066cc;
      --rdl-warning: #b87a18;
      --rdl-acc-ro:  #5a7a90;
      --rdl-acc-rw:  #3a8a5f;
      --rdl-acc-w1c: #b87a18;
      --rdl-acc-wo:  #4a5a90;
      --rdl-acc-rsv: #8a3a3a;
    }
  }
  *, *::before, *::after { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--rdl-bg); color: var(--rdl-fg);
    font-family: var(--rdl-font-chrome); font-size: 14px; height: 100vh; }
  body { display: grid; grid-template-rows: auto auto auto 1fr; min-height: 0; }
  .topbar { background: var(--rdl-chrome); padding: 8px 14px;
    border-bottom: 1px solid var(--rdl-border);
    display: flex; align-items: center; gap: 12px; font-size: 13px; }
  .topbar .title { font-family: var(--rdl-font-mono); color: var(--rdl-fg); }
  .topbar .conn { color: var(--rdl-dim); margin-left: auto; font-size: 12px; }
  .topbar .conn.ok::before { content: '●  '; color: var(--rdl-acc-rw); }
  .topbar .conn.err::before { content: '●  '; color: #d75a5a; }
  .stale-bar { background: rgba(215,168,90,0.12); border-bottom: 1px solid var(--rdl-warning);
    color: var(--rdl-warning); padding: 7px 14px; font-size: 13px;
    display: none; align-items: center; gap: 8px; }
  .stale-bar.shown { display: flex; }
  .tabs { display: flex; border-bottom: 1px solid var(--rdl-border); background: var(--rdl-chrome);
    overflow-x: auto; }
  .tab { padding: 9px 16px; font-size: 13px; color: var(--rdl-dim); cursor: pointer;
    border-right: 1px solid var(--rdl-border); white-space: nowrap; user-select: none; }
  .tab:hover { color: var(--rdl-fg); }
  .tab.active { color: var(--rdl-fg); background: var(--rdl-bg);
    border-bottom: 2px solid var(--rdl-accent); margin-bottom: -1px; }
  .body { display: grid;
    grid-template-rows: minmax(120px, min(50%, max-content)) 1fr;
    min-height: 0; }
  .tree-pane { display: grid; grid-template-rows: auto 1fr; min-height: 0;
    border-bottom: 1px solid var(--rdl-border); }
  .filter-bar { padding: 6px 12px; border-bottom: 1px solid var(--rdl-border);
    background: var(--rdl-panel); display: none; }
  .filter-bar.shown { display: block; }
  .filter-bar input { width: 100%; box-sizing: border-box; background: var(--rdl-bg);
    border: 1px solid var(--rdl-border); color: var(--rdl-fg);
    padding: 5px 9px; font-size: 13px; font-family: var(--rdl-font-chrome);
    outline: none; border-radius: 2px; }
  .filter-bar input:focus { border-color: var(--rdl-accent); }
  .filter-hint { color: var(--rdl-dim); font-size: 11px; margin-top: 4px; }
  .tree-host { overflow: auto; padding: 8px 0; min-height: 0; }
  .tree { font-family: var(--rdl-font-mono); font-size: 13px; }
  .row { display: grid; grid-template-columns: 28px 140px 1fr 100px; gap: 12px;
    align-items: baseline; padding: 3px 16px; cursor: pointer; user-select: none; }
  .row:hover { background: rgba(74,158,255,0.08); }
  .row.selected { background: var(--rdl-selected); border-left: 3px solid var(--rdl-accent);
    padding-left: 13px; }
  .row .caret { color: var(--rdl-dim); font-size: 11px; text-align: right; }
  .row .caret-toggle { cursor: pointer; padding: 0 4px; border-radius: 2px;
    transition: background 0.08s; }
  .row .caret-toggle:hover { background: rgba(74,158,255,0.18); color: var(--rdl-fg); }
  .row .addr { color: var(--rdl-dim); }
  .row .name { font-weight: 600; }
  .row .access { color: var(--rdl-dim); font-size: 12px; text-align: right;
    font-family: var(--rdl-font-chrome); }
  .row.container .name { color: var(--rdl-accent); }
  .row.container .access { font-style: italic; }
  .indent-1 { padding-left: 32px; }
  .indent-2 { padding-left: 56px; }
  .indent-3 { padding-left: 80px; }
  .indent-1.selected { padding-left: 29px; }
  .indent-2.selected { padding-left: 53px; }
  .indent-3.selected { padding-left: 77px; }
  .pill { display: inline-block; padding: 0 6px; border-radius: 2px; color: #1a1a1a;
    font-size: 11px; line-height: 17px; font-family: var(--rdl-font-chrome); font-weight: 500;
    text-align: center; }
  .pill.rw  { background: var(--rdl-acc-rw); }
  .pill.ro  { background: var(--rdl-acc-ro); }
  .pill.w1c { background: var(--rdl-acc-w1c); }
  .pill.w0c { background: var(--rdl-acc-w1c); opacity: 0.8; }
  .pill.w1s { background: var(--rdl-acc-rw); opacity: 0.8; }
  .pill.w0s { background: var(--rdl-acc-rw); opacity: 0.6; }
  .pill.wo  { background: var(--rdl-acc-wo); }
  .pill.wclr,.pill.wset { background: var(--rdl-acc-w1c); opacity: 0.7; }
  .pill.rclr,.pill.rset { background: var(--rdl-acc-ro); opacity: 0.7; }
  .pill.rsv,.pill.na { background: var(--rdl-acc-rsv); color: var(--rdl-fg); opacity: 0.8; }
  #detail { padding: 16px 20px; overflow: auto; min-height: 0; }
  #detail h2 { margin: 0 0 2px; font-size: 17px; font-weight: 600;
    font-family: var(--rdl-font-mono); }
  #detail .display-name { color: var(--rdl-fg); font-size: 13px; margin-bottom: 4px; }
  #detail .breadcrumb { color: var(--rdl-dim); font-size: 12px;
    font-family: var(--rdl-font-mono); margin-bottom: 12px; }
  #detail .meta { display: grid; grid-template-columns: auto 1fr auto 1fr;
    column-gap: 12px; row-gap: 4px; max-width: 520px; font-size: 13px; margin-bottom: 16px; }
  #detail .meta .k { color: var(--rdl-dim); }
  #detail .meta .v { color: var(--rdl-fg); font-family: var(--rdl-font-mono); }
  #detail .desc { color: var(--rdl-dim); font-size: 13px; line-height: 1.5;
    margin-bottom: 16px; max-width: 60ch; }
  #detail .fields-title { font-size: 11px; color: var(--rdl-dim);
    text-transform: uppercase; letter-spacing: 0.06em;
    border-bottom: 1px solid var(--rdl-border); padding-bottom: 6px;
    margin: 16px 0 8px; }
  #detail .field { display: grid; grid-template-columns: 60px 140px 60px 90px 1fr;
    column-gap: 12px; padding: 4px 0; border-bottom: 1px dotted #2a2a2a;
    font-family: var(--rdl-font-mono); font-size: 13px; align-items: baseline; }
  #detail .field .desc { color: var(--rdl-dim); font-family: var(--rdl-font-chrome);
    font-style: normal; }
  #detail .src-link { display: inline-block; margin-top: 16px;
    color: var(--rdl-accent); font-family: var(--rdl-font-mono); font-size: 13px; }
  #detail .placeholder { color: var(--rdl-dim); font-size: 13px; padding: 24px 0; }
  .empty { padding: 32px 40px; max-width: 60ch; }
  .empty h2 { font-size: 15px; font-weight: 600; margin: 0 0 8px; }
  .empty p { margin: 4px 0; color: var(--rdl-dim); font-size: 13px; }
  .empty code { font-family: var(--rdl-font-mono); background: var(--rdl-panel);
    padding: 1px 5px; border-radius: 2px; }
</style></head>
<body>
  <div class="topbar">
    <span class="title">${escapeAttr(filename)}</span>
    <span id="conn" class="conn">connecting…</span>
  </div>
  <div id="stale-bar" class="stale-bar">
    <span>⚠</span><span id="stale-text">Showing last good elaboration</span>
  </div>
  <div id="tabs" class="tabs"></div>
  <div class="body">
    <div class="tree-pane">
      <div id="filter-bar" class="filter-bar">
        <input id="filter-input" type="text" placeholder="Filter by name, address (0x10), field, or access (rw)…" />
        <div id="filter-hint" class="filter-hint"></div>
      </div>
      <div id="tree-host" class="tree-host">
        <div class="empty">
          <h2>Memory map viewer</h2>
          <p>Waiting for first compile of <code>${escapeAttr(filename)}</code>…</p>
        </div>
      </div>
    </div>
    <div id="detail">
      <div class="placeholder">Select a register to see details.</div>
    </div>
  </div>
<script>
${RENDER_JS}
</script>
</body></html>`;
}

function escapeAttr(s: string): string {
  return s.replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  } as Record<string, string>)[c]);
}

