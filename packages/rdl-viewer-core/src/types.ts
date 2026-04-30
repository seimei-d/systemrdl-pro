/**
 * Mirrors `schemas/elaborated-tree.json` v0.1.0. Hand-written for now;
 * Decision 9A schedules codegen → these types get auto-generated from the
 * JSON Schema in a later pass.
 */

export type ElaboratedTree = {
  schemaVersion: '0.1.0';
  elaboratedAt?: string;
  stale?: boolean;
  roots: Addrmap[];
};

export type Addrmap = {
  kind: 'addrmap';
  name: string;
  type?: string;
  displayName?: string;
  address: string;
  size: string;
  desc?: string;
  source?: SourceLoc;
  children: TreeNode[];
};

export type Regfile = {
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

export type Reg = {
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

export type Field = {
  name: string;
  displayName?: string;
  lsb: number;
  msb: number;
  access: string;
  reset?: string;
  desc?: string;
  source?: SourceLoc;
};

export type SourceLoc = {
  uri: string;
  line: number;
  column?: number;
  endLine?: number;
  endColumn?: number;
};

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
