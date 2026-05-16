import { memo, useEffect, useMemo, useRef, useState } from 'react';
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

// Approximate fixed row height — tunable. Keep in sync with .rdl-row CSS.
// Slight over-estimate is safe (more spacer, no rendered rows missing); under-
// estimate would clip rows at viewport edges.
const ROW_HEIGHT_PX = 22;
// Number of off-screen rows to render above/below the viewport so scrolling
// doesn't flash blanks. ~one screen of buffer at typical heights.
const OVERSCAN_ROWS = 30;

export function Tree({
  rows, selectedKey, onSelectReg, onRevealContainer, onToggleCollapse, onContextMenu,
  filter, filterMatchCount, hasRoots,
}: Props) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [showScrollTop, setShowScrollTop] = useState(false);
  const [scrollTop, setScrollTopState] = useState(0);
  const [viewportH, setViewportH] = useState(0);

  // Track viewport height. ResizeObserver keeps the window in sync with
  // pane resizes; initial measure populates the first render.
  useEffect(() => {
    const el = hostRef.current;
    if (!el) return;
    const measure = () => setViewportH(el.clientHeight);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Compute the visible row window. Without virtualization, mapping 25k
  // <TreeRow/> elements on every Viewer re-render (selection click,
  // expand response, etc.) cost ~50–100ms of pure-JS reconciliation
  // even though TreeRow itself is memoized. Windowed render keeps the
  // re-render cost bounded by viewport size, not tree size.
  const totalH = rows.length * ROW_HEIGHT_PX;
  const startIdx = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT_PX) - OVERSCAN_ROWS);
  const visibleCount = Math.ceil((viewportH || 600) / ROW_HEIGHT_PX) + 2 * OVERSCAN_ROWS;
  const endIdx = Math.min(rows.length, startIdx + visibleCount);
  const visibleRows = rows.slice(startIdx, endIdx);
  const offsetY = startIdx * ROW_HEIGHT_PX;

  // Selection scroll-into-view: if the selected row is outside the
  // current window, jump scrollTop so it lands in the middle. We do
  // this imperatively rather than relying on the row element existing
  // (it may not, since virtualization elides it).
  useEffect(() => {
    if (!hostRef.current || !selectedKey) return;
    const idx = rows.findIndex(r => r.key === selectedKey);
    if (idx < 0) return;
    const rowTop = idx * ROW_HEIGHT_PX;
    const rowBottom = rowTop + ROW_HEIGHT_PX;
    const host = hostRef.current;
    if (rowTop < host.scrollTop) {
      host.scrollTop = rowTop;
    } else if (rowBottom > host.scrollTop + host.clientHeight) {
      host.scrollTop = rowBottom - host.clientHeight;
    }
  }, [selectedKey, rows]);

  const onScroll = () => {
    const el = hostRef.current;
    if (!el) return;
    setScrollTopState(el.scrollTop);
    setShowScrollTop(el.scrollTop > 200);
  };
  const doScrollTop = () => {
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
        <div className="rdl-tree" style={{ height: `${totalH}px`, position: 'relative' }}>
          <div style={{ position: 'absolute', top: 0, left: 0, right: 0, transform: `translateY(${offsetY}px)` }}>
            {visibleRows.map((row, i) => (
              // Key is the absolute index into `rows`, not row.key. row.key is
              // the logical path (`top.my_reg`) and is *supposed* to be unique
              // per flat-list entry, but if any upstream slip lets two entries
              // share it (the `reg[N]` array unroll did, before the
              // serializer started emitting `name[i]`), React dedups TreeRows
              // by key — reconciler reuses one DOM node for the whole array,
              // memo bails on each scroll tick because the key matches, and
              // the viewport visually freezes on a single register while the
              // scrollbar still rides the full `totalH` height. Indexing by
              // position keeps reconciliation correct under any duplicate-key
              // bug we haven't found yet.
              <TreeRow
                key={startIdx + i}
                row={row}
                selected={selectedKey === row.key}
                onSelectReg={onSelectReg}
                onRevealContainer={onRevealContainer}
                onToggleCollapse={onToggleCollapse}
                onContextMenu={onContextMenu}
              />
            ))}
          </div>
        </div>
        {showScrollTop && (
          <button
            className="rdl-scroll-top"
            type="button"
            aria-label="Scroll to top"
            title="Scroll to top"
            onClick={doScrollTop}
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


type RowProps = {
  row: FlatRow;
  selected: boolean;
  onSelectReg: (row: FlatRow & { kind: 'reg' }) => void;
  onRevealContainer: (row: FlatRow & { kind: 'container' }) => void;
  onToggleCollapse: (key: string) => void;
  onContextMenu: (ev: React.MouseEvent, row: FlatRow) => void;
};

// Memoised so a selection change re-renders only the two rows whose
// `selected` flipped, not all 500-25k visible rows. The four callback
// props are useCallback-stable in Viewer so memo bailout is effective.
const TreeRow = memo(function TreeRow({
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
});
