import { useMemo, useState } from 'react';
import type { EncodeEntry, Field, Reg, SourceLoc, Transport } from './types';
import { BitGrid } from './BitGrid';

type Props = {
  reg: Reg | null;
  path: string[] | null;
  transport: Transport;
};

export function Detail({ reg, path, transport }: Props) {
  if (!reg || !path) {
    return (
      <div className="rdl-detail">
        <div className="placeholder">Select a register to see details.</div>
      </div>
    );
  }

  const reveal = (source: SourceLoc | undefined) => {
    if (source && transport.reveal) transport.reveal(source);
  };

  return (
    <div className="rdl-detail">
      <h2>{reg.name}</h2>
      {reg.displayName && reg.displayName !== reg.name && (
        <div className="display-name">{reg.displayName}</div>
      )}
      <div className="breadcrumb">{path.join('.')}</div>
      <div className="meta">
        <span className="k">Address</span><span className="v">{reg.address}</span>
        <span className="k">Width</span><span className="v">{String(reg.width)}</span>
        <span className="k">Reset</span><span className="v">{reg.reset ?? '—'}</span>
        <span className="k">Access</span><span className="v">{reg.accessSummary || '—'}</span>
      </div>
      {reg.desc && <div className="desc">{reg.desc}</div>}
      <BitGrid reg={reg} />
      <RegisterDecoder reg={reg} />
      <div className="fields-title">Bit fields</div>
      {(reg.fields || []).map((f, i) => (
        <FieldRow key={i} field={f} reveal={reveal} />
      ))}
      {reg.source && transport.reveal && (
        <div className="src-link" onClick={() => reveal(reg.source)}>
          → {((reg.source.uri || '').split('/').pop() || reg.source.uri)}:
          {(reg.source.line || 0) + 1}
        </div>
      )}
    </div>
  );
}

/**
 * One row in the per-field breakdown. Shows the bit range, name, badges
 * (◷ counter, ⚡ intr), access pill, reset, description. If the field has
 * an `encode` enum, append a value-name table below the row.
 */
function FieldRow({ field, reveal }: { field: Field; reveal: (s: SourceLoc | undefined) => void }) {
  const acc = (field.access || 'na').toLowerCase();
  const blurb = field.desc
    || (field.displayName && field.displayName !== field.name ? field.displayName : '')
    || '';
  const onClick = field.source ? () => reveal(field.source) : undefined;
  return (
    <div
      className="field"
      onClick={onClick}
      style={onClick ? { cursor: 'pointer' } : undefined}
      title={onClick ? 'Click to reveal in editor' : undefined}
    >
      <b>[{field.msb}:{field.lsb}]</b>
      <b>
        {field.name}
        {field.isCounter && (
          <span className="rdl-badge counter" title="counter">◷</span>
        )}
        {field.isIntr && (
          <span className="rdl-badge intr" title="interrupt">⚡</span>
        )}
      </b>
      <span className={'rdl-pill ' + acc}>{acc.toUpperCase()}</span>
      <span>{field.reset || '—'}</span>
      <span className="desc">{blurb}</span>
      {field.encode && field.encode.length > 0 && (
        <EncodeTable entries={field.encode} />
      )}
    </div>
  );
}

function EncodeTable({ entries }: { entries: EncodeEntry[] }) {
  return (
    <table className="rdl-encode-table" onClick={e => e.stopPropagation()}>
      <thead>
        <tr><th>Value</th><th>Name</th><th>Description</th></tr>
      </thead>
      <tbody>
        {entries.map((e, i) => (
          <tr key={i}>
            <td className="v">{e.value}</td>
            <td className="n">{e.name}</td>
            <td className="d">{e.desc || ''}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * Tier 3.3: paste / type a hex value, see field-by-field decode live.
 *
 * Pure UI — no transport calls. Uses the field bit ranges already in the
 * elaborated tree. Clamping to the register's width avoids confusing
 * overflow when the user pastes a 64-bit value into a 32-bit reg.
 */
function RegisterDecoder({ reg }: { reg: Reg }) {
  const [raw, setRaw] = useState('');
  const decoded = useMemo(() => {
    const n = parseInputValue(raw);
    if (n === null) return null;
    const width = reg.width || 32;
    const mask = width >= 53 ? Number.MAX_SAFE_INTEGER : (2 ** width) - 1;
    const masked = Number(n) & mask;
    const out: { name: string; value: string; raw: number; encode?: string }[] = [];
    for (const f of reg.fields || []) {
      const fwidth = f.msb - f.lsb + 1;
      const fmask = fwidth >= 53 ? Number.MAX_SAFE_INTEGER : (2 ** fwidth) - 1;
      const v = (masked >>> f.lsb) & fmask;
      const hex = '0x' + v.toString(16).padStart(Math.max(1, Math.ceil(fwidth / 4)), '0');
      const enc = f.encode?.find(e => parseInputValue(e.value) === v)?.name;
      out.push({ name: f.name, value: hex, raw: v, encode: enc });
    }
    return out;
  }, [raw, reg]);
  return (
    <div className="rdl-decoder" onClick={e => e.stopPropagation()}>
      <label className="rdl-decoder-label">
        Decode value:
        <input
          type="text"
          spellCheck={false}
          placeholder="0x… or 0b… or decimal"
          value={raw}
          onChange={e => setRaw(e.target.value)}
        />
      </label>
      {decoded && (
        <div className="rdl-decoder-out">
          {decoded.map((d, i) => (
            <span key={i} className="rdl-decoder-field" title={`${d.name} = ${d.value}`}>
              <b>{d.name}</b>=<code>{d.value}</code>
              {d.encode && <em>·{d.encode}</em>}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function parseInputValue(s: string): number | null {
  if (!s) return null;
  const trimmed = s.replace(/_/g, '').trim();
  if (!trimmed) return null;
  if (/^0x[0-9a-f]+$/i.test(trimmed)) return parseInt(trimmed.slice(2), 16);
  if (/^0b[01]+$/i.test(trimmed)) return parseInt(trimmed.slice(2), 2);
  if (/^\d+$/.test(trimmed)) return parseInt(trimmed, 10);
  return null;
}
