import type { Field, Reg } from './types';

type Cell =
  | { kind: 'field'; field: Field; msb: number; lsb: number }
  | { kind: 'reserved'; msb: number; lsb: number };

type Props = { reg: Reg };

/**
 * Visual bit grid: header row of bit indices ([width-1..0], MSB left like
 * datasheets), followed by a row of field cells. Each field is one wide
 * cell spanning all of its bits via `grid-column: span N`. Gaps between
 * fields render as "reserved" cells so the user sees the unallocated bits.
 *
 * Pre-Apr 30 attempt put one DOM node per bit with the name only on the
 * first cell — which clipped to ~3% width and showed a single letter.
 * This grid-spans the name across the whole field's column range so it
 * fits or ellipsises inside its actual width.
 */
export function BitGrid({ reg }: Props) {
  const width = reg.width || 32;
  const fields = reg.fields || [];

  // Walk MSB → LSB collecting fields; insert "reserved" cells for gaps.
  const sortedFields = [...fields].sort((a, b) => b.msb - a.msb);
  const cells: Cell[] = [];
  let cursor = width - 1;
  for (const f of sortedFields) {
    if (cursor > f.msb) {
      cells.push({ kind: 'reserved', msb: cursor, lsb: f.msb + 1 });
    }
    cells.push({ kind: 'field', field: f, msb: f.msb, lsb: f.lsb });
    cursor = f.lsb - 1;
  }
  if (cursor >= 0) {
    cells.push({ kind: 'reserved', msb: cursor, lsb: 0 });
  }

  return (
    <div className="rdl-bitgrid" role="img" aria-label={`${width}-bit register layout`}>
      <div
        className="rdl-bitgrid-bits"
        style={{ gridTemplateColumns: `repeat(${width}, minmax(0, 1fr))` }}
      >
        {Array.from({ length: width }, (_, i) => width - 1 - i).map(b => (
          <div key={b} className="rdl-bitgrid-bit">{b}</div>
        ))}
      </div>
      <div
        className="rdl-bitgrid-fields"
        style={{ gridTemplateColumns: `repeat(${width}, minmax(0, 1fr))` }}
      >
        {cells.map((c, i) => {
          const span = c.msb - c.lsb + 1;
          const range = span === 1 ? `${c.msb}` : `${c.msb}:${c.lsb}`;
          if (c.kind === 'reserved') {
            return (
              <div
                key={i}
                className="rdl-bitgrid-cell reserved"
                style={{ gridColumn: `span ${span}` }}
                title={`reserved [${range}]`}
              >
                <span className="fieldname">—</span>
              </div>
            );
          }
          const acc = (c.field.access || 'na').toLowerCase();
          return (
            <div
              key={i}
              className={`rdl-bitgrid-cell acc-${acc}`}
              style={{ gridColumn: `span ${span}` }}
              title={`[${range}] ${c.field.name} (${acc.toUpperCase()})`}
            >
              <span className="fieldname">{c.field.name}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
