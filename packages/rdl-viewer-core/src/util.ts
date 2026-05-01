import type { TreeNode, Container, Reg, Field } from './types';

export function isContainer(n: TreeNode): n is Container {
  return n.kind === 'addrmap' || n.kind === 'regfile';
}

/**
 * Hex-detect for the filter input — true when the string looks like a partial
 * hex value (with or without 0x, with or without _ separators).
 */
export function looksLikeHex(s: string): boolean {
  if (!s) return false;
  return /^(0x)?[0-9a-f_]+$/i.test(s);
}

export function normalizeAddr(s: string | undefined): string {
  return String(s || '').toLowerCase().replace(/^0x/, '').replace(/_/g, '');
}

/**
 * Filter scope — which fields the user wants the filter text matched against.
 * 'all' is the default and matches name + address + field name. Access mode
 * is intentionally excluded — searching by "rw" surfaces too many false
 * positives (every reg with at least one RW field), and there's no real use
 * case where the user needs to enumerate "all RW registers" interactively.
 */
export type FilterScope = 'all' | 'name' | 'address' | 'field';

/**
 * Whether the subtree rooted at `node` matches the filter under the chosen
 * scope. Hex normalisation still applies for address matches.
 */
export function subtreeMatches(node: TreeNode, filter: string, scope: FilterScope = 'all'): boolean {
  if (!filter) return true;
  const lower = filter.toLowerCase();
  const hex = looksLikeHex(filter) ? normalizeAddr(filter) : null;
  const m = (s: FilterScope) => scope === 'all' || scope === s;

  if (node.kind === 'reg') {
    if (m('name') && node.name.toLowerCase().includes(lower)) return true;
    if (m('address') && hex && normalizeAddr(node.address).includes(hex)) return true;
    if (m('field')) {
      // T1.7: a placeholder reg is opaque to field-scope filters — its
      // fields[] hasn't been fetched yet. Treat as match (false positive
      // beats false negative — user can click the reg to expand and see
      // for sure). Without this, filtering by field name in a lazy tree
      // would silently miss every unloaded reg.
      if (node.loadState === 'placeholder') return true;
      return (node.fields || []).some(f => f.name.toLowerCase().includes(lower));
    }
    return false;
  }
  if (m('name') && node.name?.toLowerCase().includes(lower)) return true;
  if (m('address') && hex && normalizeAddr(node.address).includes(hex)) return true;
  return (node.children || []).some(c => subtreeMatches(c, filter, scope));
}

export function findFirstReg(node: TreeNode, segs: string[]): { reg: Reg; path: string[]; key: string } | null {
  if (node.kind === 'reg') return { reg: node, path: segs, key: segs.join('.') };
  for (const c of node.children || []) {
    const r = findFirstReg(c, segs.concat([c.name]));
    if (r) return r;
  }
  return null;
}

export function findRegByKey(rootNode: TreeNode, key: string): { reg: Reg; path: string[] } | null {
  function walk(node: TreeNode, segs: string[]): { reg: Reg; path: string[] } | null {
    if (node.kind === 'reg') {
      return segs.join('.') === key ? { reg: node, path: segs } : null;
    }
    for (const c of node.children || []) {
      const r = walk(c, segs.concat([c.name]));
      if (r) return r;
    }
    return null;
  }
  return walk(rootNode, [rootNode.name]);
}

export function countRegs(roots: TreeNode[]): number {
  let n = 0;
  const visit = (node: TreeNode): void => {
    if (node.kind === 'reg') n++;
    else (node.children || []).forEach(visit);
  };
  roots.forEach(visit);
  return n;
}
