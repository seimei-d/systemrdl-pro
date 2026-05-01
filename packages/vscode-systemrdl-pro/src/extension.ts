import * as cp from 'node:child_process';
import * as vscode from 'vscode';
import {
  CloseAction,
  ErrorAction,
  LanguageClient,
  type LanguageClientOptions,
  type ServerOptions,
} from 'vscode-languageclient/node';

// Mirrors schemas/elaborated-tree.json v0.1.0. Keep in sync — Decision 9A: codegen
// will replace this hand-written shadow type in Week 5.
type ElaboratedTree = {
  schemaVersion: '0.1.0';
  // Per-URI monotonic counter. TODO-1: clients pass `sinceVersion` to skip
  // serialization+transport when the cached tree hasn't advanced. Absent in
  // the legacy reply shape so we keep it optional.
  version?: number;
  // True when the LSP recognized the client's `sinceVersion`. The body is
  // empty in this case; the client must keep its previously-rendered tree.
  unchanged?: boolean;
  elaboratedAt?: string;
  stale?: boolean;
  roots: Addrmap[];
};

type Addrmap = {
  kind: 'addrmap';
  name: string;
  type?: string;
  displayName?: string;
  address: string;
  size: string;
  desc?: string;
  source?: SourceLoc;
  children: (Addrmap | Regfile | Reg)[];
};

type Regfile = {
  kind: 'regfile';
  name: string;
  type?: string;
  displayName?: string;
  address: string;
  size: string;
  desc?: string;
  source?: SourceLoc;
  children: (Regfile | Reg)[];
};

type Reg = {
  kind: 'reg';
  name: string;
  type?: string;
  displayName?: string;
  address: string;
  width: 8 | 16 | 32 | 64;
  reset?: string;
  accessSummary?: string;
  desc?: string;
  source?: SourceLoc;
  fields: Field[];
};

type Field = {
  name: string;
  displayName?: string;
  lsb: number;
  msb: number;
  access: string;
  reset?: string;
  desc?: string;
  source?: SourceLoc;
};

type SourceLoc = {
  uri: string;
  line: number;
  column?: number;
  endLine?: number;
  endColumn?: number;
};

let client: LanguageClient | undefined;
let outputChannel: vscode.LogOutputChannel | undefined;

// One Memory Map panel per .rdl file (markdown-preview-style). Key is the
// document URI string; value carries the panel + its latest tree snapshot
// for the status-bar refresher.
type PanelEntry = {
  panel: vscode.WebviewPanel;
  uri: string;
  lastTree?: ElaboratedTree;
};
const memoryMapPanels = new Map<string, PanelEntry>();

let statusBarItem: vscode.StatusBarItem | undefined;
let cursorSyncTimer: ReturnType<typeof setTimeout> | undefined;

// Eng-review safety net #3: LSP supervisor.
// We auto-restart up to MAX_RESTARTS times within RESTART_WINDOW_MS. A burst of
// crashes (broken Python install, bad pygls upgrade, etc.) hits the cap and we
// surface a banner instead of looping forever. Successful uptime past the window
// resets the counter so a single crash later doesn't poison the next session.
const MAX_RESTARTS = 3;
const RESTART_WINDOW_MS = 60_000;
let recentCrashTimes: number[] = [];
// Suppress one cycle of cursor → viewer sync after a viewer-initiated reveal.
// Otherwise: click reg → editor jumps → onDidChangeTextEditorSelection fires →
// posts cursor → viewer re-selects (same key) → renders → no harm but wasteful.
// More importantly, if the user spam-clicks regs we'd be racing render cycles.
let suppressNextCursorSync = false;
const CURSOR_SYNC_DEBOUNCE_MS = 500;

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  outputChannel = vscode.window.createOutputChannel('SystemRDL Pro', { log: true });
  context.subscriptions.push(outputChannel);

  statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusBarItem.command = 'systemrdl-pro.showMemoryMap';
  statusBarItem.tooltip = 'Click to open SystemRDL Memory Map';
  context.subscriptions.push(statusBarItem);

  context.subscriptions.push(
    vscode.commands.registerCommand('systemrdl-pro.showMemoryMap', () =>
      showMemoryMap(context),
    ),
    vscode.commands.registerCommand('systemrdl-pro.restartServer', () =>
      restartServer(context),
    ),
    // Save → refresh the panel for that exact URI (if one is open).
    vscode.workspace.onDidSaveTextDocument(doc => {
      if (doc.languageId !== 'systemrdl-pro') return;
      const uri = doc.uri.toString();
      if (memoryMapPanels.has(uri)) {
        refreshMemoryMap(uri).catch(err =>
          outputChannel?.warn(`refresh after save failed: ${err}`),
        );
      }
    }),
    // D10: cursor → viewer. Forwards only to the panel watching the exact
    // URI the editor cursor is in.
    vscode.window.onDidChangeTextEditorSelection(event => {
      if (event.textEditor.document.languageId !== 'systemrdl-pro') return;
      const uri = event.textEditor.document.uri.toString();
      const entry = memoryMapPanels.get(uri);
      if (!entry) return;
      if (suppressNextCursorSync) {
        suppressNextCursorSync = false;
        return;
      }
      const line = event.textEditor.selection.active.line;
      if (cursorSyncTimer) clearTimeout(cursorSyncTimer);
      cursorSyncTimer = setTimeout(() => {
        safePostTo(entry, { type: 'cursor', line });
      }, CURSOR_SYNC_DEBOUNCE_MS);
    }),
    // Refresh status bar diag count when diagnostics change for the URI the
    // active editor is on.
    vscode.languages.onDidChangeDiagnostics(event => {
      const active = vscode.window.activeTextEditor;
      if (!active) return;
      const activeUri = active.document.uri.toString();
      if (event.uris.some(u => u.toString() === activeUri)) {
        const entry = memoryMapPanels.get(activeUri);
        if (entry?.lastTree) updateStatusBar(entry.lastTree, activeUri);
      }
    }),
    // Tab focus → swap status bar to track that file's panel.
    vscode.window.onDidChangeActiveTextEditor(editor => {
      if (!editor || editor.document.languageId !== 'systemrdl-pro') return;
      const uri = editor.document.uri.toString();
      const entry = memoryMapPanels.get(uri);
      if (entry?.lastTree) updateStatusBar(entry.lastTree, uri);
      else if (statusBarItem) statusBarItem.hide();
    }),
  );

  await startServer(context);
}

export async function deactivate(): Promise<void> {
  if (client) {
    await client.stop();
    client = undefined;
  }
  for (const entry of memoryMapPanels.values()) entry.panel.dispose();
  memoryMapPanels.clear();
}

// ---------------------------------------------------------------------------
// LSP server lifecycle
// ---------------------------------------------------------------------------

async function startServer(context: vscode.ExtensionContext): Promise<void> {
  const python = await resolvePython();
  if (!python) return;

  const moduleAvailable = await checkLspModule(python);
  if (!moduleAvailable) {
    showInstallBanner(python);
    return;
  }

  const serverOptions: ServerOptions = {
    command: python,
    args: ['-m', 'systemrdl_lsp', '--log-level', 'WARNING'],
    options: { env: { ...process.env } },
  };

  const clientOptions: LanguageClientOptions = {
    documentSelector: [{ scheme: 'file', language: 'systemrdl-pro' }],
    outputChannel,
    synchronize: {
      configurationSection: 'systemrdl-pro',
      fileEvents: vscode.workspace.createFileSystemWatcher('**/*.rdl'),
    },
    errorHandler: {
      error: (err, msg, count) => {
        outputChannel?.error(`LSP error (${count ?? 0}): ${err.message ?? err}`, msg);
        return { action: count && count <= 3 ? ErrorAction.Continue : ErrorAction.Shutdown };
      },
      closed: () => {
        const now = Date.now();
        recentCrashTimes = recentCrashTimes.filter(t => now - t < RESTART_WINDOW_MS);
        recentCrashTimes.push(now);
        if (recentCrashTimes.length <= MAX_RESTARTS) {
          outputChannel?.warn(
            `LSP server stopped (${recentCrashTimes.length}/${MAX_RESTARTS}); restarting…`,
          );
          return { action: CloseAction.Restart };
        }
        outputChannel?.error(
          `LSP server stopped ${recentCrashTimes.length} times in ${RESTART_WINDOW_MS / 1000}s; ` +
          'giving up auto-restart. Use "SystemRDL: Restart Language Server" once the cause is fixed.',
        );
        recentCrashTimes = [];
        showRestartBanner();
        return { action: CloseAction.DoNotRestart };
      },
    },
  };

  client = new LanguageClient(
    'systemrdl-pro',
    'SystemRDL Pro',
    serverOptions,
    clientOptions,
  );

  context.subscriptions.push({ dispose: () => client?.stop() });

  try {
    await client.start();
    outputChannel?.info(`LSP started via ${python}`);

    // TODO-1: server-pushed "tree changed" notifications eliminate the wait
    // for didSaveTextDocument. The payload is metadata-only (uri + version);
    // refreshMemoryMap then asks for the body only when our cached version
    // is older. Skip the refresh when no panel is open for that URI — no
    // point re-fetching a tree nobody is rendering.
    client.onNotification('rdl/elaboratedTreeChanged', (params: { uri: string; version: number }) => {
      if (!params || typeof params.uri !== 'string') return;
      const entry = memoryMapPanels.get(params.uri);
      if (!entry) return;
      // If we already have this exact version, the request would round-trip
      // for no benefit; skip even the request.
      if (entry.lastTree?.version === params.version) return;
      refreshMemoryMap(params.uri).catch(err =>
        outputChannel?.warn(`refresh on push failed: ${err}`),
      );
    });
  } catch (err) {
    outputChannel?.error(`Failed to start LSP: ${err}`);
    showRestartBanner();
  }
}

async function restartServer(context: vscode.ExtensionContext): Promise<void> {
  // User-initiated restart resets the supervisor's crash budget — the user
  // presumably fixed whatever was causing the crashes.
  recentCrashTimes = [];
  if (client) {
    await client.stop();
    client = undefined;
  }
  await startServer(context);
}

// ---------------------------------------------------------------------------
// Python resolution (decision 2B)
// ---------------------------------------------------------------------------

async function resolvePython(): Promise<string | undefined> {
  const setting = vscode.workspace
    .getConfiguration('systemrdl-pro')
    .get<string>('pythonPath', '')
    .trim();
  if (setting) {
    if (await isExecutable(setting)) return setting;
    showPythonNotFoundBanner(`Configured systemrdl-pro.pythonPath does not exist: ${setting}`);
    return undefined;
  }

  const fromMsPython = await getMsPythonInterpreter();
  if (fromMsPython) return fromMsPython;

  for (const candidate of ['python3', 'python']) {
    if (await isExecutable(candidate)) return candidate;
  }

  showPythonNotFoundBanner('Python 3.10+ not found on PATH and no interpreter configured.');
  return undefined;
}

async function getMsPythonInterpreter(): Promise<string | undefined> {
  const ext = vscode.extensions.getExtension('ms-python.python');
  if (!ext) return undefined;
  if (!ext.isActive) {
    try { await ext.activate(); } catch { return undefined; }
  }
  try {
    const api = ext.exports as
      | { environments?: { getActiveEnvironmentPath?: () => { path: string } | undefined } }
      | undefined;
    return api?.environments?.getActiveEnvironmentPath?.()?.path;
  } catch {
    return undefined;
  }
}

async function isExecutable(cmd: string): Promise<boolean> {
  return new Promise(resolve => {
    cp.execFile(cmd, ['--version'], { timeout: 3_000 }, err => resolve(!err));
  });
}

async function checkLspModule(python: string): Promise<boolean> {
  return new Promise(resolve => {
    cp.execFile(
      python,
      ['-c', 'import systemrdl_lsp; print(systemrdl_lsp.__version__)'],
      { timeout: 5_000 },
      err => resolve(!err),
    );
  });
}

// ---------------------------------------------------------------------------
// Banners
// ---------------------------------------------------------------------------

function showPythonNotFoundBanner(detail: string): void {
  vscode.window
    .showErrorMessage(`SystemRDL Pro: ${detail}`, 'Set pythonPath…', 'Open Settings')
    .then(choice => {
      if (choice === 'Set pythonPath…' || choice === 'Open Settings') {
        vscode.commands.executeCommand(
          'workbench.action.openSettings',
          'systemrdl-pro.pythonPath',
        );
      }
    });
}

function showInstallBanner(python: string): void {
  vscode.window
    .showErrorMessage(
      `SystemRDL Pro: 'systemrdl-lsp' is not installed in ${python}.`,
      'Install with pip…',
      'Choose Python…',
    )
    .then(choice => {
      if (choice === 'Install with pip…') {
        const term = vscode.window.createTerminal('SystemRDL Pro: install LSP');
        term.show();
        term.sendText(`${python} -m pip install --upgrade systemrdl-lsp`);
      } else if (choice === 'Choose Python…') {
        vscode.commands.executeCommand(
          'workbench.action.openSettings',
          'systemrdl-pro.pythonPath',
        );
      }
    });
}

function showRestartBanner(): void {
  vscode.window
    .showErrorMessage('SystemRDL Pro: language server stopped.', 'Restart LSP')
    .then(choice => {
      if (choice === 'Restart LSP') {
        vscode.commands.executeCommand('systemrdl-pro.restartServer');
      }
    });
}

// ---------------------------------------------------------------------------
// Memory Map webview (Week 4 walking skeleton)
// ---------------------------------------------------------------------------

async function showMemoryMap(context: vscode.ExtensionContext): Promise<void> {
  const targetUri = pickTargetUri();
  if (!targetUri) {
    vscode.window.showInformationMessage(
      'SystemRDL Pro: open a .rdl file before running Show Memory Map.',
    );
    return;
  }
  const uri = targetUri.toString();

  // If a panel for this URI already exists, just bring it forward —
  // markdown-preview-style. Otherwise create a fresh one.
  const existing = memoryMapPanels.get(uri);
  if (existing) {
    existing.panel.reveal(vscode.ViewColumn.Beside);
    await refreshMemoryMap(uri);
    return;
  }

  const viewerDist = vscode.Uri.joinPath(context.extensionUri, 'media', 'viewer');
  const fileName = targetUri.path.split('/').pop() || 'Memory Map';
  const panel = vscode.window.createWebviewPanel(
    'systemrdl-pro.memoryMap',
    `Memory Map · ${fileName}`,
    vscode.ViewColumn.Beside,
    {
      enableScripts: true,
      retainContextWhenHidden: true,
      localResourceRoots: [viewerDist],
    },
  );
  panel.iconPath = vscode.Uri.joinPath(context.extensionUri, 'media', 'icon.png');

  const entry: PanelEntry = { panel, uri };
  memoryMapPanels.set(uri, entry);

  panel.onDidDispose(
    () => { memoryMapPanels.delete(uri); },
    null,
    context.subscriptions,
  );
  panel.onDidChangeViewState(
    e => {
      if (e.webviewPanel.visible) {
        refreshMemoryMap(uri).catch(err =>
          outputChannel?.warn(`refresh on viewState change failed: ${err}`),
        );
      }
    },
    null,
    context.subscriptions,
  );
  panel.webview.onDidReceiveMessage(
    (msg: WebviewMessage) => handleWebviewMessage(msg, uri),
    undefined,
    context.subscriptions,
  );
  panel.webview.html = renderViewerHtml(panel.webview, context.extensionUri);

  await refreshMemoryMap(uri);
}

/** Post to a specific panel; no-ops gracefully if the panel was disposed. */
function safePostTo(entry: PanelEntry, message: unknown): void {
  try {
    entry.panel.webview.postMessage(message);
  } catch (err) {
    outputChannel?.warn(`webview.postMessage failed: ${err}`);
  }
}

// ---------------------------------------------------------------------------
// Webview → extension messaging (W6: bidirectional source map)
// ---------------------------------------------------------------------------

type WebviewMessage =
  | { type: 'reveal'; source: SourceLoc }
  | { type: 'copy'; text: string; label?: string };

async function handleWebviewMessage(msg: WebviewMessage, _panelUri: string): Promise<void> {
  if (msg.type === 'reveal') {
    await revealLocation(msg.source);
  } else if (msg.type === 'copy') {
    await vscode.env.clipboard.writeText(msg.text);
    const label = msg.label || 'value';
    vscode.window.setStatusBarMessage(`Copied ${label}: ${msg.text}`, 2_000);
  }
}

const flashDecoration = vscode.window.createTextEditorDecorationType({
  backgroundColor: new vscode.ThemeColor('editor.findMatchHighlightBackground'),
  isWholeLine: true,
});

async function revealLocation(loc: SourceLoc): Promise<void> {
  let uri: vscode.Uri;
  try {
    uri = vscode.Uri.parse(loc.uri);
  } catch {
    return;
  }
  const line = Math.max(0, loc.line ?? 0);
  const char = Math.max(0, loc.column ?? 0);
  const endLine = Math.max(line, loc.endLine ?? line);
  const endChar = Math.max(char, loc.endColumn ?? char + 1);
  const range = new vscode.Range(line, char, endLine, endChar);

  try {
    const doc = await vscode.workspace.openTextDocument(uri);
    // The cursor change we're about to cause must not bounce back as a cursor-sync
    // message into the viewer; suppress one cycle.
    suppressNextCursorSync = true;
    // Place the cursor at the START of the symbol — feedback: jumping to the END
    // (range.end) lands the cursor right after `DMA_BASE_ADDR`, not at the name's
    // first character. revealRange + flash still uses the broader range so the
    // visual context is intact.
    const cursorAtStart = new vscode.Range(range.start, range.start);
    // preserveFocus: true keeps keyboard focus in the webview so arrow-key
    // navigation through the tree keeps working after a reveal. Editor scrolls
    // and gets a flash highlight; user can still click into it to type.
    const editor = await vscode.window.showTextDocument(doc, {
      viewColumn: vscode.ViewColumn.One,
      preserveFocus: true,
      selection: cursorAtStart,
    });
    editor.revealRange(range, vscode.TextEditorRevealType.InCenterIfOutsideViewport);
    editor.setDecorations(flashDecoration, [range]);
    setTimeout(() => editor.setDecorations(flashDecoration, []), 200);
  } catch (err) {
    outputChannel?.warn(`reveal ${loc.uri}:${line + 1} failed: ${err}`);
    suppressNextCursorSync = false;
  }
}

async function refreshMemoryMap(uri: string): Promise<void> {
  const entry = memoryMapPanels.get(uri);
  if (!entry || !client) return;

  try {
    // TODO-1: send the version we last rendered so the LSP can skip
    // serialization + transport when nothing changed (e.g. focus changes,
    // panel re-mount, multiple notifications during a debounce window).
    const sinceVersion = entry.lastTree?.version;
    const reply = await client.sendRequest<ElaboratedTree>('rdl/elaboratedTree', {
      uri,
      ...(sinceVersion !== undefined ? { sinceVersion } : {}),
    });
    if (reply.unchanged) {
      // Cached version still current — keep the existing tree, but refresh
      // status-bar diagnostics counters since those track LSP-published diags
      // independently of the tree.
      const active = vscode.window.activeTextEditor?.document.uri.toString();
      if (active === uri && entry.lastTree) updateStatusBar(entry.lastTree, uri);
      return;
    }
    safePostTo(entry, { type: 'tree', tree: reply });
    entry.lastTree = reply;
    const active = vscode.window.activeTextEditor?.document.uri.toString();
    if (active === uri) updateStatusBar(reply, uri);
  } catch (err) {
    outputChannel?.error(`rdl/elaboratedTree failed: ${err}`);
    safePostTo(entry, {
      type: 'error',
      message: `Could not fetch elaborated tree: ${err}`,
    });
  }
}

function updateStatusBar(tree: ElaboratedTree, uri: string): void {
  if (!statusBarItem) return;
  if (!tree.roots.length) {
    statusBarItem.hide();
    return;
  }
  const total = countRegs(tree.roots);
  const rootNames = tree.roots.map(r => r.name).join(', ');
  const stale = tree.stale ? ' $(warning) stale' : '';

  let diag = '';
  const all = vscode.languages.getDiagnostics(vscode.Uri.parse(uri));
  let errors = 0, warnings = 0;
  for (const d of all) {
    if (d.severity === vscode.DiagnosticSeverity.Error) errors++;
    else if (d.severity === vscode.DiagnosticSeverity.Warning) warnings++;
  }
  if (errors) diag += ` $(error) ${errors}`;
  if (warnings) diag += ` $(warning) ${warnings}`;

  statusBarItem.text = `$(circuit-board) ${total} reg${total === 1 ? '' : 's'} · ${rootNames}${stale}${diag}`;
  statusBarItem.tooltip = stale
    ? 'Memory map showing last good elaboration · current parse failed. Click to open viewer.'
    : `Memory map · ${rootNames} · click to open`;
  statusBarItem.show();
}

function countRegs(roots: Addrmap[]): number {
  let n = 0;
  const walk = (node: Addrmap | Regfile | Reg): void => {
    if (node.kind === 'reg') n++;
    else (node.children ?? []).forEach(walk);
  };
  roots.forEach(walk);
  return n;
}

function pickTargetUri(): vscode.Uri | undefined {
  const active = vscode.window.activeTextEditor;
  if (active && active.document.languageId === 'systemrdl-pro') {
    return active.document.uri;
  }
  // Fallback: first .rdl in any visible editor.
  for (const editor of vscode.window.visibleTextEditors) {
    if (editor.document.languageId === 'systemrdl-pro') return editor.document.uri;
  }
  return undefined;
}

// ---------------------------------------------------------------------------
// Webview shell — loads the @systemrdl-pro/viewer-core React bundle and wires
// up a postMessage transport. The renderer (Tree, Detail, ContextMenu, etc.)
// lives in viewer.js shared with the rdl-viewer CLI; this shell only declares
// the host element, transport bridge, and CSP.
// ---------------------------------------------------------------------------

function renderViewerHtml(webview: vscode.Webview, extensionUri: vscode.Uri): string {
  const viewerJs = webview.asWebviewUri(
    vscode.Uri.joinPath(extensionUri, 'media', 'viewer', 'viewer.js'),
  );
  const viewerCss = webview.asWebviewUri(
    vscode.Uri.joinPath(extensionUri, 'media', 'viewer', 'viewer.css'),
  );
  // CSP: only allow scripts/styles from the webview source. The init script is
  // inline; we use a nonce so VSCode's webview CSP enforcement doesn't block it.
  const nonce = makeNonce();
  return /* html */ `<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src ${webview.cspSource} 'unsafe-inline'; script-src ${webview.cspSource} 'nonce-${nonce}'; font-src ${webview.cspSource};">
<title>SystemRDL Memory Map</title>
<link rel="stylesheet" href="${viewerCss}">
<style>html,body,#app-root{height:100%;margin:0;}</style>
</head>
<body>
  <div id="app-root"></div>
  <script nonce="${nonce}" src="${viewerJs}"></script>
  <script nonce="${nonce}">
  (function() {
    const vscode = acquireVsCodeApi();
    const updaters = new Set();
    const cursorListeners = new Set();
    let pendingTree = null;

    window.addEventListener('message', (e) => {
      const m = e.data;
      if (m && m.type === 'tree') {
        pendingTree = m.tree;
        updaters.forEach(cb => cb(m.tree));
      } else if (m && m.type === 'cursor') {
        cursorListeners.forEach(cb => cb(m.line));
      }
    });

    const transport = {
      // The host pushes the initial tree via the same 'tree' postMessage.
      // We resolve getTree() on the first one — if the host has already
      // sent it (cached pendingTree) we return immediately.
      getTree() {
        if (pendingTree) return Promise.resolve(pendingTree);
        return new Promise(resolve => {
          const off = (tree) => { updaters.delete(off); resolve(tree); };
          updaters.add(off);
        });
      },
      onTreeUpdate(cb) { updaters.add(cb); return () => updaters.delete(cb); },
      onCursorMove(cb) { cursorListeners.add(cb); return () => cursorListeners.delete(cb); },
      reveal(source) { vscode.postMessage({ type: 'reveal', source }); },
      copy(text, label) { vscode.postMessage({ type: 'copy', text, label }); },
    };

    RdlViewer.mount(document.getElementById('app-root'), transport);
  })();
  </script>
</body></html>`;
}

function makeNonce(): string {
  const chars = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
  let s = '';
  for (let i = 0; i < 32; i++) s += chars[Math.floor(Math.random() * chars.length)];
  return s;
}

