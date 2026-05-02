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
import { existsSync, readFileSync, watch } from 'node:fs';
import path from 'node:path';
import process from 'node:process';

// Resolve the @systemrdl-pro/viewer-core build output.
//
// Two layouts must work:
//
//   1. Dev — `bun run start` from `packages/rdl-viewer-cli/src/index.ts`.
//      `import.meta.dir` is `…/packages/rdl-viewer-cli/src`. The viewer
//      assets live two directories up at
//      `…/packages/rdl-viewer-core/dist`.
//
//   2. Built binary — `dist/rdl-viewer.js` produced by `bun build`. The
//      `copy-assets` build step puts the viewer assets at
//      `dist/viewer/` next to the binary, so `import.meta.dir` is
//      `…/packages/rdl-viewer-cli/dist` and we look in
//      `…/packages/rdl-viewer-cli/dist/viewer`.
//
// We try the bundled-next-to-binary path first, fall back to the dev
// path. Pre-T4-A C5 only the dev path was attempted, which silently
// served HTTP 500 on every `/viewer.js` request from a published
// binary because the relative path didn't resolve to a real directory.
const VIEWER_CORE_DIST = (() => {
  const bundled = path.resolve(import.meta.dir, 'viewer');
  if (existsSync(bundled)) return bundled;
  const dev = path.resolve(import.meta.dir, '../../rdl-viewer-core/dist');
  return dev;
})();

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
      // T4-B H11: bounds + numeric check. Pre-T4-B was bare
      // `Number(argv[++i])` which silently produced NaN if the user
      // forgot the value (`rdl-viewer file --port`) — Bun.serve then
      // assigned a random port, leaving the caller confused. Now
      // exit with an actionable error.
      const val = argv[++i];
      if (val === undefined || !/^\d+$/.test(val)) {
        console.error(`rdl-viewer: --port requires a numeric argument (got ${val === undefined ? 'nothing' : JSON.stringify(val)})`);
        process.exit(2);
      }
      args.port = Number(val);
    } else if (a === '--no-open') {
      args.open = false;
    } else if (a === '--python') {
      // T4-B H11: same — pre-T4-B passed `undefined` into spawnSync,
      // throwing an uncaught synchronous TypeError.
      const val = argv[++i];
      if (val === undefined) {
        console.error('rdl-viewer: --python requires a path argument');
        process.exit(2);
      }
      args.python = val;
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

// T4-B H10: handle SIGTERM in addition to SIGINT. Docker, systemd,
// `kill <pid>` (without args) — all default to SIGTERM, not SIGINT.
// Pre-T4-B SIGTERM took the default behaviour (exit immediately
// with no cleanup), leaking the watcher's inotify handle and
// orphaning any in-flight Python dump child as a zombie until the
// kernel reaped it. Mirror the SIGINT handler.
const shutdown = (sig: string) => {
  console.error(`rdl-viewer: received ${sig}, shutting down`);
  try { watcher.close(); } catch { /* ignore */ }
  process.exit(0);
};
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));

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
    if (url.pathname === '/viewer.js') {
      return staticAsset('viewer.js', 'application/javascript; charset=utf-8');
    }
    if (url.pathname === '/viewer.css') {
      return staticAsset('viewer.css', 'text/css; charset=utf-8');
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

/**
 * Serve a static asset out of `@systemrdl-pro/viewer-core/dist/`. The package
 * is workspace-internal, so we read from the filesystem directly instead of
 * bundling — keeps the CLI binary lightweight and lets the user re-run
 * `bun --filter @systemrdl-pro/viewer-core build` to refresh the SPA without
 * rebuilding the CLI.
 */
function staticAsset(name: string, contentType: string): Response {
  const full = path.join(VIEWER_CORE_DIST, name);
  if (!existsSync(full)) {
    return new Response(
      `viewer-core asset missing: ${name}. Run \`bun --filter @systemrdl-pro/viewer-core build\`.`,
      { status: 500, headers: { 'content-type': 'text/plain; charset=utf-8' } },
    );
  }
  const body = readFileSync(full);
  return new Response(body, {
    headers: { 'content-type': contentType, 'cache-control': 'no-cache' },
  });
}

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
// Browser SPA shell — loads the React bundle from @systemrdl-pro/viewer-core
// and wires up a fetch+SSE transport. The renderer lives in viewer.js; this
// only declares the host element, transport, and CLI-specific chrome (the
// connection-status top bar) that doesn't belong in the shared package.
// ---------------------------------------------------------------------------

function renderHtml(filename: string): string {
  return /* html */ `<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SystemRDL — ${escapeAttr(filename)}</title>
<link rel="stylesheet" href="/viewer.css">
<style>
  body { margin: 0; }
  .topbar { background: var(--rdl-chrome); padding: 8px 14px;
    border-bottom: 1px solid var(--rdl-border);
    display: flex; align-items: center; gap: 12px; font-size: 13px; }
  .topbar .title { font-family: var(--rdl-font-mono); color: var(--rdl-fg); }
  .topbar .conn { color: var(--rdl-dim); margin-left: auto; font-size: 12px; }
  .topbar .conn.ok::before { content: '●  '; color: var(--rdl-acc-rw); }
  .topbar .conn.err::before { content: '●  '; color: #d75a5a; }
  #app-shell { display: grid; grid-template-rows: auto 1fr; height: 100vh; }
  #app-root { min-height: 0; }
</style>
</head>
<body>
  <div id="app-shell">
    <div class="topbar">
      <span class="title">${escapeAttr(filename)}</span>
      <span id="conn" class="conn">connecting…</span>
    </div>
    <div id="app-root"></div>
  </div>
  <script src="/viewer.js"></script>
  <script>
  (function() {
    const conn = document.getElementById('conn');
    const setConn = (cls, txt) => { conn.className = 'conn ' + cls; conn.textContent = txt; };

    const updaters = new Set();
    function startSse() {
      const es = new EventSource('/events');
      es.addEventListener('ready', () => setConn('ok', 'live'));
      es.onmessage = ev => {
        try {
          const tree = JSON.parse(ev.data);
          updaters.forEach(cb => cb(tree));
        } catch (e) { console.error('bad SSE frame', e); }
      };
      es.onerror = () => setConn('err', 'disconnected — retrying');
    }

    const transport = {
      getTree: () => fetch('/tree').then(r => r.json()),
      onTreeUpdate(cb) { updaters.add(cb); return () => updaters.delete(cb); },
      // No reveal: there is no editor here. Copy falls back to
      // navigator.clipboard inside the viewer when transport.copy is absent.
    };

    RdlViewer.mount(document.getElementById('app-root'), transport);
    startSse();
  })();
  </script>
</body></html>`;
}

function escapeAttr(s: string): string {
  return s.replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  } as Record<string, string>)[c]);
}
