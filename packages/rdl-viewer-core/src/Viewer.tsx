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
  const [filter, setFilter] = useState('');
  const [filterScope, setFilterScope] = useState<FilterScope>('all');
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [ctxMenu, setCtxMenu] = useState<CtxMenuState>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [isElaborating, setIsElaborating] = useState(false);

  // Initial fetch + live updates.
  useEffect(() => {
    let mounted = true;
    transport.getTree().then(t => { if (mounted) setTree(t); }).catch(() => {});
    const off = transport.onTreeUpdate(t => { if (mounted) setTree(t); });
    return () => { mounted = false; off(); };
  }, [transport]);

  // Re-elaborate indicator. The host signals start/finish of a full pass; the
  // banner stays up while we wait so the user knows a fresh tree is on the
  // way (the existing tree remains interactive — only visual feedback).
  useEffect(() => {
    if (!transport.onElaborating) return;
    return transport.onElaborating(setIsElaborating);
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

  const collapseAll = useCallback(() => {
    if (!root) return;
    // Walk every container and collect its `pathSegs.join('.')` key.
    // Same key format buildFlatList uses.
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
    setSelectedKey(row.key);
    if (row.node.source && transport.reveal) transport.reveal(row.node.source);
  }, [transport]);

  const revealContainer = useCallback((row: FlatRow & { kind: 'container' }) => {
    // Containers (addrmap, regfile) — clicking the row body reveals the
    // declaration in the editor. We don't change the register selection
    // so the Detail panel keeps showing whatever reg was last picked.
    if (!transport.reveal) return;
    const source = row.node.source;
    if (!source) {
      // No source means the elaborated node has no usable src_ref. Surface
      // it via the toast so the user understands why nothing happened
      // rather than silently dropping the click.
      setToast(`No source location for ${row.node.kind} ${row.node.name}`);
      return;
    }
    transport.reveal(source);
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

  // T1.7: lazy expansion of placeholder regs. When the user selects a reg
  // whose `loadState === 'placeholder'`, fire `transport.expandNode` and
  // splice the populated reg into the tree state so Detail re-renders with
  // real fields. Per-nodeId in-flight tracking avoids stampedes when the
  // user rapidly clicks through siblings.
  const [pendingExpansions] = useState<Set<string>>(() => new Set());
  useEffect(() => {
    if (!found || !tree || !transport.expandNode) return;
    const reg = found.reg;
    if (reg.loadState !== 'placeholder' || !reg.nodeId) return;
    const nodeId = reg.nodeId;
    const version = tree.version ?? 0;
    // Key by `version:nodeId` (not raw nodeId): the same nodeId in a fresh
    // elaboration is a *new* request, even though the string matches. With
    // raw-key tracking, an in-flight expand from version v1 would block the
    // version-v2 retry until the v1 request resolves as `outdated`, leaving
    // the spinner up unnecessarily for the round-trip duration.
    const trackingKey = `${version}:${nodeId}`;
    if (pendingExpansions.has(trackingKey)) return;
    pendingExpansions.add(trackingKey);
    transport.expandNode(version, nodeId)
      .then(populated => {
        // Functional setTree so we always splice into the *current* tree,
        // not the closure-captured one. If `onTreeUpdate` replaced the tree
        // while expand was in flight, we splice into the new tree (the
        // matching placeholder is presumably still there because the new
        // elaboration would have produced the same shape with the same
        // nodeId — same DFS order). Falls through gracefully if not.
        pendingExpansions.delete(trackingKey);
        setTree(currentTree => {
          if (!currentTree) return currentTree;
          // Build a new top-level wrapper so React notices the change.
          // Mutate nested nodes in place — they're not part of React state
          // identity, only the outer envelope is.
          spliceExpandedReg(currentTree, nodeId, populated);
          return { ...currentTree };
        });
      })
      .catch(() => {
        // Swallow errors — placeholder stays visible. The common case here
        // is a soft `outdated` from the server (race against a fresh
        // elaboration). The new tree from onTreeUpdate has already arrived
        // or is in flight, but we still need to nudge React so useEffect
        // re-evaluates against the latest tree.version with the placeholder
        // again — otherwise we'd be stuck on the spinner.
        pendingExpansions.delete(trackingKey);
        setTree(currentTree => (currentTree ? { ...currentTree } : currentTree));
      });
  }, [found, tree, transport, pendingExpansions]);

  // Empty + version=0 = LSP responded before initial elaborate finished (server
  // returns a stub envelope when its cache is still empty). Don't paint the
  // "no top-level addrmap" pane in that window — it's misleading and resolves
  // automatically once `rdl/elaboratedTreeChanged` triggers a refresh. After
  // version >= 1 the empty-roots state actually means "library file with no
  // addrmap" and we let the existing message render.
  const stillElaboratingFirstPass =
    !tree || (tree.version === 0 && (tree.roots ?? []).length === 0);
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
          <span>Showing last good elaboration · current parse failed</span>
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
              // Switch active tab if different.
              if (i !== activeRoot) {
                setActiveRoot(i);
                const first = findFirstReg(roots[i], [roots[i].name]);
                setSelectedKey(first?.key ?? null);
              }
              // Always reveal the addrmap declaration in the host editor —
              // tabs are the primary surface for jumping to a top-level addrmap.
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
                onChange={e => setFilter(e.target.value.toLowerCase())}
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
            selectedKey={selectedKey}
            onSelectReg={selectReg}
            onRevealContainer={revealContainer}
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
 * T1.7: walk a tree and replace the placeholder Reg with the populated one
 * returned by `transport.expandNode`. Replaces the reference inside the
 * parent's `children` array (rather than `Object.assign`-ing in place) so
 * downstream `useMemo`s keyed on the reg reference recompute. Mutating in
 * place was leaving the Decoder panel showing stale results after a register
 * switch, because Detail's `decoded = useMemo(() => decode(reg, …), [reg])`
 * was bailing out on a referentially-equal reg whose fields had silently
 * grown under it.
 */
function spliceExpandedReg(tree: ElaboratedTree, nodeId: string, populated: Reg): void {
  type Walkable = TreeNode & { children?: TreeNode[] };
  const stack: Walkable[] = [...((tree.roots ?? []) as Walkable[])];
  while (stack.length > 0) {
    const node = stack.pop()!;
    const kids = ('children' in node && Array.isArray(node.children))
      ? node.children
      : null;
    if (kids) {
      for (let i = 0; i < kids.length; i++) {
        const child = kids[i] as TreeNode;
        if (
          child.kind === 'reg'
          && child.nodeId === nodeId
          && child.loadState === 'placeholder'
        ) {
          // Server omits loadState on expand responses — clear the placeholder
          // marker explicitly so the next render path treats this as loaded.
          kids[i] = { ...populated, loadState: 'loaded' } as TreeNode;
          return;
        }
      }
      stack.push(...(kids as Walkable[]));
    }
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
