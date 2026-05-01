import { useMemo, useState } from 'react';
import type { EncodeEntry, Field, Reg, SourceLoc, Transport } from './types';
import { BitGrid } from './BitGrid';

type Props = {
  reg: Reg | null;
  path: string[] | null;
  transport: Transport;
};

export function Detail({ reg, path, transport }: Props) {
  // Decoder input lives at the Detail level so per-field rows can show
  // the decoded value in their reset column when input is non-empty.
  const [decoderInput, setDecoderInput] = useState('');

  const decoded = useMemo(() => decode(reg, decoderInput), [reg, decoderInput]);

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

  const decoderActive = decoded !== null;

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
      {reg.accessWidth && reg.accessWidth !== reg.width && (
        <div className="rdl-split-access">
          <strong>Split access:</strong>{' '}
          {reg.width}-bit register, software accesses in {reg.accessWidth}-bit chunks
          {' '}({Math.ceil(reg.width / reg.accessWidth)}× {reg.accessWidth}-bit)
        </div>
      )}
      <BitGrid reg={reg} />
      <RegisterDecoder value={decoderInput} onChange={setDecoderInput} />
      <div className="fields-title">
        Bit fields
        {decoderActive && (
          <span className="rdl-fields-mode">
            · showing decoded values for <code>{decoderInput}</code>
          </span>
        )}
      </div>
      {(reg.fields || []).map((f, i) => (
        <FieldRow
          key={i}
          field={f}
          reveal={reveal}
          decoded={decoded?.[f.name]}
        />
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

type DecodedField = { value: string; encode?: string };

/**
 * Decode a register-level hex/bin/dec value into per-field hex strings.
 * Returns null when the input is empty or unparseable, in which case the
 * field rows fall back to showing reset values.
 */
function decode(reg: Reg | null, raw: string): Record<string, DecodedField> | null {
  if (!reg) return null;
  const n = parseInputValue(raw);
  if (n === null) return null;
  const width = reg.width || 32;
  const mask = width >= 53 ? Number.MAX_SAFE_INTEGER : (2 ** width) - 1;
  const masked = Number(n) & mask;
  const out: Record<string, DecodedField> = {};
  for (const f of reg.fields || []) {
    const fwidth = Math.max(1, f.msb - f.lsb + 1);
    const fmask = fwidth >= 53 ? Number.MAX_SAFE_INTEGER : (2 ** fwidth) - 1;
    const v = (masked >>> f.lsb) & fmask;
    const digits = Math.max(1, Math.ceil(fwidth / 4));
    const hex = '0x' + v.toString(16).padStart(digits, '0');
    const enc = f.encode?.find(e => parseInputValue(e.value) === v)?.name;
    out[f.name] = { value: hex, encode: enc };
  }
  return out;
}

/**
 * One row in the per-field breakdown. Shows the bit range, name, badges
 * (counter/intr tags), access pill, value (decoded if active else reset),
 * description. If the field has an `encode` enum, append a value-name
 * table below the row.
 */
function FieldRow({
  field, reveal, decoded,
}: {
  field: Field;
  reveal: (s: SourceLoc | undefined) => void;
  decoded: DecodedField | undefined;
}) {
  const acc = (field.access || 'na').toLowerCase();
  const blurb = field.desc
    || (field.displayName && field.displayName !== field.name ? field.displayName : '')
    || '';
  const onClick = field.source ? () => reveal(field.source) : undefined;
  const valueCell = decoded
    ? (
      <span className="rdl-field-value decoded" title={`Decoded value: ${decoded.value}${decoded.encode ? ' · ' + decoded.encode : ''} (reset: ${field.reset ?? '—'})`}>
        {decoded.value}
        {decoded.encode && <em className="rdl-field-encode-hit"> · {decoded.encode}</em>}
      </span>
    )
    : <span className="rdl-field-value" title="Reset value">{field.reset || '—'}</span>;
  return (
    <div
      className="field"
      onClick={onClick}
      style={onClick ? { cursor: 'pointer' } : undefined}
      title={onClick ? 'Click to reveal in editor' : undefined}
    >
      <b>[{field.msb}:{field.lsb}]</b>
      <span className="rdl-field-name">
        {field.name}
        {field.isCounter && (
          <span
            className="rdl-tag counter"
            title="Counter — increments on its `incr` signal (SystemRDL 9.10)."
          >counter</span>
        )}
        {field.isIntr && (
          <span
            className="rdl-tag intr"
            title="Interrupt — set by hardware on a triggering condition; cleared by software (SystemRDL 9.7)."
          >intr</span>
        )}
      </span>
      <span className={'rdl-pill ' + acc}>{acc.toUpperCase()}</span>
      {valueCell}
      <span className="desc">{blurb}</span>
      {field.encode && field.encode.length > 0 && (
        <EncodeTable entries={field.encode} />
      )}
    </div>
  );
}

function EncodeTable({ entries }: { entries: EncodeEntry[] }) {
  // Native <details> gives us collapse/expand for free, accessible by
  // keyboard (Space/Enter on the summary), no extra state to manage.
  return (
    <details
      className="rdl-encode-details"
      onClick={e => e.stopPropagation()}
    >
      <summary>
        <span className="rdl-encode-summary-label">enum</span>
        <span className="rdl-encode-summary-count">{entries.length} values</span>
      </summary>
      <table className="rdl-encode-table">
        <thead>
          <tr><th>Value</th><th>Name</th><th>Description</th></tr>
        </thead>
        <tbody>
          {entries.map((e, i) => (
            <tr key={i}>
              <td className="v">{e.value}</td>
              <td className="n">
                {e.name}
                {e.displayName && e.displayName !== e.name && (
                  <span className="display-name"> · {e.displayName}</span>
                )}
              </td>
              <td className="d">{e.desc || ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

/**
 * Decoder input. Lifted out of state ownership — Detail owns the value
 * and feeds the per-field decoded results back into each FieldRow's
 * value column.
 */
function RegisterDecoder({
  value, onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="rdl-decoder" onClick={e => e.stopPropagation()}>
      <label className="rdl-decoder-label">
        Decode value:
        <input
          type="text"
          spellCheck={false}
          placeholder="0x… or 0b… or decimal"
          value={value}
          onChange={e => onChange(e.target.value)}
        />
        {value && (
          <button
            type="button"
            className="rdl-decoder-clear"
            title="Clear decoder input → fall back to reset values"
            onClick={() => onChange('')}
          >×</button>
        )}
      </label>
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
