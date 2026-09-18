import { useEffect, useRef, useState } from 'react';

/**
 * Completed computations for this session, restorable without recomputation.
 *
 * Rooftop yield figures are not interpretable in isolation. A value of
 * 1,300 kWh/kWp/yr conveys nothing about whether a particular roof is a good
 * candidate; the informative quantity is the difference between that roof and
 * others, or between the same roof under different cleaning assumptions. The
 * task is therefore comparative, and a comparative task requires the earlier
 * results to remain available.
 *
 * Restoring from this list reads from memory and issues no Earth Engine calls,
 * so it neither costs quota nor consumes the guest allowance.
 */
export default function SiteHistory({ entries, activeId, onRestore, onForget, onClear }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const onDocumentClick = (event) => {
      if (!ref.current?.contains(event.target)) setOpen(false);
    };
    const onKey = (event) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDocumentClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocumentClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  if (!entries.length) {
    return (
      <p className="field__hint" style={{ marginTop: '0.5rem' }}>
        Computed sites are retained for this session so they can be compared without
        recomputation.
      </p>
    );
  }

  return (
    <div className="history" ref={ref}>
      <button
        className="history__toggle"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span>
          Saved sites <strong>{entries.length}</strong>
        </span>
        <span aria-hidden="true" style={{ opacity: 0.6 }}>
          {open ? '▴' : '▾'}
        </span>
      </button>

      {open && (
        <div className="history__list" role="listbox">
          {entries.map((entry) => (
            <div
              key={entry.id}
              className={`history__item${entry.id === activeId ? ' is-active' : ''}`}
            >
              <button
                className="history__restore"
                onClick={() => {
                  onRestore(entry.id);
                  setOpen(false);
                }}
              >
                <span className="history__coords">{entry.label.coords}</span>
                <span className="history__meta">
                  {entry.label.window}
                  {entry.label.area != null && (
                    <> · {Math.round(entry.label.area).toLocaleString('en-IN')} m²</>
                  )}
                  {entry.label.energy != null && (
                    <> · {Math.round(entry.label.energy).toLocaleString('en-IN')} kWh</>
                  )}
                </span>
              </button>
              <button
                className="history__forget"
                aria-label={`Remove ${entry.label.coords}`}
                onClick={() => onForget(entry.id)}
              >
                ×
              </button>
            </div>
          ))}
          <button className="history__clear" onClick={onClear}>
            Clear all
          </button>
        </div>
      )}
    </div>
  );
}
