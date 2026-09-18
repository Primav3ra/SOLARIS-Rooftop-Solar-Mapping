import { useId, useState } from 'react';

/**
 * A chart with a reachable table view behind it.
 *
 * The table is not a fallback, it is a peer. Two reasons, and the second is the
 * one that made it non-negotiable here:
 *
 * 1. A `<canvas>` is opaque to a screen reader, so without a table the figure
 *    is simply unavailable to some readers.
 * 2. An academic reader wants the numbers. A project whose whole argument is
 *    "here is the evidence" should not make the evidence harder to read than
 *    the picture of it.
 *
 * `rows` and `columns` describe the same data the chart draws, so the two
 * cannot disagree -- both are rendered from one source.
 */
export default function Figure({
  title,
  caption,
  children,
  columns,
  rows,
  tall = false,
  source,
}) {
  const [showTable, setShowTable] = useState(false);
  const id = useId();
  const hasTable = Boolean(columns?.length && rows?.length);

  return (
    <figure className="figure">
      <div className="figure__head">
        <figcaption className="figure__title" id={`${id}-title`}>
          {title}
        </figcaption>
        {hasTable && (
          <button
            className="figure__toggle"
            aria-expanded={showTable}
            aria-controls={`${id}-table`}
            onClick={() => setShowTable((s) => !s)}
          >
            {showTable ? 'Show chart' : 'Show table'}
          </button>
        )}
      </div>

      {showTable && hasTable ? (
        <div className="table-wrap" id={`${id}-table`}>
          <table>
            <thead>
              <tr>
                {columns.map((col) => (
                  <th key={col.key} className={col.numeric ? 'num' : undefined}>
                    {col.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={row.key ?? i}>
                  {columns.map((col) => (
                    <td key={col.key} className={col.numeric ? 'num' : undefined}>
                      {col.render ? col.render(row) : row[col.key]}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div
          className={`figure__canvas${tall ? ' figure__canvas--tall' : ''}`}
          role="img"
          aria-labelledby={`${id}-title`}
        >
          {children}
        </div>
      )}

      {caption && <p className="figure__caption">{caption}</p>}
      {source && <p className="source-note">Source: {source}</p>}
    </figure>
  );
}
