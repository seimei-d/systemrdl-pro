import { useEffect, useRef, useLayoutEffect, useState } from 'react';

export type CtxMenuItem =
  | { sep: true }
  | { sep?: false; label: string; hint?: string; action: () => void };

export type CtxMenuState = {
  x: number;
  y: number;
  items: CtxMenuItem[];
} | null;

type Props = {
  state: CtxMenuState;
  onClose: () => void;
};

export function ContextMenu({ state, onClose }: Props) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState({ x: 0, y: 0 });

  // Keep menu inside the viewport — measure after render, then nudge.
  useLayoutEffect(() => {
    if (!state || !ref.current) return;
    const menu = ref.current;
    const x = Math.min(state.x, window.innerWidth - menu.offsetWidth - 8);
    const y = Math.min(state.y, window.innerHeight - menu.offsetHeight - 8);
    setPos({ x: Math.max(4, x), y: Math.max(4, y) });
  }, [state]);

  useEffect(() => {
    if (!state) return;
    const onDocClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('click', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('click', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [state, onClose]);

  if (!state) return null;
  return (
    <div
      ref={ref}
      className="rdl-ctx-menu"
      role="menu"
      aria-label="Tree row actions"
      style={{ left: pos.x, top: pos.y }}
    >
      {state.items.map((it, i) => {
        if (it.sep) return <div key={i} className="sep" />;
        return (
          <div
            key={i}
            className="item"
            role="menuitem"
            onClick={() => { it.action(); onClose(); }}
          >
            <span>{it.label}</span>
            {it.hint && <span className="hint">{it.hint}</span>}
          </div>
        );
      })}
    </div>
  );
}
