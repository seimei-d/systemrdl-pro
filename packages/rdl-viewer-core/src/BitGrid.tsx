import type { Field, Reg } from './types';

type Props = { reg: Reg };

/**
 * Visual bit grid: one cell per bit in the register, MSB on the left like
 * datasheets. Each field gets a coloured rectangle spanning its bit range
 * with the field name centred inside (truncated if it doesn't fit).
 *
 * Hover any cell to highlight the whole field. Click a field to select
 * its row in the inline detail list (TODO if useful — currently click is a no-op).
 */
export function BitGrid({ reg }: Props) {
  const width = reg.width || 32;
  const fields = reg.fields || [];

  // Cell colour follows access-mode pill colours via CSS variables.
  const cells: { bit: number; field?: Field; isFirstOfField?: boolean; isLastOfField?: boolean }[] = [];
  for (let b = width - 1; b >= 0; b--) {
    const f = fields.find(f => b >= f.lsb && b <= f.msb);
    cells.push({
      bit: b,
      field: f,
      isFirstOfField: f && b === f.msb,
      isLastOfField: f && b === f.lsb,
    });
  }

  const cellsPerRow = Math.min(width, 32);
  const rowCount = Math.ceil(width / cellsPerRow);
  const rows: typeof cells[] = [];
  for (let r = 0; r < rowCount; r++) {
    rows.push(cells.slice(r * cellsPerRow, (r + 1) * cellsPerRow));
  }

  return (
    <div className="rdl-bitgrid" role="img" aria-label={`${width}-bit register layout`}>
      {rows.map((row, ri) => (
        <div key={ri} className="rdl-bitgrid-row">
          {row.map(cell => {
            const acc = (cell.field?.access || 'rsv').toLowerCase();
            const cls = [
              'rdl-bitgrid-cell',
              cell.field ? `acc-${acc}` : 'reserved',
              cell.isFirstOfField ? 'first' : '',
              cell.isLastOfField ? 'last' : '',
            ].filter(Boolean).join(' ');
            return (
              <div
                key={cell.bit}
                className={cls}
                title={cell.field
                  ? `[${cell.field.msb}:${cell.field.lsb}] ${cell.field.name} (${acc.toUpperCase()})`
                  : `[${cell.bit}] reserved`}
              >
                <span className="bitnum">{cell.bit}</span>
                {cell.isFirstOfField && cell.field && cell.field.msb !== cell.field.lsb && (
                  <span className="fieldname">{cell.field.name}</span>
                )}
                {cell.isFirstOfField && cell.field && cell.field.msb === cell.field.lsb && (
                  <span className="fieldname single">{cell.field.name}</span>
                )}
              </div>
            );
          })}
        </div>
      ))}
    </div>
  );
}
