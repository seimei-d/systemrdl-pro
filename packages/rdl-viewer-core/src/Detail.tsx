import { Fragment, memo, useDeferredValue, useMemo, useState } from 'react';
import type { Addrmap, CustomProperty, EncodeEntry, Field, Mem, ParameterBinding, Reg, SourceLoc, Transport } from './types';
import { BitGrid } from './BitGrid';

type Props = {
  reg: Reg | null;
  mem: Mem | null;
  addrmap: Addrmap | null;
  path: string[] | null;
  transport: Transport;
};

// Memoised so filter keystrokes in Viewer (which re-render the whole
// component) don't re-render the heavy bit-grid + field rows when the
// active reg hasn't actually changed. `reg` and `path` come from
// `findRegByKey` which returns the same node-reference for the same
// (root, key) pair across re-renders.
function DetailInner({ reg, mem, addrmap, path, transport }: Props) {
  const [decoderInput, setDecoderInput] = useState('');

  // Defer the reg/mem/path so React keeps showing the previous content
  // while it renders the next one. This prevents the empty-frame flicker
  // between register switches — the old content stays on screen until the
  // new render is committed in a single paint.
  const dReg = useDeferredValue(reg);
  const dMem = useDeferredValue(mem);
  const dPath = useDeferredValue(path);

  const r = dReg ?? reg;  // show deferred, but never show null if real value is available
  const m = dMem ?? mem;
  const p = dPath ?? path;

  const decoded = useMemo(() => decode(reg, decoderInput), [reg, decoderInput]);

  const reveal = (source: SourceLoc | undefined) => {
    if (source && transport.reveal) transport.reveal(source);
  };

  if (!r && !m && !addrmap) {
    return (
      <div className="rdl-detail">
        <div className="placeholder">Select a register or memory to see details.</div>
      </div>
    );
  }

  if (addrmap && !r && !m) {
    return (
      <div className="rdl-detail">
        <h2>{addrmap.name}</h2>
        {addrmap.displayName && addrmap.displayName !== addrmap.name && (
          <div className="display-name">{addrmap.displayName}</div>
        )}
        {p && <div className="breadcrumb">{p.join('.')}</div>}
        <div className="meta">
          <span className="k">Address</span><span className="v">{addrmap.address}</span>
          <span className="k">Size</span><span className="v">{addrmap.size}</span>
          {addrmap.isBridge && <><span className="k">Type</span><span className="v">Bridge</span></>}
        </div>
        {addrmap.desc && <div className="desc">{addrmap.desc}</div>}
        {addrmap.parameters && addrmap.parameters.length > 0 && (
          <ParametersTable parameters={addrmap.parameters} />
        )}
        {addrmap.customProperties && addrmap.customProperties.length > 0 && (
          <CustomPropertiesTable properties={addrmap.customProperties} />
        )}
      </div>
    );
  }

  // ─── Memory detail ─────────────────────────────────────────────────────
  if (m && !r) {
    return (
      <div className="rdl-detail">
        <h2>{m.name}</h2>
        {m.displayName && m.displayName !== m.name && (
          <div className="display-name">{m.displayName}</div>
        )}
        {p && <div className="breadcrumb">{p.join('.')}</div>}
        <div className="meta">
          <span className="k">Address</span><span className="v">{m.address}</span>
          <span className="k">Size</span><span className="v">{m.size}</span>
          {m.memEntries != null && (
            <><span className="k">Entries</span><span className="v">{String(m.memEntries)}</span></>
          )}
          {m.memWidth != null && (
            <><span className="k">Width</span><span className="v">{String(m.memWidth)} bits</span></>
          )}
        </div>
        {m.desc && <div className="desc">{m.desc}</div>}
        {m.parameters && m.parameters.length > 0 && (
          <ParametersTable parameters={m.parameters} />
        )}
      </div>
    );
  }

  // At this point r is non-null (checked by !r && !m and m && !r branches above)
  const regNonNull = r!;
  const decoderActive = decoded !== null;

  return (
    <div className="rdl-detail">
      <h2>{regNonNull.name}</h2>
      {regNonNull.displayName && regNonNull.displayName !== regNonNull.name && (
        <div className="display-name">{regNonNull.displayName}</div>
      )}
      <div className="breadcrumb">{p!.join('.')}</div>
      <div className="meta">
        <span className="k">Address</span><span className="v">{regNonNull.address}</span>
        <span className="k">Width</span><span className="v">{String(regNonNull.width)}</span>
        <span className="k">Reset</span><span className="v">{regNonNull.reset ?? '—'}</span>
        <span className="k">Access</span><span className="v">{regNonNull.accessSummary || '—'}</span>
        {regNonNull.customProperties?.map(property => (
          <Fragment key={property.name}>
            <span className="k">{property.name}</span>
            <span className="v">{renderCustomPropertyValue(property.value)}</span>
          </Fragment>
        ))}
      </div>
      {regNonNull.desc && <div className="desc">{regNonNull.desc}</div>}
      {regNonNull.parameters && regNonNull.parameters.length > 0 && (
        <ParametersTable parameters={regNonNull.parameters} />
      )}
      {regNonNull.accessWidth && regNonNull.accessWidth !== regNonNull.width && (
        <div className="rdl-split-access">
          <strong>Split access:</strong>{' '}
          {regNonNull.width}-bit register, software accesses in {regNonNull.accessWidth}-bit chunks
          {' '}({Math.ceil(regNonNull.width / regNonNull.accessWidth)}× {regNonNull.accessWidth}-bit)
        </div>
      )}
      {regNonNull.loadState === 'placeholder' ? (
        // T1.7: spine-only spawn — fields[] hasn't been fetched yet. The
        // Viewer's useEffect kicks off transport.expandNode in parallel with
        // this render; once it returns, the tree is spliced and Detail
        // re-renders with real fields.
        <div className="rdl-loading" role="status" aria-live="polite">
          Loading register details…
        </div>
      ) : (
        <>
          <BitGrid reg={regNonNull} />
          <RegisterDecoder value={decoderInput} onChange={setDecoderInput} />
          <div className="fields-title">
            Bit fields
            {decoderActive && (
              <span className="rdl-fields-mode">
                · showing decoded values for <code>{decoderInput}</code>
              </span>
            )}
          </div>
          <div className="field-header" aria-hidden="true">
            <span>Bits</span>
            <span>Name</span>
            <span>Access</span>
            <span>Reset / Value</span>
            <span>Description</span>
            <span>Properties</span>
          </div>
          {(regNonNull.fields || []).map((f, i) => (
            <FieldRow
              key={i}
              field={f}
              reveal={reveal}
              decoded={decoded?.[f.name]}
            />
          ))}
        </>
      )}
    </div>
  );
}
export const Detail = memo(DetailInner);

type DecodedField = { value: string; encode?: string };

/**
 * Decode a register-level hex/bin/dec value into per-field hex strings.
 * Returns null when the input is empty or unparseable, in which case the
 * field rows fall back to showing reset values.
 *
 * Implementation note: JavaScript bitwise operators (`>>>`, `&`) operate on
 * 32-bit signed integers. For 64-bit (or any >32-bit) registers, fields
 * whose `lsb >= 32` would silently decode to `0` because `>>> 32` is `>>> 0`
 * in JS. We therefore route the entire decode through `BigInt` whenever
 * the register width exceeds 32 bits — `BigInt` shifts and masks operate
 * at arbitrary precision.
 */
function decode(reg: Reg | null, raw: string): Record<string, DecodedField> | null {
  if (!reg) return null;
  const width = reg.width || 32;
  const out: Record<string, DecodedField> = {};

  if (width > 32) {
    const n = parseInputValueBig(raw);
    if (n === null) return null;
    const regMask = (1n << BigInt(width)) - 1n;
    const masked = n & regMask;
    for (const f of reg.fields || []) {
      const fwidth = Math.max(1, f.msb - f.lsb + 1);
      const fmask = (1n << BigInt(fwidth)) - 1n;
      const v = (masked >> BigInt(f.lsb)) & fmask;
      const digits = Math.max(1, Math.ceil(fwidth / 4));
      const hex = '0x' + v.toString(16).padStart(digits, '0');
      const enc = f.encode?.find(e => {
        const ev = parseInputValueBig(e.value);
        return ev !== null && ev === v;
      })?.name;
      out[f.name] = { value: hex, encode: enc };
    }
    return out;
  }

  // ≤32-bit register — Number-based path. Stays for the common case so
  // we don't pay BigInt allocation cost on every field of every reg.
  const n = parseInputValue(raw);
  if (n === null) return null;
  const mask = width >= 32 ? 0xFFFFFFFF : (2 ** width) - 1;
  // `& 0xFFFFFFFF` returns a signed Int32; `>>> 0` re-coerces to unsigned.
  const masked = (n & mask) >>> 0;
  for (const f of reg.fields || []) {
    const fwidth = Math.max(1, f.msb - f.lsb + 1);
    const fmask = fwidth >= 32 ? 0xFFFFFFFF : (2 ** fwidth) - 1;
    const v = ((masked >>> f.lsb) & fmask) >>> 0;
    const digits = Math.max(1, Math.ceil(fwidth / 4));
    const hex = '0x' + v.toString(16).padStart(digits, '0');
    const enc = f.encode?.find(e => parseInputValue(e.value) === v)?.name;
    out[f.name] = { value: hex, encode: enc };
  }
  return out;
}

/**
 * Same parse rules as ``parseInputValue`` but returns a ``BigInt`` so
 * values above 2^53 don't lose precision.
 */
function parseInputValueBig(s: string): bigint | null {
  if (!s) return null;
  const trimmed = s.replace(/_/g, '').trim();
  if (!trimmed) return null;
  try {
    if (/^0x[0-9a-f]+$/i.test(trimmed)) return BigInt('0x' + trimmed.slice(2));
    if (/^0b[01]+$/i.test(trimmed)) return BigInt('0b' + trimmed.slice(2));
    if (/^\d+$/.test(trimmed)) return BigInt(trimmed);
  } catch {
    return null;
  }
  return null;
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
      {field.customProperties && field.customProperties.length > 0 && (
        <table className="rdl-field-properties" onClick={e => e.stopPropagation()}>
          <tbody>
            {field.customProperties.map(property => (
              <tr key={property.name}>
                <th scope="row">{property.name}</th>
                <td>{renderCustomPropertyValue(property.value)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {field.encode && field.encode.length > 0 && (
        <EncodeTable entries={field.encode} />
      )}
    </div>
  );
}

function CustomPropertiesTable({
  properties,
}: {
  properties: CustomProperty[];
}) {
  return (
    <div className="rdl-custom-properties" onClick={e => e.stopPropagation()}>
      <div className="rdl-custom-properties-title">Custom properties</div>
      <table className="rdl-custom-properties-table">
        <tbody>
          {properties.map(property => (
            <tr key={property.name}>
              <td className="n"><code>{property.name}</code></td>
              <td className="v"><code>{renderCustomPropertyValue(property.value)}</code></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function renderCustomPropertyValue(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return JSON.stringify(value) ?? String(value);
}

function ParametersTable({ parameters }: { parameters: ParameterBinding[] }) {
  return (
    <div className="rdl-parameters" onClick={e => e.stopPropagation()}>
      <div className="rdl-parameters-title">Parameters</div>
      <table className="rdl-parameters-table">
        <thead>
          <tr><th>Name</th><th>Type</th><th>Value</th></tr>
        </thead>
        <tbody>
          {parameters.map((p, i) => (
            <tr key={i}>
              <td className="n"><code>{p.name}</code></td>
              <td className="t">{p.type ?? '—'}</td>
              <td className="v"><code>{renderParamValue(p.value)}</code></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function renderParamValue(v: unknown): string {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'number' && Number.isInteger(v)) {
    // Integers render in both decimal and hex when non-trivial — matches the
    // muscle memory of register people who think in both bases.
    return v >= 16 ? `${v} (0x${v.toString(16)})` : String(v);
  }
  if (typeof v === 'boolean' || typeof v === 'number') return String(v);
  if (typeof v === 'string') return v;
  return String(v);
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
