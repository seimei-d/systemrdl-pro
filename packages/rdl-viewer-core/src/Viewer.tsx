import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ElaboratedTree, Reg, SourceLoc, Transport, TreeNode } from './types';
import { findFirstReg, findRegByKey, isContainer, subtreeMatches, type FilterScope } from './util';
import { buildFlatList, FlatRow, Tree } from './Tree';
import { Detail } from './Detail';
import { ContextMenu, CtxMenuItem, CtxMenuState } from './ContextMenu';
import { OverviewMap } from './OverviewMap';

type Props = { transport: Transport };

export function Viewer({ transport }: Props) {
  const [tree, setTree] = useState<ElaboratedTree | null>(null);
  const [activeRoot, setActiveRoot] = useState(0);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [filter, setFilter] = useState('');
  const [filterScope, setFilterScope] = useState<FilterScope>('all');
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [ctxMenu, setCtxMenu] = useState<CtxMenuState>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [overviewOpen, setOverviewOpen] = useState(true);

  // Initial fetch + live updates.
  useEffect(() => {
    let mounted = true;
    transport.getTree().then(t => { if (mounted) setTree(t); }).catch(() => {});
    const off = transport.onTreeUpdate(t => { if (mounted) setTree(t); });
    return () => { mounted = false; off(); };
  }, [transport]);

  // Optional editor cursor sync.
  useEffect(() => {
    if (!transport.onCursorMove) return;
    return transport.onCursorMove(line0b => {
      if (!tree) return;
      const result = locateByCursorLine(tree.roots, line0b);
      if (!result) return;
      if (result.kind === 'tab' && activeRoot !== result.index) {
        setActiveRoot(result.index);
        const first = findFirstReg(tree.roots[result.index], [tree.roots[result.index].name]);
        setSelectedKey(first?.key ?? null);
      } else if (result.kind === 'reg') {
        setSelectedKey(result.key);
      }
    });
  }, [tree, activeRoot, transport]);

  const roots = tree?.roots ?? [];
  const root = roots[activeRoot];

  // Compute flat list once per render — drives Tree and keyboard handler.
  const flatRows = useMemo<FlatRow[]>(
    () => (root ? buildFlatList(root, filter, filterScope, collapsed) : []),
    [root, filter, filterScope, collapsed],
  );

  // Auto-select first reg when root changes or selection becomes invalid.
  useEffect(() => {
    if (!root) return;
    const stillValid = selectedKey && findRegByKey(root, selectedKey);
    if (!stillValid) {
      const first = findFirstReg(root, [root.name]);
      setSelectedKey(first?.key ?? null);
    }
  }, [root, selectedKey]);

  const filterMatchCount = useMemo(
    () => flatRows.filter(r => r.kind === 'reg').length,
    [flatRows],
  );

  const toggleCollapse = useCallback((key: string) => {
    setCollapsed(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  }, []);

  const selectReg = useCallback((row: FlatRow & { kind: 'reg' }) => {
    setSelectedKey(row.key);
    if (row.node.source && transport.reveal) transport.reveal(row.node.source);
  }, [transport]);

  const onOverviewReveal = useCallback((reg: Reg, segs: string[]) => {
    // Selecting by `segs.join('.')` matches the key format buildFlatList /
    // findRegByKey use in the tree, so the reg auto-highlights in the
    // tree below as soon as the user clicks an overview tile.
    setSelectedKey(segs.join('.'));
    if (reg.source && transport.reveal) transport.reveal(reg.source);
  }, [transport]);

  // Cmd/Ctrl-F to focus filter (the input is already in the page; we just focus it).
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === 'f' || e.key === 'F')) {
        const input = document.getElementById('rdl-filter-input') as HTMLInputElement | null;
        if (input) { input.focus(); input.select(); e.preventDefault(); }
      } else if (e.key === 'Escape') {
        const input = document.getElementById('rdl-filter-input') as HTMLInputElement | null;
        if (input && (document.activeElement === input || filter)) {
          setFilter('');
          input.value = '';
          input.blur();
          e.preventDefault();
        }
      }
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [filter]);

  // Toast auto-dismiss.
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 1800);
    return () => clearTimeout(t);
  }, [toast]);

  const onCopy = useCallback((text: string, label: string) => {
    if (transport.copy) {
      transport.copy(text, label);
    } else if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(
        () => setToast(`Copied ${label}: ${text}`),
        () => setToast('Copy failed — check browser permissions'),
      );
    } else {
      setToast('Clipboard API unavailable');
    }
  }, [transport]);

  const onContextMenu = useCallback((ev: React.MouseEvent, row: FlatRow) => {
    ev.preventDefault();
    const path = row.pathSegs.join('.');
    const node = row.node as { address?: string; type?: string; source?: SourceLoc };
    const items: CtxMenuItem[] = [
      { label: 'Copy Name', hint: path, action: () => onCopy(path, 'name') },
      { label: 'Copy Address', hint: node.address ?? '', action: () => onCopy(node.address ?? '', 'address') },
    ];
    if (node.type) items.push({ label: 'Copy Type', hint: node.type, action: () => onCopy(node.type ?? '', 'type') });
    if (node.source && transport.reveal) {
      items.push({ sep: true });
      items.push({ label: 'Reveal in Editor', hint: '', action: () => transport.reveal!(node.source!) });
    } else if (node.source) {
      items.push({ sep: true });
      const fileName = (node.source.uri || '').split('/').pop() || node.source.uri;
      const ref = `${fileName}:${(node.source.line || 0) + 1}`;
      items.push({ label: 'Copy Source Path', hint: ref, action: () => onCopy(ref, 'source') });
    }
    setCtxMenu({ x: ev.clientX, y: ev.clientY, items });
  }, [onCopy, transport]);

  const found = root && selectedKey ? findRegByKey(root, selectedKey) : null;

  if (!tree) {
    return (
      <div className="rdl-viewer">
        <div className="rdl-empty"><p>Loading…</p></div>
      </div>
    );
  }

  return (
    <div className="rdl-viewer">
      {tree.stale && (
        <div className="rdl-stale-bar">
          <span>⚠</span>
          <span>Showing last good elaboration · current parse failed</span>
        </div>
      )}
      <div className="rdl-tabs" role="tablist">
        {roots.map((r, i) => (
          <button
            key={`${r.name}-${i}`}
            className={'rdl-tab' + (i === activeRoot ? ' active' : '')}
            role="tab"
            aria-selected={i === activeRoot}
            title={(r.type ? r.type + ' · ' : '') + r.address}
            onClick={() => {
              if (i === activeRoot) return;
              setActiveRoot(i);
              const first = findFirstReg(roots[i], [roots[i].name]);
              setSelectedKey(first?.key ?? null);
            }}
          >{r.name}</button>
        ))}
        <button
          className="rdl-overview-toggle"
          type="button"
          aria-pressed={overviewOpen}
          title={overviewOpen ? 'Hide map overview' : 'Show map overview'}
          onClick={() => setOverviewOpen(o => !o)}
        >{overviewOpen ? 'Hide map' : 'Show map'}</button>
      </div>
      {overviewOpen && root && (
        <OverviewMap root={root} onRevealReg={onOverviewReveal} />
      )}
      <div className="rdl-body">
        <div className="rdl-tree-pane">
          <div className="rdl-filter-bar">
            <div className="rdl-filter-row">
              <select
                className="rdl-filter-scope"
                value={filterScope}
                onChange={e => setFilterScope(e.target.value as FilterScope)}
                title="Limit filter to this column"
                aria-label="Filter scope"
              >
                <option value="all">All</option>
                <option value="name">Name</option>
                <option value="address">Address</option>
                <option value="field">Field</option>
              </select>
              <input
                id="rdl-filter-input"
                type="text"
                placeholder={filterScopePlaceholder(filterScope)}
                onChange={e => setFilter(e.target.value.toLowerCase())}
              />
            </div>
          </div>
          <Tree
            rows={flatRows}
            selectedKey={selectedKey}
            onSelectReg={selectReg}
            onToggleCollapse={toggleCollapse}
            onContextMenu={onContextMenu}
            filter={filter}
            filterMatchCount={filterMatchCount}
            hasRoots={roots.length > 0}
          />
        </div>
        <Detail
          reg={found?.reg ?? null}
          path={found?.path ?? null}
          transport={transport}
        />
      </div>
      <ContextMenu state={ctxMenu} onClose={() => setCtxMenu(null)} />
      {toast && <div className="rdl-toast shown" role="status" aria-live="polite">{toast}</div>}
    </div>
  );
}

function filterScopePlaceholder(scope: FilterScope): string {
  switch (scope) {
    case 'name':    return 'Filter by register or container name…';
    case 'address': return 'Filter by address (e.g. 0x10, 0010)…';
    case 'field':   return 'Filter by field name…';
    default:        return 'Filter (matches name, address, or field name)…';
  }
}

/**
 * Walk roots looking for a node whose source line matches `line0b`. Used by
 * the editor → viewer cursor sync (Decision D10). Returns either:
 *   - { kind: 'tab', index } — top-level addrmap → switch tabs
 *   - { kind: 'reg', key } — matched a reg or one of its fields → select it
 */
function locateByCursorLine(roots: TreeNode[], line0b: number):
  | { kind: 'tab'; index: number }
  | { kind: 'reg'; key: string }
  | null {
  for (let i = 0; i < roots.length; i++) {
    const r = roots[i];
    if (r.source?.line === line0b) return { kind: 'tab', index: i };
  }
  for (let i = 0; i < roots.length; i++) {
    const found = walk(roots[i], [roots[i].name]);
    if (found) return { kind: 'reg', key: found };
  }
  return null;
  function walk(node: TreeNode, segs: string[]): string | null {
    if (node.kind === 'reg') {
      for (const f of node.fields || []) {
        if (f.source?.line === line0b) return segs.join('.');
      }
      if (node.source?.line === line0b) return segs.join('.');
      return null;
    }
    if (node.source?.line === line0b) return null; // container line — let outer logic handle
    for (const c of node.children || []) {
      const r = walk(c, segs.concat([c.name]));
      if (r) return r;
    }
    return null;
  }
}
