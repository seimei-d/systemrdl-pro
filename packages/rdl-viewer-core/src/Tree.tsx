import { useEffect, useMemo, useRef, useState } from 'react';
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
  onSelectReg: (row: FlatRow & { kind: 'reg' }) => void;
  onRevealContainer: (row: FlatRow & { kind: 'container' }) => void;
  onToggleCollapse: (key: string) => void;
  onContextMenu: (ev: React.MouseEvent, row: FlatRow) => void;
  filter: string;
  filterMatchCount: number;
  hasRoots: boolean;
};

export function Tree({
  rows, selectedKey, onSelectReg, onRevealContainer, onToggleCollapse, onContextMenu,
  filter, filterMatchCount, hasRoots,
}: Props) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [showScrollTop, setShowScrollTop] = useState(false);

  // Scroll selected row into view after each render so click-to-reveal in the
  // editor (which doesn't move the tree-pane scroll) doesn't leave the user
  // looking at a different part of the tree than the detail pane describes.
  useEffect(() => {
    if (!hostRef.current || !selectedKey) return;
    const el = hostRef.current.querySelector<HTMLElement>(`[data-key="${cssAttr(selectedKey)}"]`);
    if (el) el.scrollIntoView({ block: 'nearest' });
  }, [selectedKey, rows]);

  // Show the scroll-to-top button once the user scrolls past ~one screen.
  // Pulses to draw attention on long trees (1000-reg stress fixture) where
  // returning to the top by hand is tedious.
  const onScroll = () => {
    if (!hostRef.current) return;
    setShowScrollTop(hostRef.current.scrollTop > 200);
  };
  const scrollTop = () => {
    if (hostRef.current) hostRef.current.scrollTo({ top: 0, behavior: 'smooth' });
  };

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
        onScroll={onScroll}
      >
        <div className="rdl-tree">
          {rows.map(row => (
            <TreeRow
              key={row.key}
              row={row}
              selected={selectedKey === row.key}
              onSelectReg={onSelectReg}
              onRevealContainer={onRevealContainer}
              onToggleCollapse={onToggleCollapse}
              onContextMenu={onContextMenu}
            />
          ))}
        </div>
        {showScrollTop && (
          <button
            className="rdl-scroll-top"
            type="button"
            aria-label="Scroll to top"
            title="Scroll to top"
            onClick={scrollTop}
          >
            <svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">
              <path d="M4 10 L8 6 L12 10" />
            </svg>
          </button>
        )}
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
  onSelectReg: (row: FlatRow & { kind: 'reg' }) => void;
  onRevealContainer: (row: FlatRow & { kind: 'container' }) => void;
  onToggleCollapse: (key: string) => void;
  onContextMenu: (ev: React.MouseEvent, row: FlatRow) => void;
};

function TreeRow({
  row, selected, onSelectReg, onRevealContainer, onToggleCollapse, onContextMenu,
}: RowProps) {
  const indent = row.depth > 0 ? `rdl-indent-${Math.min(row.depth, 3)}` : '';
  const cls = ['rdl-row', indent];
  if (row.kind === 'container') cls.push('container');
  if (selected) cls.push('selected');
  const className = cls.filter(Boolean).join(' ');

  if (row.kind === 'container') {
    const node = row.node;
    const isBridge = node.kind === 'addrmap' && (node as { isBridge?: boolean }).isBridge;
    const kindLabel = node.kind + (node.type ? ` (${node.type})` : '') + (isBridge ? ' · bridge' : '');
    const rowTitle = (isBridge ? 'bridge addrmap · ' : '') +
      'Click to reveal in editor · use the chevron to collapse';
    // Click on the row body (not the caret button) reveals the container's
    // source in the host editor — same UX as register rows. Caret button
    // keeps its own click handler with stopPropagation so it only collapses,
    // never reveals.
    return (
      <div
        className={className}
        role="treeitem"
        aria-level={row.depth + 1}
        aria-expanded={row.expanded}
        data-key={row.key}
        onClick={() => onRevealContainer(row)}
        onContextMenu={(e) => onContextMenu(e, row)}
        title={rowTitle}
        style={{ cursor: 'pointer' }}
      >
        <button
          type="button"
          className="caret-toggle"
          aria-label={row.expanded ? 'Collapse' : 'Expand'}
          aria-expanded={row.expanded}
          title={row.expanded ? 'Click to collapse' : 'Click to expand'}
          onClick={(e) => { e.stopPropagation(); onToggleCollapse(row.key); }}
        >
          <svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">
            <path d={row.expanded ? 'M3 6 L8 11 L13 6' : 'M6 3 L11 8 L6 13'} />
          </svg>
        </button>
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
