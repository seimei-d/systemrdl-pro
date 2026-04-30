import type { Reg, SourceLoc, Transport } from './types';

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
      <div className="fields-title">Bit fields</div>
      {(reg.fields || []).map((f, i) => {
        const acc = (f.access || 'na').toLowerCase();
        const blurb = f.desc || (f.displayName && f.displayName !== f.name ? f.displayName : '') || '';
        const onClick = f.source ? () => reveal(f.source) : undefined;
        return (
          <div
            className="field"
            key={i}
            onClick={onClick}
            style={onClick ? { cursor: 'pointer' } : undefined}
            title={onClick ? 'Click to reveal in editor' : undefined}
          >
            <b>[{f.msb}:{f.lsb}]</b>
            <b>{f.name}</b>
            <span className={'rdl-pill ' + acc}>{acc.toUpperCase()}</span>
            <span>{f.reset || '—'}</span>
            <span className="desc">{blurb}</span>
          </div>
        );
      })}
      {reg.source && transport.reveal && (
        <div className="src-link" onClick={() => reveal(reg.source)}>
          → {((reg.source.uri || '').split('/').pop() || reg.source.uri)}:
          {(reg.source.line || 0) + 1}
        </div>
      )}
    </div>
  );
}
