// Auto-generated from `schemas/elaborated-tree.json`. DO NOT EDIT.
// Regenerate via `bun run codegen` (Decision 9A).


export type HexU64 = string;

export type SourceLoc = {
  uri: string;
  line: number;
  column?: number;
  endLine?: number;
  endColumn?: number;
};

export type AccessMode = 'rw' | 'ro' | 'wo' | 'w1c' | 'w0c' | 'w1s' | 'w0s' | 'rclr' | 'rset' | 'wclr' | 'wset' | 'na';

export type Field = {
  name: string;
  displayName?: string;
  lsb: number;
  msb: number;
  access: AccessMode;
  reset?: HexU64;
  desc?: string;
  source?: SourceLoc;
  encode?: {
  name: string;
  value: HexU64;
  desc?: string;
}[];
};

export type Reg = {
  kind: 'reg';
  name: string;
  type?: string;
  displayName?: string;
  address: HexU64;
  width: 8 | 16 | 32 | 64;
  reset?: HexU64;
  accessSummary?: string;
  desc?: string;
  source?: SourceLoc;
  fields: Field[];
};

export type Regfile = {
  kind: 'regfile';
  name: string;
  type?: string;
  displayName?: string;
  address: HexU64;
  size: HexU64;
  desc?: string;
  source?: SourceLoc;
  children: (Regfile | Reg)[];
};

export type Addrmap = {
  kind: 'addrmap';
  name: string;
  type?: string;
  displayName?: string;
  address: HexU64;
  size: HexU64;
  desc?: string;
  source?: SourceLoc;
  children: (Addrmap | Regfile | Reg)[];
};

export type ElaboratedTree = {
  schemaVersion: '0.1.0';
  version?: number;
  unchanged?: boolean;
  elaboratedAt?: string;
  stale?: boolean;
  roots: Addrmap[];
};
