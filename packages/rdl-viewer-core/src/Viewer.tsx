import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ElaboratedTree, Mem, Reg, SourceLoc, Transport, TreeNode } from './types';
import { findFirstReg, isContainer, subtreeMatches, type FilterScope } from './util';
import { buildFlatList, FlatRow, Tree } from './Tree';
import { Detail } from './Detail';
import { ContextMenu, CtxMenuItem, CtxMenuState } from './ContextMenu';

type Props = { transport: Transport };

type Selection =
  | { kind: 'reg'; key: string }
  | { kind: 'mem'; key: string }
  | null;

export function Viewer({ transport }: Props) {
  const [tree, setTree] = useState<ElaboratedTree | null>(null);
  const [activeRoot, setActiveRoot] = useState(0);
  const [selection, setSelection] = useState<Selection>(null);
  const [filter, setFilter] = useState('');
  const filterTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [filterScope, setFilterScope] = useState<FilterScope>('all');
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [ctxMenu, setCtxMenu] = useState<CtxMenuState>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [isElaborating, setIsElaborating] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [retryCounter, setRetryCounter] = useState(0);

  useEffect(() => {
    let mounted = true;
    setLoadError(null);
    transport.getTree()
      .then(t => { if (mounted) setTree(t); })
      .catch(err => {
        if (!mounted) return;
        const msg = err instanceof Error ? err.message : String(err);
        setLoadError(msg || 'unknown error');
      });
    const off = transport.onTreeUpdate(t => {
      if (!mounted) return;
      setTree(t);
      setLoadError(null);
    });
    return () => { mounted = false; off(); };
  }, [transport, retryCounter]);

  useEffect(() => {
    if (!transport.onElaborating) return;
    return transport.onElaborating(setIsElaborating);
  }, [transport]);

  const roots = tree?.roots ?? [];
  const root = roots[activeRoot];

  const lineIndex = useMemo(() => {
    const m = new Map<number, { kind: 'tab'; index: number } | { kind: 'reg'; key: string }>();
    if (!tree) return m;
    const rs = tree.roots ?? [];
    for (let i = 0; i < rs.length; i++) {
      const ln = rs[i].source?.line;
      if (ln !== undefined && !m.has(ln)) m.set(ln, { kind: 'tab', index: i });
    }
    function walk(node: TreeNode, segs: string[]): void {
      if (node.kind === 'reg') {
        const key = segs.join('.');
        for (const f of node.fields || []) {
          const fl = f.source?.line;
          if (fl !== undefined && !m.has(fl)) m.set(fl, { kind: 'reg', key });
        }
        const rl = node.source?.line;
        if (rl !== undefined && !m.has(rl)) m.set(rl, { kind: 'reg', key });
        return;
      }
      if (node.kind === 'mem') {
        const ml = node.source?.line;
        if (ml !== undefined && !m.has(ml)) m.set(ml, { kind: 'reg', key: segs.join('.') });
        return;
      }
      for (const c of node.children || []) walk(c, segs.concat([c.name]));
    }
    for (const r of rs) walk(r, [r.name]);
    return m;
  }, [tree]);

  useEffect(() => {
    if (!transport.onCursorMove) return;
    return transport.onCursorMove(line0b => {
      if (!tree) return;
      const result = lineIndex.get(line0b);
      if (!result) return;
      if (result.kind === 'tab' && activeRoot !== result.index) {
        setActiveRoot(result.index);
        setSelection(null);
        const first = findFirstReg(tree.roots[result.index], [tree.roots[result.index].name]);
        if (first) setSelection({ kind: 'reg', key: first.key });
      } else if (result.kind === 'reg') {
        setSelection({ kind: 'reg', key: result.key });
      }
    });
  }, [tree, activeRoot, transport, lineIndex]);

  const flatRows = useMemo<FlatRow[]>(
    () => (root ? buildFlatList(root, filter, filterScope, collapsed) : []),
    [root, filter, filterScope, collapsed],
  );

  // key → {reg, path} index
  const regIndex = useMemo(() => {
    const m = new Map<string, { reg: Reg; path: string[] }>();
    if (!root) return m;
    function walk(node: TreeNode, segs: string[]): void {
      if (node.kind === 'reg') {
        m.set(segs.join('.'), { reg: node, path: segs });
        return;
      }
      if (node.kind === 'mem') return;
      const kids = node.children;
      if (!kids) return;
      for (const c of kids) walk(c, segs.concat([c.name]));
    }
    walk(root, [root.name]);
    return m;
  }, [root]);

  // key → {mem, path} index
  const memIndex = useMemo(() => {
    const m = new Map<string, { mem: Mem; path: string[] }>();
    if (!root) return m;
    function walk(node: TreeNode, segs: string[]): void {
      if (node.kind === 'reg') return;
      if (node.kind === 'mem') {
        m.set(segs.join('.'), { mem: node, path: segs });
        return;
      }
      const kids = node.children;
      if (!kids) return;
      for (const c of kids) walk(c, segs.concat([c.name]));
    }
    walk(root, [root.name]);
    return m;
  }, [root]);

  // Auto-select first reg when root changes or selection becomes invalid.
  useEffect(() => {
    if (!root) return;
    if (selection) {
      const stillValid = selection.kind === 'reg'
        ? regIndex.has(selection.key)
        : memIndex.has(selection.key);
      if (!stillValid) {
        setSelection(null);
      }
    }
    if (!selection) {
      const first = findFirstReg(root, [root.name]);
      if (first) setSelection({ kind: 'reg', key: first.key });
    }
  }, [root, selection, regIndex, memIndex]);

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

  const collapseAll = useCallback(() => {
    if (!root) return;
    const keys = new Set<string>();
    const walk = (node: TreeNode, segs: string[]): void => {
      if (isContainer(node)) {
        keys.add(segs.join('.'));
        (node.children ?? []).forEach(c => walk(c, [...segs, c.name]));
      }
    };
    walk(root, [root.name]);
    setCollapsed(keys);
  }, [root]);

  const expandAll = useCallback(() => {
    setCollapsed(new Set());
  }, []);

  const selectReg = useCallback((row: FlatRow & { kind: 'reg' }) => {
    setSelection({ kind: 'reg', key: row.key });
    if (row.node.source && transport.reveal) transport.reveal(row.node.source);
  }, [transport]);

  const revealContainer = useCallback((row: FlatRow & { kind: 'container' }) => {
    if (!transport.reveal) return;
    const source = row.node.source;
    if (!source) {
      setToast(`No source location for ${row.node.kind} ${row.node.name}`);
      return;
    }
    transport.reveal(source);
  }, [transport]);

  const selectMem = useCallback((row: FlatRow & { kind: 'mem' }) => {
    setSelection({ kind: 'mem', key: row.key });
    if (row.node.source && transport.reveal) transport.reveal(row.node.source);
  }, [transport]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === 'f' || e.key === 'F')) {
        const input = document.getElementById('rdl-filter-input') as HTMLInputElement | null;
        if (input) { input.focus(); input.select(); e.preventDefault(); }
      } else if (e.key === 'Escape') {
        const input = document.getElementById('rdl-filter-input') as HTMLInputElement | null;
        if (input && (document.activeElement === input || filter)) {
          if (filterTimer.current) clearTimeout(filterTimer.current);
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
      items.push({
        label: 'Reveal in Editor',
        hint: '',
        action: () => transport.reveal!(node.source!, true),
      });
    } else if (node.source) {
      items.push({ sep: true });
      const fileName = (node.source.uri || '').split('/').pop() || node.source.uri;
      const ref = `${fileName}:${(node.source.line || 0) + 1}`;
      items.push({ label: 'Copy Source Path', hint: ref, action: () => onCopy(ref, 'source') });
    }
    setCtxMenu({ x: ev.clientX, y: ev.clientY, items });
  }, [onCopy, transport]);

  // Populated regs from `transport.expandNode`
  const [expandedRegs, setExpandedRegs] = useState<Map<string, Reg>>(() => new Map());

  const foundReg = useMemo(() => {
    if (!selection || selection.kind !== 'reg') return null;
    const base = regIndex.get(selection.key) ?? null;
    if (!base) return null;
    const v = tree?.version ?? 0;
    const nid = base.reg.nodeId;
    if (nid && base.reg.loadState === 'placeholder') {
      const populated = expandedRegs.get(`${v}:${nid}`);
      if (populated) {
        return { reg: { ...populated, loadState: 'loaded' as const }, path: base.path };
      }
    }
    return base;
  }, [regIndex, selection, expandedRegs, tree?.version]);

  const foundMem = useMemo(() => {
    if (!selection || selection.kind !== 'mem') return null;
    return memIndex.get(selection.key) ?? null;
  }, [memIndex, selection]);

  const pendingExpansions = useRef<Set<string>>(new Set()).current;
  useEffect(() => {
    if (!foundReg || !tree || !transport.expandNode) return;
    const reg = foundReg.reg;
    if (reg.loadState !== 'placeholder' || !reg.nodeId) return;
    const nodeId = reg.nodeId;
    const version = tree.version ?? 0;
    const trackingKey = `${version}:${nodeId}`;
    if (pendingExpansions.has(trackingKey)) return;
    if (expandedRegs.has(trackingKey)) return;
    pendingExpansions.add(trackingKey);
    transport.expandNode(version, nodeId)
      .then(populated => {
        pendingExpansions.delete(trackingKey);
        setExpandedRegs(prev => {
          const next = new Map<string, Reg>();
          const prefix = `${version}:`;
          for (const [k, v] of prev) {
            if (k.startsWith(prefix)) next.set(k, v);
          }
          next.set(trackingKey, populated);
          return next;
        });
      })
      .catch(() => {
        pendingExpansions.delete(trackingKey);
        setExpandedRegs(prev => new Map(prev));
      });
  }, [foundReg, tree, transport, pendingExpansions, expandedRegs]);

  const activeSource = foundReg?.reg.source ?? foundMem?.mem.source ?? null;
  const stillElaboratingFirstPass =
    !tree || (tree.version === 0 && (tree.roots ?? []).length === 0);
  if (loadError && !tree) {
    return (
      <div className="rdl-viewer">
        <div className="rdl-empty rdl-load-error" role="alert">
          <p><strong>⚠ Could not load elaborated tree</strong></p>
          <p style={{ opacity: 0.85, fontSize: '0.9em' }}>{loadError}</p>
          <button
            type="button"
            className="rdl-retry-btn"
            onClick={() => { setLoadError(null); setRetryCounter(n => n + 1); }}
          >
            Retry
          </button>
        </div>
      </div>
    );
  }
  if (stillElaboratingFirstPass) {
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
          <span>Errors detected — see Problems panel · viewer may show last good or pre-error state</span>
        </div>
      )}
      {isElaborating && (
        <div className="rdl-elaborating-bar" role="status" aria-live="polite">
          <span className="rdl-elaborating-spinner" aria-hidden="true" />
          <span>Re-elaborating in background — current tree stays interactive</span>
        </div>
      )}
      <div className="rdl-tabs" role="tablist">
        {roots.map((r, i) => (
          <button
            key={`${r.name}-${i}`}
            className={'rdl-tab' + (i === activeRoot ? ' active' : '')}
            role="tab"
            aria-selected={i === activeRoot}
            title={[
              r.type,
              r.address,
              r.isBridge ? 'bridge' : null,
              'click to reveal in editor',
            ].filter(Boolean).join(' · ')}
            onClick={() => {
              if (i !== activeRoot) {
                setActiveRoot(i);
                setSelection(null);
                const first = findFirstReg(roots[i], [roots[i].name]);
                if (first) setSelection({ kind: 'reg', key: first.key });
              }
              if (r.source && transport.reveal) transport.reveal(r.source);
            }}
          >
            {r.name}
            {r.isBridge && (
              <span className="rdl-tag bridge" title="Bridge addrmap (clause 9.2)">bridge</span>
            )}
          </button>
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
                onChange={e => {
                  const v = e.target.value.toLowerCase();
                  if (filterTimer.current) clearTimeout(filterTimer.current);
                  filterTimer.current = setTimeout(() => setFilter(v), 150);
                }}
              />
              <button
                type="button"
                className="rdl-tree-action"
                title="Collapse all containers"
                onClick={collapseAll}
              >▸</button>
              <button
                type="button"
                className="rdl-tree-action"
                title="Expand all containers"
                onClick={expandAll}
              >▾</button>
            </div>
          </div>
          <Tree
            rows={flatRows}
            selectedKey={selection?.key ?? null}
            onSelectReg={selectReg}
            onRevealContainer={revealContainer}
            onRevealMem={selectMem}
            onToggleCollapse={toggleCollapse}
            onContextMenu={onContextMenu}
            filter={filter}
            filterMatchCount={filterMatchCount}
            hasRoots={roots.length > 0}
          />
        </div>
        <Detail
          reg={foundReg?.reg ?? null}
          mem={foundMem?.mem ?? null}
          path={foundReg?.path ?? foundMem?.path ?? null}
          transport={transport}
        />
      </div>
      {activeSource && transport.reveal && (
        <div className="rdl-viewer-src-link" onClick={() => transport.reveal!(activeSource)}>
          → {((activeSource.uri || '').split('/').pop() || activeSource.uri)}:
          {(activeSource.line || 0) + 1}
        </div>
      )}
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
