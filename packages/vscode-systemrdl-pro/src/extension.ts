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
  elaboratedAt?: string;
  stale?: boolean;
  roots: Addrmap[];
};

type Addrmap = {
  kind: 'addrmap';
  name: string;
  type?: string;
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
let memoryMapPanel: vscode.WebviewPanel | undefined;
let lastTreeUri: string | undefined;

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  outputChannel = vscode.window.createOutputChannel('SystemRDL Pro', { log: true });
  context.subscriptions.push(outputChannel);

  context.subscriptions.push(
    vscode.commands.registerCommand('systemrdl-pro.showMemoryMap', () =>
      showMemoryMap(context),
    ),
    vscode.commands.registerCommand('systemrdl-pro.restartServer', () =>
      restartServer(context),
    ),
    // When the user saves any .rdl, refresh whichever URI the viewer is currently showing.
    vscode.workspace.onDidSaveTextDocument(doc => {
      if (doc.languageId !== 'systemrdl-pro') return;
      // If the user saves the file the panel is showing, refresh.
      if (lastTreeUri && doc.uri.toString() === lastTreeUri) {
        refreshMemoryMap().catch(err =>
          outputChannel?.warn(`refresh after save failed: ${err}`),
        );
      }
    }),
  );

  await startServer(context);
}

export async function deactivate(): Promise<void> {
  if (client) {
    await client.stop();
    client = undefined;
  }
  memoryMapPanel?.dispose();
  memoryMapPanel = undefined;
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
        outputChannel?.warn('LSP server stopped.');
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
  } catch (err) {
    outputChannel?.error(`Failed to start LSP: ${err}`);
    showRestartBanner();
  }
}

async function restartServer(context: vscode.ExtensionContext): Promise<void> {
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
  lastTreeUri = targetUri.toString();

  if (!memoryMapPanel) {
    memoryMapPanel = vscode.window.createWebviewPanel(
      'systemrdl-pro.memoryMap',
      'SystemRDL Memory Map',
      vscode.ViewColumn.Beside,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        // The webview only loads inline content from us; no remote resources.
        localResourceRoots: [],
      },
    );
    memoryMapPanel.iconPath = vscode.Uri.joinPath(context.extensionUri, 'media', 'icon.png');
    memoryMapPanel.onDidDispose(
      () => {
        memoryMapPanel = undefined;
        lastTreeUri = undefined;
      },
      null,
      context.subscriptions,
    );
    memoryMapPanel.webview.onDidReceiveMessage(
      (msg: WebviewMessage) => handleWebviewMessage(msg),
      undefined,
      context.subscriptions,
    );
    memoryMapPanel.webview.html = renderViewerHtml();
  } else {
    memoryMapPanel.reveal(vscode.ViewColumn.Beside);
  }

  await refreshMemoryMap();
}

// ---------------------------------------------------------------------------
// Webview → extension messaging (W6: bidirectional source map)
// ---------------------------------------------------------------------------

type WebviewMessage =
  | { type: 'reveal'; source: SourceLoc };

async function handleWebviewMessage(msg: WebviewMessage): Promise<void> {
  if (msg.type === 'reveal') {
    await revealLocation(msg.source);
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
    const editor = await vscode.window.showTextDocument(doc, {
      viewColumn: vscode.ViewColumn.One,
      preserveFocus: false,
      selection: range,
    });
    editor.revealRange(range, vscode.TextEditorRevealType.InCenterIfOutsideViewport);
    // 200ms whole-line flash (U2: smooth scroll + flash)
    editor.setDecorations(flashDecoration, [range]);
    setTimeout(() => editor.setDecorations(flashDecoration, []), 200);
  } catch (err) {
    outputChannel?.warn(`reveal ${loc.uri}:${line + 1} failed: ${err}`);
  }
}

async function refreshMemoryMap(): Promise<void> {
  if (!memoryMapPanel || !lastTreeUri || !client) return;

  try {
    const tree = await client.sendRequest<ElaboratedTree>('rdl/elaboratedTree', {
      uri: lastTreeUri,
    });
    // Eng review silent-failure gap #2: don't post into a disposed webview.
    if (memoryMapPanel?.visible !== undefined) {
      memoryMapPanel.webview.postMessage({ type: 'tree', tree });
    }
  } catch (err) {
    outputChannel?.error(`rdl/elaboratedTree failed: ${err}`);
    if (memoryMapPanel?.webview) {
      memoryMapPanel.webview.postMessage({
        type: 'error',
        message: `Could not fetch elaborated tree: ${err}`,
      });
    }
  }
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

function renderViewerHtml(): string {
  // CSP — only inline styles + inline scripts. No remote resources.
  // Design tokens mirror docs/design.md "Viewer UX → Design Tokens" (D11/D12/D14).
  // Vanilla-DOM walking skeleton — Week 5 swaps it for rdl-viewer-core Svelte without
  // changing the postMessage protocol (`tree` in, `reveal` out).
  return /* html */ `<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline';">
<title>SystemRDL Memory Map</title>
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
  html, body { margin: 0; padding: 0; background: var(--rdl-bg); color: var(--rdl-fg);
    font-family: var(--rdl-font-chrome); font-size: 13px; height: 100vh; }
  body { display: grid; grid-template-rows: auto auto 1fr auto; min-height: 0; }
  .stale-bar { background: rgba(215,168,90,0.12); border-bottom: 1px solid var(--rdl-warning);
    color: var(--rdl-warning); padding: 6px 12px; font-size: 12px;
    display: none; align-items: center; gap: 8px; }
  .stale-bar.shown { display: flex; }
  .tabs { display: flex; border-bottom: 1px solid var(--rdl-border); background: var(--rdl-chrome);
    overflow-x: auto; }
  .tab { padding: 8px 16px; font-size: 12px; color: var(--rdl-dim); cursor: pointer;
    border-right: 1px solid var(--rdl-border); white-space: nowrap; user-select: none; }
  .tab:hover { color: var(--rdl-fg); }
  .tab.active { color: var(--rdl-fg); background: var(--rdl-bg);
    border-bottom: 2px solid var(--rdl-accent); margin-bottom: -1px; }
  .body { display: grid; grid-template-columns: 320px 1fr; min-height: 0; }
  /* D13: auto-stack at narrow viewport (right-dock side-by-code use case) */
  @media (max-width: 700px) {
    .body { grid-template-columns: 1fr; grid-template-rows: 1fr 1fr; }
    #detail { border-left: none; border-top: 1px solid var(--rdl-border); }
  }
  .tree-host { overflow: auto; padding: 8px 0; min-height: 0; }
  .tree { font-family: var(--rdl-font-mono); font-size: 12px; }
  .row { display: grid; grid-template-columns: 28px 130px 1fr 90px; gap: 12px; align-items: baseline;
    padding: 3px 16px; cursor: default; }
  .row.expandable { cursor: pointer; }
  .row.expandable:hover { background: rgba(74,158,255,0.08); }
  .row.selected { background: var(--rdl-selected); border-left: 3px solid var(--rdl-accent);
    padding-left: 13px; }
  .row .caret { color: var(--rdl-dim); font-size: 10px; user-select: none; text-align: right; }
  .row .addr { color: var(--rdl-dim); }
  .row .name { font-weight: 600; }
  .row .access { color: var(--rdl-dim); font-size: 11px; text-align: right;
    font-family: var(--rdl-font-chrome); }
  .indent-1 { padding-left: 32px; }
  .indent-2 { padding-left: 56px; }
  .indent-1.selected { padding-left: 29px; }
  .indent-2.selected { padding-left: 53px; }
  .field-row-inline { display: grid; grid-template-columns: 56px 130px 60px 60px 1fr; gap: 12px;
    padding: 2px 0 2px 64px; color: var(--rdl-dim); font-size: 11px;
    font-family: var(--rdl-font-mono); }
  .field-row-inline b { color: var(--rdl-fg); font-weight: 500; }
  .pill { display: inline-block; padding: 0 6px; border-radius: 2px; color: #1a1a1a;
    font-size: 10px; line-height: 16px; font-family: var(--rdl-font-chrome); font-weight: 500;
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

  /* Detail pane (W5) */
  #detail { border-left: 1px solid var(--rdl-border); padding: 16px 20px;
    overflow: auto; min-height: 0; }
  #detail h2 { margin: 0 0 4px; font-size: 16px; font-weight: 600;
    font-family: var(--rdl-font-mono); }
  #detail .breadcrumb { color: var(--rdl-dim); font-size: 11px;
    font-family: var(--rdl-font-mono); margin-bottom: 12px; }
  #detail .meta { display: grid; grid-template-columns: auto 1fr auto 1fr;
    column-gap: 12px; row-gap: 4px; max-width: 480px; font-size: 12px;
    margin-bottom: 16px; }
  #detail .meta .k { color: var(--rdl-dim); }
  #detail .meta .v { color: var(--rdl-fg); font-family: var(--rdl-font-mono); }
  #detail .desc { color: var(--rdl-dim); font-size: 12px; line-height: 1.5;
    margin-bottom: 16px; max-width: 60ch; }
  #detail .fields-title { font-size: 10px; color: var(--rdl-dim);
    text-transform: uppercase; letter-spacing: 0.06em;
    border-bottom: 1px solid var(--rdl-border); padding-bottom: 6px;
    margin: 16px 0 8px; }
  #detail .field { display: grid; grid-template-columns: 56px 130px 60px 80px 1fr;
    column-gap: 12px; padding: 4px 0; border-bottom: 1px dotted #2a2a2a;
    font-family: var(--rdl-font-mono); font-size: 12px; align-items: baseline; }
  #detail .field .desc { color: var(--rdl-dim); font-family: var(--rdl-font-chrome);
    font-style: normal; }
  #detail .src-link { display: inline-block; margin-top: 16px;
    color: var(--rdl-accent); cursor: pointer;
    font-family: var(--rdl-font-mono); font-size: 12px; }
  #detail .src-link:hover { text-decoration: underline; }
  #detail .placeholder { color: var(--rdl-dim); font-size: 12px; padding: 24px 0; }

  .empty { padding: 32px 40px; max-width: 60ch; }
  .empty h2 { font-size: 14px; font-weight: 600; margin: 0 0 8px; }
  .empty p { margin: 4px 0; color: var(--rdl-dim); font-size: 12px; }
  .empty code { font-family: var(--rdl-font-mono); background: var(--rdl-panel);
    padding: 1px 5px; border-radius: 2px; }
  .status { font-size: 11px; color: var(--rdl-dim); padding: 6px 12px;
    border-top: 1px solid var(--rdl-border); display: flex; justify-content: space-between; }
</style></head>
<body>
  <div id="stale-bar" class="stale-bar">
    <span>⚠</span><span id="stale-text">Showing last good elaboration</span>
  </div>
  <div id="tabs" class="tabs"></div>
  <div class="body">
    <div id="tree-host" class="tree-host">
      <div class="empty">
        <h2>Memory map viewer</h2>
        <p>Waiting for elaborated tree from <code>systemrdl-lsp</code>…</p>
      </div>
    </div>
    <div id="detail">
      <div class="placeholder">Select a register to see details.</div>
    </div>
  </div>
  <div id="status" class="status">
    <span id="status-left">—</span>
    <span id="status-right">v0.4 walking skeleton</span>
  </div>
<script>
const vscode = acquireVsCodeApi();

let state = {
  roots: [],
  activeRootIndex: 0,
  expandedRegs: new Set(),
  selectedRegKey: null,
};

window.addEventListener('message', (event) => {
  const m = event.data;
  if (m.type === 'tree')  applyTree(m.tree);
  else if (m.type === 'error') showError(m.message);
});

function applyTree(tree) {
  state.roots = tree.roots || [];
  if (state.activeRootIndex >= state.roots.length) state.activeRootIndex = 0;
  document.getElementById('stale-bar').classList.toggle('shown', !!tree.stale);
  document.getElementById('stale-text').textContent = tree.stale
    ? 'Showing last good elaboration · current parse failed'
    : 'Showing last good elaboration';
  if (!state.roots.length) {
    showEmpty();
    return;
  }

  // Auto-select first reg if nothing is currently selected (D4).
  if (!state.selectedRegKey || !findRegByKey(state.roots[state.activeRootIndex], state.selectedRegKey)) {
    const first = findFirstReg(state.roots[state.activeRootIndex]);
    if (first) {
      state.selectedRegKey = regKey(first);
      state.expandedRegs.add(state.selectedRegKey);
    }
  }

  renderTabs();
  renderTree();
  renderDetail();

  const total = countRegs(state.roots);
  const elapsed = tree.elaboratedAt ? new Date(tree.elaboratedAt).toLocaleTimeString() : '';
  document.getElementById('status-left').textContent =
    \`Elaborated \${elapsed} · \${total} register\${total === 1 ? '' : 's'} · \${state.roots[state.activeRootIndex].name}\`;
}

function countRegs(roots) {
  let n = 0;
  const walk = (node) => {
    if (node.kind === 'reg') n++;
    else if (node.children) node.children.forEach(walk);
  };
  roots.forEach(walk);
  return n;
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
      state.expandedRegs.clear();
      state.selectedRegKey = null;
      // Force re-derivation of selection on next applyTree call by triggering a render
      // with the same data. We just walk what's already in state.
      const first = findFirstReg(state.roots[i]);
      if (first) {
        state.selectedRegKey = regKey(first);
        state.expandedRegs.add(state.selectedRegKey);
      }
      renderTabs();
      renderTree();
      renderDetail();
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
  walk(root, tree, 0);
  host.appendChild(tree);

  // Scroll the selected row into view.
  const sel = host.querySelector('.row.selected');
  if (sel) sel.scrollIntoView({ block: 'nearest' });
}

function findFirstReg(node) {
  if (node.kind === 'reg') return node;
  for (const c of node.children || []) {
    const r = findFirstReg(c);
    if (r) return r;
  }
  return null;
}

function findRegByKey(node, key) {
  if (node.kind === 'reg') return regKey(node) === key ? node : null;
  for (const c of node.children || []) {
    const r = findRegByKey(c, key);
    if (r) return r;
  }
  return null;
}

function regKey(reg) { return reg.address + '|' + reg.name; }

function walk(node, host, depth) {
  if (node.kind === 'addrmap' || node.kind === 'regfile') {
    if (node.kind === 'regfile') {
      const row = document.createElement('div');
      row.className = 'row indent-' + depth;
      row.innerHTML = '<span class="caret">▼</span>' +
        '<span class="addr">' + node.address + '</span>' +
        '<span class="name">' + escapeHtml(node.name) + '</span>' +
        '<span class="access" title="regfile">regfile</span>';
      host.appendChild(row);
    }
    (node.children || []).forEach(c =>
      walk(c, host, depth + (node.kind === 'regfile' ? 1 : 0)));
    return;
  }
  if (node.kind === 'reg') {
    const expanded = state.expandedRegs.has(regKey(node));
    const selected = state.selectedRegKey === regKey(node);
    const row = document.createElement('div');
    row.className = 'row expandable indent-' + depth + (selected ? ' selected' : '');
    row.innerHTML = '<span class="caret">' + (expanded ? '▼' : '▶') + '</span>' +
      '<span class="addr">' + node.address + '</span>' +
      '<span class="name">' + escapeHtml(node.name) + '</span>' +
      '<span class="access">' + (node.accessSummary || '') + '</span>';
    row.addEventListener('click', () => {
      state.selectedRegKey = regKey(node);
      if (expanded) state.expandedRegs.delete(regKey(node));
      else state.expandedRegs.add(regKey(node));
      renderTree();
      renderDetail();
    });
    host.appendChild(row);
    if (expanded) (node.fields || []).forEach(f => {
      const r = document.createElement('div');
      r.className = 'field-row-inline';
      const accLower = (f.access || 'na').toLowerCase();
      r.innerHTML = '<b>[' + f.msb + ':' + f.lsb + ']</b>' +
        '<b>' + escapeHtml(f.name) + '</b>' +
        '<span class="pill ' + accLower + '">' + accLower.toUpperCase() + '</span>' +
        '<span>' + (f.reset || '—') + '</span>' +
        '<span style="color:var(--rdl-dim);font-family:var(--rdl-font-chrome);font-style:italic">' +
          escapeHtml(f.desc || '') + '</span>';
      host.appendChild(r);
    });
  }
}

function renderDetail() {
  const host = document.getElementById('detail');
  if (!state.selectedRegKey) {
    host.innerHTML = '<div class="placeholder">Select a register to see details.</div>';
    return;
  }
  const reg = findRegByKey(state.roots[state.activeRootIndex], state.selectedRegKey);
  if (!reg) {
    host.innerHTML = '<div class="placeholder">Selected register no longer exists.</div>';
    return;
  }
  const path = state.roots[state.activeRootIndex].name + '.' + reg.name;
  let html = '';
  html += '<h2>' + escapeHtml(reg.name) + '</h2>';
  html += '<div class="breadcrumb">' + escapeHtml(path) + '</div>';
  html += '<div class="meta">';
  html += '<span class="k">Address</span><span class="v">' + reg.address + '</span>';
  html += '<span class="k">Width</span><span class="v">' + reg.width + '</span>';
  if (reg.reset !== undefined) {
    html += '<span class="k">Reset</span><span class="v">' + reg.reset + '</span>';
  } else {
    html += '<span class="k">Reset</span><span class="v">—</span>';
  }
  html += '<span class="k">Access</span><span class="v">' + (reg.accessSummary || '—') + '</span>';
  html += '</div>';
  if (reg.desc) html += '<div class="desc">' + escapeHtml(reg.desc) + '</div>';
  html += '<div class="fields-title">Bit fields</div>';
  (reg.fields || []).forEach(f => {
    const accLower = (f.access || 'na').toLowerCase();
    html += '<div class="field" data-source="' + (f.source ? encodeURIComponent(JSON.stringify(f.source)) : '') + '"' +
      (f.source ? ' style="cursor:pointer" title="Click to reveal in editor"' : '') + '>' +
      '<b>[' + f.msb + ':' + f.lsb + ']</b>' +
      '<b>' + escapeHtml(f.name) + '</b>' +
      '<span class="pill ' + accLower + '">' + accLower.toUpperCase() + '</span>' +
      '<span>' + (f.reset || '—') + '</span>' +
      '<span class="desc">' + escapeHtml(f.desc || '') + '</span>' +
      '</div>';
  });
  if (reg.source) {
    const fileName = (reg.source.uri || '').split('/').pop() || reg.source.uri;
    html += '<div class="src-link" data-source="' + encodeURIComponent(JSON.stringify(reg.source)) + '">' +
      '→ ' + escapeHtml(fileName) + ':' + ((reg.source.line || 0) + 1) + '</div>';
  }
  host.innerHTML = html;

  // Wire reveal handlers (W6: bidirectional source map).
  host.querySelectorAll('[data-source]').forEach(el => {
    const raw = el.getAttribute('data-source');
    if (!raw) return;
    el.addEventListener('click', () => {
      try {
        const source = JSON.parse(decodeURIComponent(raw));
        vscode.postMessage({ type: 'reveal', source });
      } catch (e) { /* ignore */ }
    });
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}

function showEmpty() {
  document.getElementById('tabs').innerHTML = '';
  document.getElementById('tree-host').innerHTML =
    '<div class="empty">' +
    '<h2>No top-level addrmap found</h2>' +
    '<p>The viewer renders only elaborated maps. For library files (regfile/reg without addrmap), ' +
    'use hover and the Outline view in the editor.</p>' +
    '</div>';
  document.getElementById('detail').innerHTML =
    '<div class="placeholder">No selection.</div>';
  document.getElementById('status-left').textContent = '—';
}

function showError(msg) {
  document.getElementById('tree-host').innerHTML =
    '<div class="empty">' +
    '<h2>Could not load tree</h2>' +
    '<p><code>' + escapeHtml(msg) + '</code></p>' +
    '</div>';
}
</script>
</body></html>`;
}
