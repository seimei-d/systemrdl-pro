import { useEffect, useMemo, useState } from 'react';
import type { Addrmap, Container, Reg, Regfile, SourceLoc, TreeNode } from './types';

type Props = {
  root: Addrmap;
  /** Reveal a register's source in the host editor + auto-select in the tree. */
  onRevealReg: (reg: Reg, pathSegs: string[]) => void;
};

// Minimum visible tile width — prevents tiny registers from collapsing to a
// hair-line next to a multi-MB regfile.
const MIN_TILE_PX = 72;

function isContainer(node: TreeNode): node is Container {
  return node.kind === 'addrmap' || node.kind === 'regfile';
}

function parseHex(s: string): number {
  // Strip the 0x prefix and underscores; parse as a regular int. Schema
  // guarantees the format.
  return Number.parseInt(s.replace(/^0x/i, '').replace(/_/g, ''), 16);
}

type Tile =
  | { kind: 'node'; node: Reg | Regfile | Addrmap; pathSegs: string[]; size: number }
  | { kind: 'hole'; start: number; size: number };

function buildTiles(container: Container, parentPath: string[]): Tile[] {
  // Sort children by address so the strip reads left-to-right in memory order.
  const sorted = [...(container.children || [])].sort(
    (a, b) => parseHex(a.address) - parseHex(b.address),
  );
  const tiles: Tile[] = [];
  const containerStart = parseHex(container.address);
  const containerSize = parseHex(container.size);
  let cursor = containerStart;
  for (const child of sorted) {
    const start = parseHex(child.address);
    const size = parseHex(getSize(child));
    if (start > cursor) {
      tiles.push({ kind: 'hole', start: cursor, size: start - cursor });
    }
    tiles.push({
      kind: 'node',
      node: child,
      pathSegs: [...parentPath, child.name],
      size: Math.max(1, size),
    });
    cursor = start + size;
  }
  if (cursor < containerStart + containerSize) {
    tiles.push({ kind: 'hole', start: cursor, size: containerStart + containerSize - cursor });
  }
  return tiles;
}

function getSize(node: TreeNode): string {
  // Reg has `width` in bits; convert to bytes for the size representation
  // used elsewhere. Containers carry `size` directly.
  if (node.kind === 'reg') {
    const bytes = Math.max(1, Math.ceil((node.width || 32) / 8));
    return '0x' + bytes.toString(16);
  }
  return node.size;
}

function fmtBytes(bytes: number): string {
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GiB`;
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${bytes} B`;
}

/**
 * Memory-map overview: a horizontal strip of every direct child of the
 * current container. Tiles are sized with `flex-grow: size` so larger
 * regfiles take more visual real-estate, but a `min-width` ensures tiny
 * registers never disappear next to multi-MB blocks.
 *
 * Click on a regfile/addrmap tile drills into it (breadcrumb tracks the
 * stack). Click on a reg tile reveals the source in the editor + selects
 * the matching node in the main tree below.
 */
export function OverviewMap({ root, onRevealReg }: Props) {
  const [path, setPath] = useState<Container[]>([root]);
  // Reset stack when the active tab (root) changes.
  useEffect(() => {
    setPath([root]);
  }, [root]);

  const current = path[path.length - 1];
  const segPrefix = useMemo(() => path.map(c => c.name), [path]);
  const tiles = useMemo(
    () => buildTiles(current, segPrefix),
    [current, segPrefix],
  );

  const onTileClick = (tile: Tile) => {
    if (tile.kind !== 'node') return;
    if (tile.node.kind === 'reg') {
      onRevealReg(tile.node, segPrefix.concat([tile.node.name]));
      return;
    }
    // Drill into a container.
    setPath(prev => [...prev, tile.node as Container]);
  };

  const accentClass = (tile: Tile): string => {
    if (tile.kind === 'hole') return 'rdl-overview-tile reserved';
    const node = tile.node;
    if (node.kind === 'reg') {
      const acc = (node.accessSummary || 'na').split('/')[0].toLowerCase();
      return `rdl-overview-tile reg acc-${acc}`;
    }
    return `rdl-overview-tile container ${node.kind}`;
  };

  return (
    <div className="rdl-overview" role="region" aria-label="Memory map overview">
      <div className="rdl-overview-header">
        <div className="rdl-overview-crumbs" role="navigation" aria-label="Map breadcrumb">
          {path.map((c, i) => {
            const last = i === path.length - 1;
            return (
              <span key={`${c.name}-${i}`} className="rdl-overview-crumb">
                <button
                  type="button"
                  className={'rdl-overview-crumb-btn' + (last ? ' active' : '')}
                  onClick={() => {
                    if (last) return;
                    setPath(prev => prev.slice(0, i + 1));
                  }}
                  title={`${c.kind} ${c.name} · ${c.address} · ${fmtBytes(parseHex(c.size))}`}
                >{c.name}</button>
                {!last && <span className="rdl-overview-crumb-sep">›</span>}
              </span>
            );
          })}
        </div>
        <div className="rdl-overview-meta">
          {tiles.filter(t => t.kind === 'node').length} children · {fmtBytes(parseHex(current.size))}
        </div>
      </div>
      <div className="rdl-overview-column">
        {tiles.map((tile, i) => {
          // Linear flex-grow = byte size: tiles take vertical space *proportional
          // to their address-space share* of the parent addrmap. A 4 MB regfile
          // inside a 4 MB addrmap fills the whole column; a 4 B reg is a thin
          // strip at the appropriate y-offset. Min-height (CSS) clamps the
          // smallest tiles so their text stays readable; holes have no clamp
          // so a sparse map shows its real holes at their honest size.
          const flexGrow = Math.max(1, tile.size);
          if (tile.kind === 'hole') {
            const showLabel = tile.size >= 16;  // tiny gaps render as a thin line
            return (
              <div
                key={`hole-${i}`}
                className={accentClass(tile)}
                style={{ flexGrow }}
                title={`reserved · 0x${tile.start.toString(16)} · ${fmtBytes(tile.size)}`}
              >
                {showLabel && (
                  <>
                    <span className="rdl-overview-tile-addr">0x{tile.start.toString(16)}</span>
                    <span className="rdl-overview-tile-label">— reserved —</span>
                    <span className="rdl-overview-tile-size">{fmtBytes(tile.size)}</span>
                  </>
                )}
              </div>
            );
          }
          const node = tile.node;
          const accSummary = node.kind === 'reg' ? (node.accessSummary || '') : node.kind;
          const tooltip = `${node.kind} ${node.name}\n${node.address} · ${fmtBytes(tile.size)}` +
            (node.kind === 'reg' && node.accessSummary ? `\n${node.accessSummary}` : '');
          return (
            <button
              key={`${node.name}-${i}`}
              type="button"
              className={accentClass(tile)}
              style={{ flexGrow }}
              onClick={() => onTileClick(tile)}
              title={tooltip}
            >
              <span className="rdl-overview-tile-addr">{node.address}</span>
              <span className="rdl-overview-tile-name">{node.name}</span>
              <span className="rdl-overview-tile-size">{accSummary}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
