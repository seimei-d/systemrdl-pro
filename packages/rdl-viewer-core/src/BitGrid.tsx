import type { Field, Reg } from './types';

type Cell =
  | { kind: 'field'; field: Field; msb: number; lsb: number }
  | { kind: 'reserved'; msb: number; lsb: number };

type Row = {
  msb: number;  // inclusive top bit of this row
  lsb: number;  // inclusive bottom bit
  cells: Cell[];
};

type Props = { reg: Reg };

const BITS_PER_ROW = 16;

/**
 * Split a register's bit space into 16-bit-wide rows, datasheet style:
 *
 * - 1..16 bits  → one row
 * - 17..32 bits → two rows (high half on top)
 * - 33..64 bits → up to four rows
 *
 * Each row carries its own bit-index header and a list of cells covering the
 * row's range. Fields that cross a 16-bit boundary are split into per-row
 * segments — both halves render with the same field name + access colour.
 */
function buildRows(width: number, fields: Field[]): Row[] {
  const rows: Row[] = [];
  const numRows = Math.max(1, Math.ceil(width / BITS_PER_ROW));
  for (let r = 0; r < numRows; r++) {
    const rowHigh = width - 1 - r * BITS_PER_ROW;
    const rowLow = Math.max(0, width - (r + 1) * BITS_PER_ROW);
    // Fields that intersect this row's range, clipped to [rowLow..rowHigh].
    const intersecting = fields
      .filter(f => f.msb >= rowLow && f.lsb <= rowHigh)
      .map(f => ({
        field: f,
        msb: Math.min(f.msb, rowHigh),
        lsb: Math.max(f.lsb, rowLow),
      }))
      .sort((a, b) => b.msb - a.msb);

    const cells: Cell[] = [];
    let cursor = rowHigh;
    for (const rf of intersecting) {
      if (cursor > rf.msb) {
        cells.push({ kind: 'reserved', msb: cursor, lsb: rf.msb + 1 });
      }
      cells.push({ kind: 'field', field: rf.field, msb: rf.msb, lsb: rf.lsb });
      cursor = rf.lsb - 1;
    }
    if (cursor >= rowLow) {
      cells.push({ kind: 'reserved', msb: cursor, lsb: rowLow });
    }
    rows.push({ msb: rowHigh, lsb: rowLow, cells });
  }
  return rows;
}

/**
 * Visual bit grid. Multi-line for wide registers (one line per 16 bits).
 * Field cells span their bit range via `grid-column: span N`; field names
 * wrap across multiple text lines so long identifiers don't truncate to "f…".
 */
export function BitGrid({ reg }: Props) {
  const width = reg.width || 32;
  const fields = reg.fields || [];
  const rows = buildRows(width, fields);

  return (
    <div className="rdl-bitgrid" role="img" aria-label={`${width}-bit register layout`}>
      {rows.map((row, ri) => {
        const span = row.msb - row.lsb + 1;
        return (
          <div key={ri} className="rdl-bitgrid-row">
            <div
              className="rdl-bitgrid-bits"
              style={{ gridTemplateColumns: `repeat(${span}, minmax(0, 1fr))` }}
            >
              {Array.from({ length: span }, (_, i) => row.msb - i).map(b => (
                <div key={b} className="rdl-bitgrid-bit">{b}</div>
              ))}
            </div>
            <div
              className="rdl-bitgrid-fields"
              style={{ gridTemplateColumns: `repeat(${span}, minmax(0, 1fr))` }}
            >
              {row.cells.map((c, ci) => {
                const cellSpan = c.msb - c.lsb + 1;
                const range = cellSpan === 1 ? `${c.msb}` : `${c.msb}:${c.lsb}`;
                if (c.kind === 'reserved') {
                  return (
                    <div
                      key={ci}
                      className="rdl-bitgrid-cell reserved"
                      style={{ gridColumn: `span ${cellSpan}` }}
                      title={`reserved [${range}]`}
                    >
                      <span className="fieldname">—</span>
                    </div>
                  );
                }
                const acc = (c.field.access || 'na').toLowerCase();
                return (
                  <div
                    key={ci}
                    className={`rdl-bitgrid-cell acc-${acc}`}
                    style={{ gridColumn: `span ${cellSpan}` }}
                    title={`[${range}] ${c.field.name} (${acc.toUpperCase()})`}
                  >
                    <span className="fieldname">{c.field.name}</span>
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}
