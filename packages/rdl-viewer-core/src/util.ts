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
 * Whether the subtree rooted at `node` matches the filter. Filter scope:
 * register/container name, register address, field name, and field access mode.
 * A filter that looks hex (`looksLikeHex`) is normalised and substring-matched
 * against the canonical address form (so "0x10" / "10" / "0010" all hit a
 * register at 0x0000_0010).
 */
export function subtreeMatches(node: TreeNode, filter: string): boolean {
  if (!filter) return true;
  const lower = filter.toLowerCase();
  const hex = looksLikeHex(filter) ? normalizeAddr(filter) : null;
  if (node.kind === 'reg') {
    if (node.name.toLowerCase().includes(lower)) return true;
    if (hex && normalizeAddr(node.address).includes(hex)) return true;
    return (node.fields || []).some(
      f => f.name.toLowerCase().includes(lower) ||
           (f.access && f.access.toLowerCase().includes(lower)),
    );
  }
  if (node.name?.toLowerCase().includes(lower)) return true;
  if (hex && normalizeAddr(node.address).includes(hex)) return true;
  return (node.children || []).some(c => subtreeMatches(c, filter));
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
