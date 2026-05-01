/**
 * Re-exports the auto-generated SystemRDL elaborated-tree shape (Decision 9A)
 * plus a few viewer-only types that aren't in the schema (TreeNode union,
 * Container narrowing, Transport bridge contract).
 *
 * Schema source of truth: `schemas/elaborated-tree.json`. Regenerate via
 * `bun run codegen` — never hand-edit `_generated/elaborated-tree.ts`.
 */

export type {
  AccessMode,
  Addrmap,
  ElaboratedTree,
  Field,
  HexU64,
  Reg,
  Regfile,
  SourceLoc,
} from './_generated/elaborated-tree';

import type { Addrmap, ElaboratedTree, Reg, Regfile, SourceLoc } from './_generated/elaborated-tree';

export type TreeNode = Addrmap | Regfile | Reg;
export type Container = Addrmap | Regfile;

/**
 * Bridge contract. The host (VSCode webview or rdl-viewer CLI) implements
 * these so the viewer doesn't care which transport delivers tree updates
 * or where a "reveal" actually goes.
 */
export type Transport = {
  /** Initial fetch on mount. */
  getTree(): Promise<ElaboratedTree>;
  /**
   * Subscribe to live tree updates. Returns an unsubscribe function. Webview
   * implementations forward postMessage 'tree' frames; CLI implementations
   * consume Server-Sent Events.
   */
  onTreeUpdate(cb: (tree: ElaboratedTree) => void): () => void;
  /**
   * Optional: cursor-line push from the editor side (only meaningful in the
   * VSCode webview). The viewer uses this to auto-select a register when the
   * user moves the editor cursor onto its declaration line.
   */
  onCursorMove?(cb: (line0b: number) => void): () => void;
  /**
   * Reveal a source location in the host editor. Absent in the CLI surface.
   */
  reveal?(source: SourceLoc): void;
  /**
   * Copy text to the host clipboard. If absent, the viewer falls back to
   * navigator.clipboard with a toast on failure.
   */
  copy?(text: string, label?: string): void;
};
