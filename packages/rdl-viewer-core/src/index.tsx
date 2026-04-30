/**
 * Public entry point for `@systemrdl-pro/viewer-core`.
 *
 * Both the VSCode extension webview and the standalone `rdl-viewer` CLI
 * import this module. They mount the React viewer into a host element and
 * provide a Transport — the viewer is transport-agnostic, so postMessage
 * (webview) and fetch+SSE (CLI) plug in interchangeably.
 *
 * Usage (host side):
 *
 *     import { mount } from '@systemrdl-pro/viewer-core';
 *     import '@systemrdl-pro/viewer-core/style.css';
 *
 *     const unmount = mount(document.getElementById('root')!, {
 *       getTree: () => fetch('/tree').then(r => r.json()),
 *       onTreeUpdate: cb => { ... return unsub; },
 *       reveal: source => { ... },     // optional
 *       copy: (text, label) => { ... } // optional, falls back to navigator.clipboard
 *     });
 */

import { createRoot, type Root } from 'react-dom/client';
import { Viewer } from './Viewer';
import type { Transport } from './types';

export type { Transport, ElaboratedTree, Reg, Addrmap, Regfile, Field, SourceLoc, TreeNode } from './types';

export function mount(target: HTMLElement, transport: Transport): () => void {
  const root: Root = createRoot(target);
  root.render(<Viewer transport={transport} />);
  return () => root.unmount();
}
