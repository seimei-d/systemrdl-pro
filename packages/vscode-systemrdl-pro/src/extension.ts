import * as cp from 'node:child_process';
import * as crypto from 'node:crypto';
import * as vscode from 'vscode';
import {
  CloseAction,
  ErrorAction,
  LanguageClient,
  type LanguageClientOptions,
  type ServerOptions,
} from 'vscode-languageclient/node';
import type {
  Addrmap,
  ElaboratedTree,
  Reg,
  Regfile,
  SourceLoc,
} from './types/elaborated-tree.generated';

let client: LanguageClient | undefined;
// Resolves once `client.start()` has succeeded. The webview-panel
// deserializer awaits this so it doesn't race the LSP boot — without
// it a panel restored on Reload Window arrives before the LSP is up
// and gets an empty tree.
let clientReady: Promise<void> = new Promise(() => { /* replaced in startServer */ });
let signalClientReady: () => void = () => undefined;
function resetClientReadyPromise(): void {
  clientReady = new Promise(resolve => { signalClientReady = resolve; });
}
resetClientReadyPromise();
let outputChannel: vscode.LogOutputChannel | undefined;

// One Memory Map panel per .rdl file (markdown-preview-style). Key is the
// document URI string; value carries the panel + its latest tree snapshot
// for the status-bar refresher.
type PanelEntry = {
  panel: vscode.WebviewPanel;
  uri: string;
  lastTree?: ElaboratedTree;
  // per-panel cursor-sync state. Pre-T4-B these were
  // module-globals — with two panels open (multi-root, Decision 3C),
  // a reveal in panel A would suppress the next cursor sync for
  // panel B too, and a cursor move in panel A's editor would clear
  // panel B's pending debounce. Each panel now owns its own.
  cursorSyncTimer?: ReturnType<typeof setTimeout>;
  suppressNextCursorSync?: boolean;
  // cached reg count + root-name list, computed once when
  // a fresh tree arrives. updateStatusBar fires per tab-switch and
  // per debounced diag change; countRegs is O(N), running it on
  // every call wasted measurable time on big designs.
  cachedRegCount?: number;
  cachedRootNames?: string;
  // in-flight refreshMemoryMap promise. Coalesces concurrent calls
  // (drag/resize fires onDidChangeViewState repeatedly). Pre-T4-D
  // each one issued a fresh rdl/elaboratedTree LSP request even
  // though the sinceVersion guard short-circuited the response body.
  refreshing?: Promise<void>;
  // Set when refreshMemoryMap is called while a fetch is in flight.
  // Without this flag the inflight guard would silently drop the
  // newer request and the viewer could stay 1+ versions behind
  // until some unrelated event re-triggered refresh — observed in
  // the field as "edited address, took 15s to show". One queued
  // re-fetch is enough; subsequent calls during the queued one
  // coalesce into the same flag.
  refreshQueued?: boolean;
};
const memoryMapPanels = new Map<string, PanelEntry>();

let statusBarItem: vscode.StatusBarItem | undefined;

// Eng-review safety net #3: LSP supervisor.
// We auto-restart up to MAX_RESTARTS times within RESTART_WINDOW_MS. A burst of
// crashes (broken Python install, bad pygls upgrade, etc.) hits the cap and we
// surface a banner instead of looping forever. Successful uptime past the window
// resets the counter so a single crash later doesn't poison the next session.
const MAX_RESTARTS = 3;
const RESTART_WINDOW_MS = 60_000;
let recentCrashTimes: number[] = [];
// cursor-sync suppression and debounce live PER panel on
// PanelEntry — see the type above. Module-level state used to drop
// events for the wrong panel in multi-root setups.
const CURSOR_SYNC_DEBOUNCE_MS = 500;
// Debounce for the diagnostics-change → status-bar refresh path.
// One firing per ~200 ms is enough; the underlying LSP publishes can
// spike at multi-per-keystroke rates during elaboration catchup.
let diagDebounceTimer: ReturnType<typeof setTimeout> | undefined;

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  outputChannel = vscode.window.createOutputChannel('SystemRDL Pro', { log: true });
  context.subscriptions.push(outputChannel);

  statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusBarItem.command = 'systemrdl-pro.showMemoryMap';
  statusBarItem.tooltip = 'Click to open SystemRDL Memory Map';
  context.subscriptions.push(statusBarItem);
  // register the flash decoration disposable now (lazy
  // creation on first reveal). The disposable wrapper handles the
  // possibility that the decoration is never actually instantiated.
  context.subscriptions.push({
    dispose: () => {
      if (flashDecoration) {
        try { flashDecoration.dispose(); } catch { /* ignore */ }
        flashDecoration = undefined;
      }
    },
  });

  context.subscriptions.push(
    vscode.commands.registerCommand('systemrdl-pro.showMemoryMap', () =>
      showMemoryMap(context),
    ),
    vscode.commands.registerCommand('systemrdl-pro.restartServer', () =>
      restartServer(context),
    ),
    vscode.commands.registerCommand('systemrdl-pro.showIncludePaths', () =>
      showIncludePaths(),
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
      if (entry.suppressNextCursorSync) {
        entry.suppressNextCursorSync = false;
        return;
      }
      const line = event.textEditor.selection.active.line;
      if (entry.cursorSyncTimer) clearTimeout(entry.cursorSyncTimer);
      entry.cursorSyncTimer = setTimeout(() => {
        safePostTo(entry, { type: 'cursor', line });
      }, CURSOR_SYNC_DEBOUNCE_MS);
    }),
    // Refresh status bar diag count when diagnostics change for the URI the
    // active editor is on. Debounced — pre-T4-D this fired once per LSP
    // diagnostic publish (multiple per keystroke during debounce catchup),
    // each call iterating the entire DiagnosticCollection via
    // `vscode.languages.getDiagnostics`.
    vscode.languages.onDidChangeDiagnostics(event => {
      const active = vscode.window.activeTextEditor;
      if (!active) return;
      const activeUri = active.document.uri.toString();
      if (!event.uris.some(u => u.toString() === activeUri)) return;
      if (diagDebounceTimer) clearTimeout(diagDebounceTimer);
      diagDebounceTimer = setTimeout(() => {
        const entry = memoryMapPanels.get(activeUri);
        if (entry?.lastTree) updateStatusBar(entry.lastTree, activeUri);
      }, 200);
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

  // Register a serializer so memory-map panels survive a window reload.
  // VSCode persists the webview panel's state (the URI we wrote via
  // `vscode.setState({ uri })` in the inline init script) and calls
  // `deserializeWebviewPanel` after reload to recreate the same panel.
  context.subscriptions.push(
    vscode.window.registerWebviewPanelSerializer('systemrdl-pro.memoryMap', {
      async deserializeWebviewPanel(panel, state) {
        const uri = (state && typeof state === 'object' && typeof (state as { uri?: unknown }).uri === 'string')
          ? (state as { uri: string }).uri
          : null;
        if (!uri) {
          panel.dispose();
          return;
        }
        const viewerDist = vscode.Uri.joinPath(context.extensionUri, 'media', 'viewer');
        panel.webview.options = {
          enableScripts: true,
          localResourceRoots: [viewerDist],
        };
        attachMemoryMapPanel(context, panel, uri);
        // The LSP may still be starting — its `client.start()` hasn't
        // resolved yet during early-activation deserialization. Wait
        // for it, then open the document so the language client fires
        // didOpen and the LSP populates its elaboration cache.
        // Without this step the panel comes up empty — the user had to
        // click into the editor to trigger a didOpen manually.
        try {
          await clientReady;
          await vscode.workspace.openTextDocument(vscode.Uri.parse(uri));
        } catch (err) {
          outputChannel?.warn(`deserialize: open document failed for ${uri}: ${err}`);
        }
        await refreshMemoryMap(uri).catch(() => undefined);
      },
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
  // settle the clientReady promise on EVERY exit path,
  // including the early returns below. Pre-T4-B clientReady was a
  // bare `Promise<void>` whose resolver was only called inside the
  // success branch of `client.start()`. If we hit the no-python or
  // no-module early return, the promise stayed pending forever and
  // any deserialized panel awaiting `clientReady` (line ~155 in
  // PanelSerializer) blocked on it indefinitely — viewer stayed
  // permanently blank with no error message even after a restart
  // fixed the underlying issue. We signal ready on early exit so
  // awaiters at least unblock and can render their own "no LSP
  // available" state instead of hanging.
  const python = await resolvePython();
  if (!python) {
    signalClientReady();
    return;
  }

  const moduleAvailable = await checkLspModule(python);
  if (!moduleAvailable) {
    showInstallBanner(python);
    signalClientReady();
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
      // capture the watcher into the lifecycle aggregate so
      // it gets disposed on restart / deactivate. The
      // LanguageClientOptions.synchronize handoff doesn't guarantee
      // disposal — every restart used to mint a fresh watcher and
      // leak the prior file-descriptor handle.
      fileEvents: lspFileWatcher = vscode.workspace.createFileSystemWatcher('**/*.rdl'),
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

  // T1.6: register a StaticFeature that injects experimental.systemrdlLazyTree
  // into the InitializeParams. Server reads this in INITIALIZED and switches
  // its rdl/elaboratedTree responses to spine envelopes (placeholder regs +
  // on-demand expandNode). Old servers see an unknown experimental key and
  // ignore it; we still get a full tree from them. Forward-compat both ways.
  client.registerFeature({
    fillClientCapabilities: (capabilities: { experimental?: Record<string, unknown> }) => {
      const exp = (capabilities.experimental ??= {});
      exp.systemrdlLazyTree = true;
    },
    initialize: () => {},
    getState: () => ({ kind: 'static' as const }),
    clear: () => {},
  });

  // each call to startServer (initial + every restartServer)
  // used to push a NEW `{ dispose: () => client?.stop() }` into
  // context.subscriptions WITHOUT removing the previous one, AND
  // register a fresh set of `client.onNotification` handlers. After N
  // restarts the extension would call client.stop() N+1 times on
  // shutdown (some throwing because the client was already stopped)
  // and fire each push notification N×refreshMemoryMap. Track the
  // current per-startServer disposable in a module-level variable
  // and dispose the OLD one before pushing the new one. The
  // notification handlers use vscode-languageclient's Disposable
  // return value, which we collect into the same disposable so they
  // unhook automatically on stop.
  if (clientLifecycleDisposable) {
    clientLifecycleDisposable.dispose();
    clientLifecycleDisposable = undefined;
  }
  // dispose the prior file watcher (if any) before
  // LanguageClient takes ownership of the new one. Without this,
  // every restart leaks the previous watcher's fd/inotify handle.
  if (lspFileWatcher) {
    try { lspFileWatcher.dispose(); } catch { /* ignore */ }
    lspFileWatcher = undefined;
  }
  const lifecycleDisposables: vscode.Disposable[] = [];
  const lifecycleAggregate: vscode.Disposable = {
    dispose: () => {
      while (lifecycleDisposables.length) {
        const d = lifecycleDisposables.pop()!;
        try { d.dispose(); } catch { /* ignore */ }
      }
      try { client?.stop(); } catch { /* ignore */ }
    },
  };
  clientLifecycleDisposable = lifecycleAggregate;
  context.subscriptions.push(lifecycleAggregate);

  try {
    await client.start();
    outputChannel?.info(`LSP started via ${python}`);
    signalClientReady();

    // TODO-1: server-pushed "tree changed" notifications eliminate the wait
    // for didSaveTextDocument. The payload is metadata-only (uri + version);
    // refreshMemoryMap then asks for the body only when our cached version
    // is older. Skip the refresh when no panel is open for that URI — no
    // point re-fetching a tree nobody is rendering.
    lifecycleDisposables.push(
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
      }),
    );

    // Re-elaborate started/finished — drives the viewer's "re-elaborating"
    // banner so the user knows the LSP is working on a fresh tree while the
    // existing one stays interactive. Fired only when the LSP actually runs
    // a full pass (the buffer-equality skip in _full_pass_async never fires
    // these), so the banner doesn't blink on no-op didChanges.
    lifecycleDisposables.push(
      client.onNotification('rdl/elaborationStarted', (params: { uri: string }) => {
        if (!params || typeof params.uri !== 'string') return;
        const entry = memoryMapPanels.get(params.uri);
        if (!entry) return;
        safePostTo(entry, { type: 'elaborating', state: true });
      }),
    );
    lifecycleDisposables.push(
      client.onNotification('rdl/elaborationFinished', (params: { uri: string }) => {
        if (!params || typeof params.uri !== 'string') return;
        const entry = memoryMapPanels.get(params.uri);
        if (!entry) return;
        safePostTo(entry, { type: 'elaborating', state: false });
      }),
    );
  } catch (err) {
    outputChannel?.error(`Failed to start LSP: ${err}`);
    showRestartBanner();
    // same reasoning as the early-returns above — if
    // client.start() throws (e.g., transport handshake failure,
    // pygls version mismatch), unblock awaiters so they can render
    // a fallback state instead of hanging on the readiness gate.
    signalClientReady();
  }
}

// tracked outside startServer so restartServer can dispose
// the prior lifecycle aggregate before standing up a fresh one.
let clientLifecycleDisposable: vscode.Disposable | undefined;
// tracked outside startServer to ensure proper disposal
// across restartServer cycles. createFileSystemWatcher leaks the
// inotify/fd handle if not disposed.
let lspFileWatcher: vscode.FileSystemWatcher | undefined;

async function restartServer(context: vscode.ExtensionContext): Promise<void> {
  // User-initiated restart resets the supervisor's crash budget — the user
  // presumably fixed whatever was causing the crashes.
  recentCrashTimes = [];
  if (client) {
    await client.stop();
    client = undefined;
  }
  // Reset the readiness gate so any deserializer waiting on a previous
  // promise gets the fresh signal from the new client.start().
  resetClientReadyPromise();
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
  attachMemoryMapPanel(context, panel, uri);
  await refreshMemoryMap(uri);
}

/**
 * Attach all the per-panel wiring (handlers, HTML, tracking) to a freshly
 * created or freshly deserialized `WebviewPanel`. Single source of truth so
 * the live `showMemoryMap` path and the `WebviewPanelSerializer.deserializeWebviewPanel`
 * path produce identical setup.
 */
function attachMemoryMapPanel(
  context: vscode.ExtensionContext,
  panel: vscode.WebviewPanel,
  uri: string,
): void {
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
  panel.webview.html = renderViewerHtml(panel.webview, context.extensionUri, uri);
}

/** Post to a specific panel; no-ops gracefully if the panel was disposed. */
function safePostTo(entry: PanelEntry, message: unknown): void {
  // postMessage returns a Thenable<boolean> that REJECTS when the webview
  // has been disposed (panel.dispose() — e.g. user closed the Memory Map
  // tab). The synchronous try/catch only catches errors thrown immediately,
  // not promise rejections, so a closed-panel post used to surface as
  // unhandled "Error: Webview is disposed" in the host log. We attach a
  // .then handler to swallow it explicitly — this is the normal race when
  // a late LSP notification arrives just after the user closes the panel.
  try {
    Promise.resolve(entry.panel.webview.postMessage(message)).then(
      undefined,
      () => {},
    );
  } catch (err) {
    outputChannel?.warn(`webview.postMessage failed: ${err}`);
  }
}

// ---------------------------------------------------------------------------
// Webview → extension messaging (W6: bidirectional source map)
// ---------------------------------------------------------------------------

type WebviewMessage =
  | { type: 'reveal'; source: SourceLoc }
  | { type: 'copy'; text: string; label?: string }
  | { type: 'expandNode'; version: number; nodeId: string };

async function handleWebviewMessage(msg: WebviewMessage, panelUri: string): Promise<void> {
  if (msg.type === 'reveal') {
    await revealLocation(msg.source);
  } else if (msg.type === 'copy') {
    await vscode.env.clipboard.writeText(msg.text);
    const label = msg.label || 'value';
    vscode.window.setStatusBarMessage(`Copied ${label}: ${msg.text}`, 2_000);
  } else if (msg.type === 'expandNode') {
    // T1.6: viewer asked to flesh out a placeholder reg's fields[]. Forward
    // to the LSP using the panel's URI (implicit — webview doesn't track it),
    // post the result back to the webview, and splice it into entry.lastTree
    // so subsequent sinceVersion checks stay coherent.
    if (!client) return;
    const entry = memoryMapPanels.get(panelUri);
    if (!entry) return;
    try {
      const reg = await client.sendRequest<unknown>('rdl/expandNode', {
        uri: panelUri,
        version: msg.version,
        nodeId: msg.nodeId,
      });
      // Server returns a soft `{outdated: true}` sentinel when the version
      // ask is stale — that's a normal race (new tree was elaborated between
      // the viewer reading tree.version and the request landing). Route it
      // to the error channel so the viewer clears in-flight tracking and
      // retries on the next onTreeUpdate (which will carry a fresh version).
      if (reg && typeof reg === 'object' && (reg as { outdated?: boolean }).outdated) {
        // echo the version back so the webview can route the
        // result to the right `${version}:${nodeId}` resolver key. The
        // pre-T4-A wire shape (no version field) keyed only on
        // ``nodeId`` and silently overwrote prior resolvers when the
        // user double-clicked or when a v2 elaborate landed mid-flight.
        safePostTo(entry, {
          type: 'expandNodeError',
          version: msg.version,
          nodeId: msg.nodeId,
          message: 'outdated',
        });
        return;
      }
      safePostTo(entry, {
        type: 'expandNodeResult',
        version: msg.version,
        nodeId: msg.nodeId,
        reg,
      });
      if (entry.lastTree) {
        spliceExpandedNode(entry.lastTree, msg.nodeId, reg);
      }
    } catch (err) {
      outputChannel?.warn(`rdl/expandNode failed for ${msg.nodeId}: ${err}`);
      safePostTo(entry, {
        type: 'expandNodeError',
        version: msg.version,
        nodeId: msg.nodeId,
        message: String(err),
      });
    }
  }
}

/** Walk an ElaboratedTree and replace the matching placeholder reg with
 * the expanded full reg dict. Mutates `tree` in place. T1.6: keeps
 * `entry.lastTree` consistent with what the webview now displays so the
 * next sinceVersion check on the same version returns `unchanged` correctly.
 */
function spliceExpandedNode(tree: ElaboratedTree, nodeId: string, expanded: unknown): void {
  type Walkable = { nodeId?: string; children?: unknown[]; fields?: unknown[]; loadState?: string; kind?: string };
  const stack: Walkable[] = [...((tree.roots as Walkable[]) ?? [])];
  while (stack.length > 0) {
    const node = stack.pop()!;
    if (node.kind === 'reg' && node.nodeId === nodeId && node.loadState === 'placeholder') {
      Object.assign(node, expanded);
      // expand_node response intentionally omits loadState — we must clear
      // the placeholder marker explicitly, otherwise next sinceVersion check
      // ships the still-marked node back to the webview and the viewer
      // re-fires expand in a loop.
      node.loadState = 'loaded';
      return;
    }
    if (Array.isArray(node.children)) stack.push(...(node.children as Walkable[]));
  }
}

// lazy-create the decoration on first activate() so we can
// push it to context.subscriptions for proper disposal. Pre-T4-B this
// was a module-load-time singleton with no disposal path — leaked one
// VSCode decoration handle per extension-host activation.
let flashDecoration: vscode.TextEditorDecorationType | undefined;
function getFlashDecoration(): vscode.TextEditorDecorationType {
  if (!flashDecoration) {
    flashDecoration = vscode.window.createTextEditorDecorationType({
      backgroundColor: new vscode.ThemeColor('editor.findMatchHighlightBackground'),
      isWholeLine: true,
    });
  }
  return flashDecoration;
}

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
    // per-panel suppression. The cursor change we're about to
    // cause must not bounce back as a cursor-sync message into the
    // viewer panel that initiated the reveal; suppress one cycle on
    // THAT panel only. Pre-T4-B this was a module-global, so a
    // reveal in panel A would silently drop the next cursor sync for
    // panel B too in multi-root setups.
    const docUriStr = doc.uri.toString();
    const targetEntry = memoryMapPanels.get(docUriStr);
    if (targetEntry) targetEntry.suppressNextCursorSync = true;
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
    const dec = getFlashDecoration();
    editor.setDecorations(dec, [range]);
    setTimeout(() => editor.setDecorations(dec, []), 200);
  } catch (err) {
    outputChannel?.warn(`reveal ${loc.uri}:${line + 1} failed: ${err}`);
    const docUriStr = uri.toString();
    const targetEntry = memoryMapPanels.get(docUriStr);
    if (targetEntry) targetEntry.suppressNextCursorSync = false;
  }
}

async function refreshMemoryMap(uri: string): Promise<void> {
  const entry = memoryMapPanels.get(uri);
  if (!entry || !client) return;
  // Coalesce concurrent calls AND queue exactly one follow-up. Pre-fix
  // the inflight guard alone silently dropped any version notification
  // that arrived during an in-flight fetch, leaving the viewer one
  // version behind until some unrelated event re-triggered refresh
  // (observed as "edited address, took 15s to update"). Mirror the
  // CLI's refreshInFlight + refreshQueued idiom — N→1 coalescing on
  // arrival, plus one extra fetch after the in-flight one finishes
  // if anything was dropped.
  if (entry.refreshing) {
    entry.refreshQueued = true;
    return entry.refreshing;
  }
  const run = () => {
    const p = doRefreshMemoryMap(uri, entry).finally(() => {
      entry.refreshing = undefined;
      if (entry.refreshQueued) {
        entry.refreshQueued = false;
        // Fire-and-forget — caller has long since returned.
        run();
      }
    });
    entry.refreshing = p;
    return p;
  };
  return run();
}

async function doRefreshMemoryMap(uri: string, entry: PanelEntry): Promise<void> {
  if (!client) return;
  try {
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
    // P13: hydrate the per-panel cache so updateStatusBar doesn't
    // recompute O(N) every tab switch / diag debounce tick.
    entry.cachedRegCount = countRegs(reply.roots);
    entry.cachedRootNames = reply.roots.map(r => r.name).join(', ');
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
  // P13: prefer the cached counts populated by refreshMemoryMap. Fall
  // back to live recompute if the entry hasn't been hydrated yet (e.g.,
  // we got here via a path that doesn't go through refreshMemoryMap).
  const entry = memoryMapPanels.get(uri);
  const total = entry?.cachedRegCount ?? countRegs(tree.roots);
  const rootNames =
    entry?.cachedRootNames ?? tree.roots.map(r => r.name).join(', ');
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

type IncludePathsReply = {
  uri: string | null;
  paths: { path: string; source: 'setting' | 'peakrdl.toml' | 'sibling' }[];
};

async function showIncludePaths(): Promise<void> {
  const target = pickTargetUri();
  if (!target) {
    vscode.window.showInformationMessage(
      'Open a SystemRDL (.rdl) file first — include paths are resolved per-file.',
    );
    return;
  }
  if (!client) {
    vscode.window.showWarningMessage('LSP not running.');
    return;
  }

  let reply: IncludePathsReply;
  try {
    reply = await client.sendRequest<IncludePathsReply>('rdl/includePaths', {
      uri: target.toString(),
    });
  } catch (err) {
    vscode.window.showErrorMessage(`rdl/includePaths failed: ${err}`);
    return;
  }

  if (!reply.paths.length) {
    vscode.window.showInformationMessage(
      'No include search paths in effect. Set systemrdl-pro.includePaths or drop a peakrdl.toml.',
    );
    return;
  }

  const items: vscode.QuickPickItem[] = reply.paths.map(p => ({
    label: p.path,
    description: `· from ${p.source}`,
    detail: p.source === 'setting'
      ? 'systemrdl-pro.includePaths (workspace settings.json)'
      : p.source === 'peakrdl.toml'
        ? '[parser] incl_search_paths in an ancestor peakrdl.toml'
        : "Implicit fallback to the file's own directory",
  }));

  const picked = await vscode.window.showQuickPick(items, {
    title: `Effective include paths for ${vscode.workspace.asRelativePath(target)}`,
    placeHolder: 'Press Enter on a path to reveal it in the OS file manager. Esc to dismiss.',
    matchOnDescription: true,
    matchOnDetail: true,
  });
  if (picked) {
    try {
      await vscode.commands.executeCommand('revealFileInOS', vscode.Uri.file(picked.label));
    } catch (err) {
      outputChannel?.warn(`revealFileInOS failed for ${picked.label}: ${err}`);
    }
  }
}

// ---------------------------------------------------------------------------
// Webview shell — loads the @systemrdl-pro/viewer-core React bundle and wires
// up a postMessage transport. The renderer (Tree, Detail, ContextMenu, etc.)
// lives in viewer.js shared with the rdl-viewer CLI; this shell only declares
// the host element, transport bridge, and CSP.
// ---------------------------------------------------------------------------

/**
 * Read `systemrdl-pro.viewer.colors` and emit a `:root { --rdl-...: …; }`
 * block that overrides the design-token defaults baked into viewer.css.
 *
 * Empty config → empty string → no extra style. Unknown keys are silently
 * ignored (we only forward documented tokens).
 */
function readPaletteOverrides(): string {
  const cfg = vscode.workspace.getConfiguration('systemrdl-pro').get<Record<string, string>>('viewer.colors');
  if (!cfg || typeof cfg !== 'object' || Object.keys(cfg).length === 0) return '';
  // Map config key → CSS custom property name. Aligned with viewer-core/styles.css.
  const map: Record<string, string> = {
    rw: '--rdl-acc-rw', ro: '--rdl-acc-ro', wo: '--rdl-acc-wo',
    w1c: '--rdl-acc-w1c', rsv: '--rdl-acc-rsv',
    accent: '--rdl-accent', warning: '--rdl-warning',
    bg: '--rdl-bg', panel: '--rdl-panel', chrome: '--rdl-chrome',
    border: '--rdl-border', fg: '--rdl-fg', dim: '--rdl-dim',
    selected: '--rdl-selected',
  };
  const lines: string[] = [];
  for (const [key, val] of Object.entries(cfg)) {
    const cssVar = map[key];
    if (!cssVar || typeof val !== 'string' || !val.trim()) continue;
    // Disallow any character that could escape the style block. CSS values
    // are pretty open (rgb(), var(), etc.) but `;` `}` would terminate the rule.
    if (/[;}<>]/.test(val)) continue;
    lines.push(`${cssVar}: ${val.trim()};`);
  }
  if (!lines.length) return '';
  return `:root{${lines.join(' ')}}`;
}

function renderViewerHtml(
  webview: vscode.Webview,
  extensionUri: vscode.Uri,
  panelUri: string,
): string {
  const viewerJs = webview.asWebviewUri(
    vscode.Uri.joinPath(extensionUri, 'media', 'viewer', 'viewer.js'),
  );
  const viewerCss = webview.asWebviewUri(
    vscode.Uri.joinPath(extensionUri, 'media', 'viewer', 'viewer.css'),
  );
  // CSP: only allow scripts/styles from the webview source. The init script is
  // inline; we use a nonce so VSCode's webview CSP enforcement doesn't block it.
  const nonce = makeNonce();
  const paletteOverrides = readPaletteOverrides();
  return /* html */ `<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src ${webview.cspSource} 'unsafe-inline'; script-src ${webview.cspSource} 'nonce-${nonce}'; font-src ${webview.cspSource};">
<title>SystemRDL Memory Map</title>
<link rel="stylesheet" href="${viewerCss}">
<style>html,body,#app-root{height:100%;margin:0;}${paletteOverrides}</style>
</head>
<body>
  <div id="app-root"></div>
  <script nonce="${nonce}" src="${viewerJs}"></script>
  <script nonce="${nonce}">
  (function() {
    const vscode = acquireVsCodeApi();
    // Persist the panel's source URI so VSCode's webview-panel serializer
    // can restore the viewer after a window reload (Ctrl+Shift+P → Reload).
    vscode.setState({ uri: ${JSON.stringify(panelUri)} });
    const updaters = new Set();
    const cursorListeners = new Set();
    const elaboratingListeners = new Set();
    let pendingTree = null;
    let elaboratingState = false;
    // per-(version,nodeId) resolvers for the lazy expandNode
    // round-trip. Pre-T4-A keyed on nodeId only — rapid clicks and
    // cross-version races overwrote pending resolvers and leaked
    // promises (spinner stuck forever). Now keyed by version+":"+nodeId
    // so v1 results don't resolve v2 promises, and rapid duplicate
    // clicks for the same key short-circuit to the existing in-flight
    // promise instead of overwriting its resolver.
    const expandResolvers = new Map();
    const expandRejectors = new Map();
    const expandPending   = new Map();

    window.addEventListener('message', (e) => {
      const m = e.data;
      if (m && m.type === 'tree') {
        pendingTree = m.tree;
        updaters.forEach(cb => cb(m.tree));
      } else if (m && m.type === 'cursor') {
        cursorListeners.forEach(cb => cb(m.line));
      } else if (m && m.type === 'elaborating') {
        elaboratingState = !!m.state;
        elaboratingListeners.forEach(cb => cb(elaboratingState));
      } else if (m && m.type === 'expandNodeResult') {
        const key = (m.version != null ? m.version : '?') + ':' + m.nodeId;
        const r = expandResolvers.get(key);
        expandResolvers.delete(key);
        expandRejectors.delete(key);
        expandPending.delete(key);
        if (r) r(m.reg);
      } else if (m && m.type === 'expandNodeError') {
        const key = (m.version != null ? m.version : '?') + ':' + m.nodeId;
        const j = expandRejectors.get(key);
        expandResolvers.delete(key);
        expandRejectors.delete(key);
        expandPending.delete(key);
        if (j) j(new Error(m.message || 'expandNode failed'));
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
      onElaborating(cb) {
        elaboratingListeners.add(cb);
        // Replay the latest known state so listeners that subscribe after the
        // first started/finished message still see the correct banner.
        cb(elaboratingState);
        return () => elaboratingListeners.delete(cb);
      },
      reveal(source) { vscode.postMessage({ type: 'reveal', source }); },
      copy(text, label) { vscode.postMessage({ type: 'copy', text, label }); },
      expandNode(version, nodeId) {
        const key = version + ':' + nodeId;
        // Dedup in-flight requests for the same (version,nodeId): rapid
        // double-clicks must NOT post two LSP requests and overwrite
        // the resolver of the first.
        const existing = expandPending.get(key);
        if (existing) return existing;
        const p = new Promise((resolve, reject) => {
          expandResolvers.set(key, resolve);
          expandRejectors.set(key, reject);
        });
        expandPending.set(key, p);
        vscode.postMessage({ type: 'expandNode', version, nodeId });
        return p;
      },
    };

    RdlViewer.mount(document.getElementById('app-root'), transport);
  })();
  </script>
</body></html>`;
}

function makeNonce(): string {
  // cryptographically secure nonce per VSCode webview CSP guidance.
  // Pre-T4-B used Math.random() — fine for collision avoidance in
  // practice (VSCode's webview is sandboxed) but the CSP nonce mechanism
  // is meant to defeat injection attacks, and a non-CSPRNG nonce is a
  // category error a security audit would flag.
  return crypto.randomBytes(18).toString('base64url');
}

