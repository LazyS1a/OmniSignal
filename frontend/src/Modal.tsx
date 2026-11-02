import { useEffect, useRef, type ReactNode } from 'react';

export default function Modal({ title, children, onClose, wide = false }: { title: string; children: ReactNode; onClose: () => void; wide?: boolean }) {
 const ref = useRef<HTMLDivElement>(null);
 const closeRef = useRef(onClose);
 closeRef.current = onClose;
 useEffect(() => {
  const prior = document.activeElement as HTMLElement;
  const previousOverflow = document.body.style.overflow;
  document.body.style.overflow = 'hidden';
  ref.current?.focus();
  function keys(e: KeyboardEvent) {
   if (e.key === 'Escape') closeRef.current();
   if (e.key === 'Tab') {
    const els = Array.from(ref.current?.querySelectorAll<HTMLElement>('button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),a[href]') || []).filter(el => el.offsetParent !== null);
    const first = els[0], last = els[els.length - 1];
    if (e.shiftKey && (document.activeElement === first || document.activeElement === ref.current)) { e.preventDefault(); last?.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
   }
  }
  document.addEventListener('keydown', keys);
  return () => { document.body.style.overflow = previousOverflow; document.removeEventListener('keydown', keys); prior?.focus(); };
 }, []);
 return <div className="modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}><div ref={ref} tabIndex={-1} className={`modal ${wide ? 'modal-wide' : ''}`} role="dialog" aria-modal="true" aria-label={title}><div className="modal-heading"><h2>{title}</h2><button className="icon-button" onClick={onClose} aria-label="关闭"><svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m6 6 12 12M6 18 18 6" /></svg></button></div>{children}</div></div>;
}
