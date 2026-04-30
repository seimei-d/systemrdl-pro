import { useEffect, useMemo, useRef } from 'react';
import type { Addrmap, Reg, SourceLoc, Transport, TreeNode } from './types';
import { isContainer, subtreeMatches, type FilterScope } from './util';
import type { CtxMenuItem } from './ContextMenu';

export type FlatRow =
  | { kind: 'container'; key: string; depth: number; expanded: boolean; hasChildren: boolean; node: Addrmap | (TreeNode & { kind: 'regfile' }); pathSegs: string[] }
  | { kind: 'reg'; key: string; depth: number; node: Reg; pathSegs: string[] };

/**
 * Walk the active root and produce a flat ordered list of visible rows
 * respecting filter and collapse state. The keyboard handler in <Viewer/>
 * walks this list for Up/Down/parent/child navigation, and the renderer
 * just maps it to <TreeRow/>s. Single source of truth.
 */
export function buildFlatList(
  root: TreeNode,
  filter: string,
  scope: FilterScope,
  collapsed: Set<string>,
): FlatRow[] {
  const out: FlatRow[] = [];
  function visit(node: TreeNode, depth: number, segs: string[]): void {
    if (filter && !subtreeMatches(node, filter, scope)) return;
    if (isContainer(node)) {
      const key = segs.concat([node.name]).join('.');
      const expanded = !!filter || !collapsed.has(key);
      out.push({
        kind: 'container', key, depth, expanded,
        hasChildren: (node.children || []).length > 0,
        node, pathSegs: segs.concat([node.name]),
      });
      if (expanded) {
        for (const c of node.children || []) visit(c, depth + 1, segs.concat([node.name]));
      }
      return;
    }
    if (node.kind === 'reg') {
      const path = segs.concat([node.name]);
      out.push({ kind: 'reg', key: path.join('.'), depth, node, pathSegs: path });
    }
  }
  visit(root, 0, []);
  return out;
}

type Props = {
  rows: FlatRow[];
  selectedKey: string | null;
  focusedKey: string | null;
  onSelectReg: (row: FlatRow & { kind: 'reg' }) => void;
  onToggleCollapse: (key: string) => void;
  onFocus: (key: string) => void;
  onContextMenu: (ev: React.MouseEvent, row: FlatRow) => void;
  filter: string;
  filterMatchCount: number;
  hasRoots: boolean;
};

export function Tree({
  rows, selectedKey, focusedKey, onSelectReg, onToggleCollapse, onFocus, onContextMenu,
  filter, filterMatchCount, hasRoots,
}: Props) {
  const hostRef = useRef<HTMLDivElement | null>(null);

  // Scroll focused row into view after each render.
  useEffect(() => {
    if (!hostRef.current || !focusedKey) return;
    const el = hostRef.current.querySelector<HTMLElement>(`[data-key="${cssAttr(focusedKey)}"]`);
    if (el) el.scrollIntoView({ block: 'nearest' });
  }, [focusedKey, rows]);

  // tabindex=0 + role=tree are kept for screen-reader semantics and so users
  // who Tab into the panel still land here. Arrow-key handling lives at the
  // document level in <Viewer/> so focus quirks in the webview iframe don't
  // disable it.

  if (!hasRoots) {
    return (
      <div ref={hostRef} className="rdl-tree-host" tabIndex={0} role="tree" aria-label="Memory map tree">
        <div className="rdl-empty">
          <h2>No top-level addrmap found</h2>
          <p>The viewer renders only elaborated maps. For library files (regfile/reg without addrmap), use hover and the Outline view in the editor.</p>
        </div>
      </div>
    );
  }

  return (
    <>
      {filter && (
        <div className="rdl-filter-hint" style={{ padding: '0 12px 6px', background: 'var(--rdl-panel)' }}>
          {filterMatchCount} match{filterMatchCount === 1 ? '' : 'es'}
        </div>
      )}
      <div
        ref={hostRef}
        className="rdl-tree-host"
        tabIndex={0}
        role="tree"
        aria-label="Memory map tree"
      >
        <div className="rdl-tree">
          {rows.map(row => (
            <TreeRow
              key={row.key}
              row={row}
              selected={selectedKey === row.key}
              focused={focusedKey === row.key}
              onSelectReg={onSelectReg}
              onToggleCollapse={onToggleCollapse}
              onFocus={onFocus}
              onContextMenu={onContextMenu}
            />
          ))}
        </div>
      </div>
    </>
  );
}

function cssAttr(s: string): string {
  return String(s).replace(/"/g, '\\"');
}

type RowProps = {
  row: FlatRow;
  selected: boolean;
  focused: boolean;
  onSelectReg: (row: FlatRow & { kind: 'reg' }) => void;
  onToggleCollapse: (key: string) => void;
  onFocus: (key: string) => void;
  onContextMenu: (ev: React.MouseEvent, row: FlatRow) => void;
};

function TreeRow({ row, selected, focused, onSelectReg, onToggleCollapse, onFocus, onContextMenu }: RowProps) {
  const indent = row.depth > 0 ? `rdl-indent-${Math.min(row.depth, 3)}` : '';
  const cls = ['rdl-row', indent];
  if (row.kind === 'container') cls.push('container');
  if (selected) cls.push('selected');
  if (focused) cls.push('focused');
  const className = cls.filter(Boolean).join(' ');

  if (row.kind === 'container') {
    const node = row.node;
    const caret = row.expanded ? '▼' : '▶';
    const kindLabel = node.kind + (node.type ? ` (${node.type})` : '');
    const handleClick = () => {
      onFocus(row.key);
      // Reveal in editor on body click — caret has its own listener with stopPropagation.
      // (No direct reveal here; the parent decides via onSelectReg-like callback if needed.)
    };
    return (
      <div
        className={className}
        role="treeitem"
        aria-level={row.depth + 1}
        aria-expanded={row.expanded}
        data-key={row.key}
        title="Click caret to fold"
        onClick={handleClick}
        onContextMenu={(e) => onContextMenu(e, row)}
      >
        <span
          className="caret caret-toggle"
          title={row.expanded ? 'Click to collapse' : 'Click to expand'}
          onClick={(e) => { e.stopPropagation(); onToggleCollapse(row.key); }}
        >{caret}</span>
        <span className="addr">{node.address}</span>
        <span className="name">{node.name}</span>
        <span className="access" title={kindLabel}>{kindLabel}</span>
      </div>
    );
  }

  // reg
  const reg = row.node;
  return (
    <div
      className={className}
      role="treeitem"
      aria-level={row.depth + 1}
      aria-selected={selected}
      data-key={row.key}
      onClick={() => onSelectReg(row)}
      onContextMenu={(e) => onContextMenu(e, row)}
    >
      <span className="caret"> </span>
      <span className="addr">{reg.address}</span>
      <span className="name">{reg.name}</span>
      <span className="access">{reg.accessSummary || ''}</span>
    </div>
  );
}
