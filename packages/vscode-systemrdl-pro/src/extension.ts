import * as cp from 'node:child_process';
import * as vscode from 'vscode';
import {
  CloseAction,
  ErrorAction,
  LanguageClient,
  type LanguageClientOptions,
  type ServerOptions,
} from 'vscode-languageclient/node';

let client: LanguageClient | undefined;
let outputChannel: vscode.LogOutputChannel | undefined;
let memoryMapPanel: vscode.WebviewPanel | undefined;

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
  if (!python) {
    // Banner has already been shown by resolvePython.
    return;
  }

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
        // Eng review silent-failure gap #1: surface a banner so the user can restart.
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
// Python resolution (decision 2B: explicit pythonPath + fallback chain)
// ---------------------------------------------------------------------------

async function resolvePython(): Promise<string | undefined> {
  // 1. Workspace setting — most explicit, wins.
  const setting = vscode.workspace
    .getConfiguration('systemrdl-pro')
    .get<string>('pythonPath', '')
    .trim();
  if (setting) {
    if (await isExecutable(setting)) return setting;
    showPythonNotFoundBanner(`Configured systemrdl-pro.pythonPath does not exist: ${setting}`);
    return undefined;
  }

  // 2. ms-python.python extension's selected interpreter.
  const fromMsPython = await getMsPythonInterpreter();
  if (fromMsPython) return fromMsPython;

  // 3. PATH lookup for `python3` then `python`.
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
    try {
      await ext.activate();
    } catch {
      return undefined;
    }
  }
  // ms-python.python exposes an environments API; we read the active one.
  // Fallback gracefully if the API shape changes.
  try {
    const api = ext.exports as
      | { environments?: { getActiveEnvironmentPath?: () => { path: string } | undefined } }
      | undefined;
    const active = api?.environments?.getActiveEnvironmentPath?.();
    if (active?.path) return active.path;
  } catch {
    // ignore
  }
  return undefined;
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
// Banners (eng review silent-failure gaps #1 + #3 + decision 2B)
// ---------------------------------------------------------------------------

function showPythonNotFoundBanner(detail: string): void {
  vscode.window
    .showErrorMessage(
      `SystemRDL Pro: ${detail}`,
      'Set pythonPath…',
      'Open Settings',
    )
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
// Memory Map webview (Week 1 placeholder; Week 4-5 replaces with rdl-viewer-core)
// ---------------------------------------------------------------------------

function showMemoryMap(context: vscode.ExtensionContext): void {
  if (memoryMapPanel) {
    memoryMapPanel.reveal(vscode.ViewColumn.Beside);
    return;
  }

  memoryMapPanel = vscode.window.createWebviewPanel(
    'systemrdl-pro.memoryMap',
    'SystemRDL Memory Map',
    vscode.ViewColumn.Beside,
    {
      enableScripts: false, // Week 4-5 will flip this when rdl-viewer-core is integrated
      retainContextWhenHidden: true,
    },
  );

  memoryMapPanel.iconPath = vscode.Uri.joinPath(context.extensionUri, 'media', 'icon.png');

  memoryMapPanel.onDidDispose(
    () => {
      memoryMapPanel = undefined;
    },
    null,
    context.subscriptions,
  );

  memoryMapPanel.webview.html = renderPlaceholder();
}

function renderPlaceholder(): string {
  // Week 1: PeakRDL-html fallback referenced in the design doc would go here.
  // For now we ship a neutral placeholder; design tokens from docs/design.md will
  // arrive with rdl-viewer-core in Week 4-5.
  return /* html */ `<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'unsafe-inline';">
<title>SystemRDL Memory Map</title>
<style>
  :root { color-scheme: dark light; }
  body {
    font-family: var(--vscode-font-family, 'Inter', system-ui, sans-serif);
    color: var(--vscode-foreground);
    background: var(--vscode-editor-background);
    margin: 0;
    padding: 32px 40px;
    line-height: 1.5;
  }
  h1 { font-size: 16px; font-weight: 600; margin: 0 0 8px; }
  p  { font-size: 13px; max-width: 60ch; color: var(--vscode-descriptionForeground); }
  code { font-family: var(--vscode-editor-font-family, 'JetBrains Mono', monospace); }
  .roadmap { margin-top: 24px; font-size: 12px; }
  .roadmap li { margin: 4px 0; }
  .tag { display: inline-block; padding: 1px 6px; font-size: 10px;
         background: var(--vscode-badge-background); color: var(--vscode-badge-foreground);
         border-radius: 2px; margin-left: 6px; vertical-align: middle; }
</style></head>
<body>
  <h1>Memory map viewer <span class="tag">Week 1 placeholder</span></h1>
  <p>
    Diagnostics are running. The interactive memory map ships in Week 4-5 — see
    the canonical design (mockup: variant B, tree + detail pane) in
    <code>docs/design.md</code>.
  </p>
  <ul class="roadmap">
    <li>Week 2-3 — full LSP (hover, outline, goto-def, completion)</li>
    <li>Week 4-5 — Svelte tree+detail viewer over <code>rdl/elaboratedTree</code></li>
    <li>Week 6 — bidirectional source map (click + hover ↔ editor)</li>
  </ul>
</body></html>`;
}
