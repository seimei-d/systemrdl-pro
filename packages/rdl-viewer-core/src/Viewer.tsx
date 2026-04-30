import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ElaboratedTree, Reg, SourceLoc, Transport, TreeNode } from './types';
import { findFirstReg, findRegByKey, isContainer, subtreeMatches, type FilterScope } from './util';
import { buildFlatList, FlatRow, Tree } from './Tree';
import { Detail } from './Detail';
import { ContextMenu, CtxMenuItem, CtxMenuState } from './ContextMenu';

type Props = { transport: Transport };

export function Viewer({ transport }: Props) {
  const [tree, setTree] = useState<ElaboratedTree | null>(null);
  const [activeRoot, setActiveRoot] = useState(0);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [focusedKey, setFocusedKey] = useState<string | null>(null);
  const [filter, setFilter] = useState('');
  const [filterScope, setFilterScope] = useState<FilterScope>('all');
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [ctxMenu, setCtxMenu] = useState<CtxMenuState>(null);
  const [toast, setToast] = useState<string | null>(null);

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
      setFocusedKey(first?.key ?? null);
    }
  }, [root, selectedKey]);

  // Keep focus visible — if focused row was hidden by collapse, drop to selection.
  useEffect(() => {
    if (focusedKey && !flatRows.some(r => r.key === focusedKey)) {
      setFocusedKey(selectedKey ?? flatRows[0]?.key ?? null);
    }
  }, [flatRows, focusedKey, selectedKey]);

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
    setFocusedKey(key);
  }, []);

  const selectReg = useCallback((row: FlatRow & { kind: 'reg' }) => {
    setSelectedKey(row.key);
    setFocusedKey(row.key);
    if (row.node.source && transport.reveal) transport.reveal(row.node.source);
  }, [transport]);

  const onKey = useCallback((e: React.KeyboardEvent) => {
    if (!flatRows.length) return;
    const idx = flatRows.findIndex(r => r.key === focusedKey);
    const cur = idx >= 0 ? flatRows[idx] : null;
    const move = (j: number) => {
      if (j < 0 || j >= flatRows.length) return;
      setFocusedKey(flatRows[j].key);
    };
    const findParent = (of: number) => {
      for (let j = of - 1; j >= 0; j--) if (flatRows[j].depth < flatRows[of].depth) return j;
      return -1;
    };
    switch (e.key) {
      case 'ArrowDown': move(idx + 1); e.preventDefault(); break;
      case 'ArrowUp':   move(idx > 0 ? idx - 1 : 0); e.preventDefault(); break;
      case 'Home':      move(0); e.preventDefault(); break;
      case 'End':       move(flatRows.length - 1); e.preventDefault(); break;
      case 'ArrowRight':
        if (cur?.kind === 'container') {
          if (!cur.expanded && cur.hasChildren) toggleCollapse(cur.key);
          else if (idx + 1 < flatRows.length && flatRows[idx + 1].depth > cur.depth) move(idx + 1);
        }
        e.preventDefault();
        break;
      case 'ArrowLeft':
        if (cur?.kind === 'container' && cur.expanded) toggleCollapse(cur.key);
        else if (cur) {
          const p = findParent(idx);
          if (p >= 0) move(p);
        }
        e.preventDefault();
        break;
      case 'Enter':
      case ' ':
        if (!cur) break;
        if (cur.kind === 'container') toggleCollapse(cur.key);
        else if (cur.kind === 'reg') selectReg(cur);
        e.preventDefault();
        break;
    }
  }, [flatRows, focusedKey, toggleCollapse, selectReg]);

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
              setFocusedKey(first?.key ?? null);
            }}
          >{r.name}</button>
        ))}
      </div>
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
            focusedKey={focusedKey}
            onSelectReg={selectReg}
            onToggleCollapse={toggleCollapse}
            onFocus={setFocusedKey}
            onContextMenu={onContextMenu}
            onKey={onKey}
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
