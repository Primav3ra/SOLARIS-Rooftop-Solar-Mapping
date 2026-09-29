import { useEffect, useRef } from "react";
import Figure from "./Figure.jsx";
import Term from "./Term.jsx";
import PayloadInspector from "./PayloadInspector.jsx";
import {
  Chart,
  baseOptions,
  palette,
  lossColour,
  directLabels,
  cssVar,
  crosshair,
  enableKeyboardScrub,
} from "./charts.js";
import { fmt } from "../data/reports.js";

/**
 * The four loss terms, in one place.
 *
 * Declared once so the stacked share chart, the waterfall and the finding
 * lines cannot disagree about what a term is called or which colour it wears.
 */
const LOSS_TERMS = [
  {
    term: "shadow",
    key: "shadow_contribution_pct",
    label: "Shadow",
    blame: "neighbouring buildings",
  },
  {
    term: "sky_view",
    key: "svf_contribution_pct",
    label: "Sky view",
    blame: "blocked sky",
  },
  {
    term: "heat",
    key: "uhi_contribution_pct",
    label: "Heat island",
    blame: "urban warmth",
  },
  {
    term: "soiling",
    key: "soiling_contribution_pct",
    label: "Soiling",
    blame: "dust",
  },
];

/**
 * The cascade as losses rather than running totals.
 *
 * Each stage carries what went in, what came out, and the loss as a share of
 * the baseline -- which is the number the waterfall draws and the finding
 * line quotes, so both read from one computation.
 */
function cascadeStages(result) {
  const baseline = result.baseline_yield_kwh;
  if (!baseline) return [];
  const steps = [
    { term: "shadow", label: "Shadow", after: result.after_shadow_yield_kwh },
    { term: "sky_view", label: "Sky view", after: result.after_svf_yield_kwh },
    { term: "heat", label: "Heat island", after: result.after_uhi_yield_kwh },
    {
      term: "soiling",
      label: "Soiling",
      after: result.after_soiling_yield_kwh,
    },
  ];
  let before = baseline;
  const out = [];
  for (const step of steps) {
    if (step.after == null) continue;
    out.push({
      ...step,
      before,
      lossPct: ((before - step.after) / baseline) * 100,
    });
    before = step.after;
  }
  return out;
}

/** The term with the largest share, for the finding line. */
function dominantTerm(contribution) {
  if (!contribution) return null;
  return LOSS_TERMS.map((t) => ({
    ...t,
    value: contribution[t.key] ?? 0,
  })).sort((a, b) => b.value - a.value)[0];
}

/** Terms too small to label on the bar, named in prose instead. */
function tinyTerms(contribution) {
  if (!contribution) return [];
  return LOSS_TERMS.map((t) => ({
    ...t,
    value: contribution[t.key] ?? 0,
  })).filter((t) => t.value < 8);
}

function severityBadge(quality) {
  const severity = quality?.severity ?? "unknown";
  const map = {
    ok: "badge--ok",
    degraded: "badge--warn",
    unreliable: "badge--bad",
  };
  return <span className={`badge ${map[severity] ?? ""}`}>{severity}</span>;
}

/**
 * Part-to-whole across four stages with long labels: a horizontal stacked bar.
 *
 * Not a pie. Four wedges are readable but a pie makes comparing two of them
 * hard, and the question a reader has here is "which one dominates and by how
 * much", which is a length comparison.
 */
/**
 * The sentence a reader actually wants, computed from the result on screen.
 *
 * Captions on this page used to describe the chart type -- "share of total
 * modelled loss" -- which is true of every instance of the figure and tells
 * you nothing about the one in front of you. These state the finding, and
 * they move when the query moves.
 */
function cascadeFinding(result) {
  const stages = cascadeStages(result);
  if (!stages.length) return null;
  const total = stages.reduce((sum, stage) => sum + stage.lossPct, 0);
  const worst = [...stages].sort((a, b) => b.lossPct - a.lossPct)[0];
  const kept = 100 - total;
  return (
    <>
      <strong>
        {kept.toFixed(1)}% of the baseline survives the loss chain
      </strong>{" "}
      — {total.toFixed(1)}% is lost across four stages, and{" "}
      {worst.label.toLowerCase()} is the largest single step at{" "}
      {worst.lossPct.toFixed(2)} points.
    </>
  );
}

function contributionFinding(contribution) {
  const top = dominantTerm(contribution);
  if (!top) return null;
  const small = tinyTerms(contribution);
  const smallText = small.length
    ? ` Too small to label on the bar: ${small
        .map((t) => `${t.label.toLowerCase()} at ${t.value.toFixed(1)}%`)
        .join(", ")}.`
    : "";
  return (
    <>
      <strong>
        {top.label} accounts for {top.value.toFixed(0)}% of the loss here
      </strong>{" "}
      — {top.blame} is the dominant term at this site.{smallText}
    </>
  );
}

function shadeFinding(intervals) {
  if (!intervals?.length) return null;
  const lit = intervals.filter((i) => i.sun_above_horizon !== false);
  if (!lit.length) return null;
  const peak = lit.reduce((a, b) =>
    (b.shade_fraction ?? 0) > (a.shade_fraction ?? 0) ? b : a,
  );
  const peakPct = (peak.shade_fraction ?? 0) * 100;
  const label = peak.label_ist ?? peak.label;
  if (peakPct < 0.5) {
    return (
      <>
        <strong>This roof is effectively unshaded</strong> — the worst interval,{" "}
        {label}, still has only {peakPct.toFixed(2)}% of its area in shade.
        Shadow is not the constraint on this building.
      </>
    );
  }
  return (
    <>
      <strong>
        Shade peaks at {peakPct.toFixed(1)}% during {label}
      </strong>{" "}
      — that interval is where an obstruction would cost the most, and where a
      panel layout should give way first.
    </>
  );
}

/**
 * Where the beam fraction came from, in words.
 *
 * The raw source string is built for a log line -- "model:decomposition@
 * 20260929:period" -- and a reader should not have to parse a version stamp to
 * learn whether the number was measured or corrected. The full string stays in
 * the response for anyone who wants it.
 */
function beamProvenance(result) {
  const source = result.beam_fraction_source ?? "";
  const era5 = result.era5_beam_fraction;
  if (source.startsWith("model:")) {
    return era5 != null
      ? `Learned correction, from ${fmt.fixed(era5, 3)} as ERA5 measured it`
      : "Learned correction of the ERA5 split";
  }
  if (source.startsWith("erbs")) {
    return "Erbs (1982) correlation — outside the model’s training range";
  }
  return "ERA5 reanalysis, uncorrected";
}

function ContributionChart({ contribution }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current || !contribution) return undefined;
    const p = palette();
    const entries = LOSS_TERMS.map(({ key, label, term }) => ({
      label,
      term,
      value: contribution[key] ?? 0,
    }));
    const chart = new Chart(ref.current, {
      type: "bar",
      data: {
        labels: ["Share of modelled loss"],
        datasets: entries.map((entry) => ({
          label: entry.label,
          data: [entry.value],
          backgroundColor: lossColour(entry.term),
          // A 2px surface gap between segments, not a border around them.
          borderColor: cssVar("--bg-raised", "#11161f"),
          borderWidth: { top: 0, bottom: 0, left: 1, right: 1 },
          borderSkipped: false,
        })),
      },
      options: {
        ...baseOptions({ showLegend: true }),
        indexAxis: "y",
        plugins: {
          ...baseOptions({ showLegend: true }).plugins,
          directLabels: {
            // Below about 8% the segment is narrower than its own label, and
            // a clipped number reads as a rendering fault. Those terms are
            // named in the finding line and listed in the table instead.
            format: (value) => (value >= 8 ? Math.round(value) + "%" : ""),
            minWidth: 30,
            outsideWhenTight: false,
          },
          tooltip: {
            ...baseOptions().plugins.tooltip,
            callbacks: {
              label: (item) =>
                item.dataset.label +
                ": " +
                item.raw.toFixed(1) +
                "% of total loss",
            },
          },
        },
        scales: {
          x: {
            stacked: true,
            max: 100,
            ticks: { color: p.dim, callback: (v) => v + "%" },
            grid: { color: p.grid, drawTicks: false },
            border: { color: p.grid },
          },
          y: {
            stacked: true,
            ticks: { display: false },
            grid: { display: false },
          },
        },
      },
      plugins: [directLabels],
    });
    return () => chart.destroy();
  }, [contribution]);
  return <canvas ref={ref} />;
}

/**
 * The stage cascade, drawn as a waterfall of losses.
 *
 * This was five columns of running totals: 50,000 down to 48,900. Every bar
 * sat within 2% of its neighbour, so the figure read as "nothing happens"
 * while the number beneath it said a twentieth of the roof's output was gone.
 * Drawing each stage as a floating bar spanning [after, before] makes the
 * bar's height the loss itself, which is what the chart is about.
 */
function CascadeChart({ result }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current || !result) return undefined;
    const p = palette();
    const stages = cascadeStages(result);
    if (!stages.length) return undefined;

    const opts = baseOptions({ yTitle: "kWh remaining" });
    const chart = new Chart(ref.current, {
      type: "bar",
      data: {
        labels: stages.map((stage) => stage.label),
        datasets: [
          {
            data: stages.map((stage) => [stage.after, stage.before]),
            backgroundColor: stages.map((stage) => lossColour(stage.term)),
            borderWidth: 0,
            borderRadius: 4,
          },
        ],
      },
      options: {
        ...opts,
        plugins: {
          ...opts.plugins,
          directLabels: {
            // Always labelled, including a stage that costs nothing.
            //
            // A zero-loss stage draws a zero-height bar, so heat island
            // vanished from the waterfall while still holding an axis tick --
            // which reads as a rendering fault rather than as "this term cost
            // nothing here". The number says it plainly instead.
            format: (_value, _dataset, i) => {
              const pct = stages[i].lossPct;
              return (pct >= 0.005 ? "-" : "") + pct.toFixed(2) + "%";
            },
            minWidth: 9999,
          },
          tooltip: {
            ...opts.plugins.tooltip,
            callbacks: {
              title: (items) => stages[items[0].dataIndex].label,
              label: (item) => {
                const stage = stages[item.dataIndex];
                return [
                  fmt.num(Math.round(stage.before - stage.after)) +
                    " kWh lost here",
                  stage.lossPct.toFixed(2) + "% of the baseline",
                  fmt.num(Math.round(stage.after)) + " kWh still standing",
                ];
              },
            },
          },
        },
        scales: {
          ...opts.scales,
          y: {
            ...opts.scales.y,
            // Zoomed to the band the losses occupy rather than anchored at
            // zero. Anchoring at zero is right when the bar length IS the
            // quantity; here the quantity is the gap between two large
            // numbers, and a zero baseline hides it entirely.
            min: Math.min(...stages.map((stage) => stage.after)) * 0.997,
            ticks: {
              color: p.dim,
              font: { size: 11 },
              callback: (v) => fmt.num(Math.round(v)),
            },
          },
        },
      },
      plugins: [directLabels],
    });
    return () => chart.destroy();
  }, [result]);
  return <canvas ref={ref} />;
}

/**
 * Shade by time of day, over an explicit day/night track.
 *
 * The bars alone were ambiguous: a night interval and a fully sunlit interval
 * both drew nothing, and the caption promised greyed bars that a zero value
 * cannot produce. A background band now marks the hours with no sun above the
 * horizon, so "no sun" and "no shade" are different states on the page rather
 * than only in the prose.
 */
function ShadeChart({ intervals }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current || !intervals?.length) return undefined;
    const p = palette();
    const night = intervals.map((i) => i.sun_above_horizon === false);
    const values = intervals.map((i) => (i.shade_fraction ?? 0) * 100);
    const peak = Math.max(...values, 0.0001);

    const nightBand = {
      id: "nightBand",
      beforeDatasetsDraw(chart) {
        const { ctx, chartArea, scales } = chart;
        if (!chartArea) return;
        ctx.save();
        ctx.fillStyle = cssVar("--bg-inset", "#1a212d");
        const slot = chartArea.width / Math.max(intervals.length, 1);
        night.forEach((isNight, i) => {
          if (!isNight) return;
          const centre = scales.x.getPixelForValue(i);
          ctx.fillRect(
            centre - slot / 2,
            chartArea.top,
            slot,
            chartArea.height,
          );
        });
        ctx.restore();
      },
    };

    const opts = baseOptions({ yTitle: "Roof in shade (% of area)" });
    const chart = new Chart(ref.current, {
      type: "bar",
      data: {
        labels: intervals.map((i) => i.label_ist ?? i.label),
        datasets: [
          {
            data: values,
            backgroundColor: p.accent,
            borderWidth: 0,
            borderRadius: 4,
          },
        ],
      },
      options: {
        ...opts,
        plugins: {
          ...opts.plugins,
          directLabels: {
            format: (value, _d, i) => (night[i] ? "" : value.toFixed(2) + "%"),
            minWidth: 9999,
          },
          tooltip: {
            ...opts.plugins.tooltip,
            callbacks: {
              label: (item) =>
                night[item.dataIndex]
                  ? "Sun below the horizon, so shade is undefined here"
                  : item.raw.toFixed(2) + "% of roof area in shade",
            },
          },
        },
        scales: {
          ...opts.scales,
          y: { ...opts.scales.y, beginAtZero: true, suggestedMax: peak * 1.4 },
        },
      },
      plugins: [directLabels, nightBand],
    });
    return () => chart.destroy();
  }, [intervals]);
  return <canvas ref={ref} />;
}

export default function ResultPanel({
  result,
  series,
  error,
  busy,
  meta,
  request,
  onRetry,
}) {
  if (error) {
    const title = error.allowanceExhausted
      ? "Session allowance reached"
      : error.isConfiguration
        ? "Earth Engine is not configured on this server"
        : "Request did not complete";

    return (
      <div className="page">
        <div
          className={`callout ${error.allowanceExhausted ? "" : "callout--caution"}`}
        >
          <div className="callout__title">{title}</div>
          <p>{error.message}</p>

          {/* A configuration problem is not a fault to retry. Showing the
              server's own hint is the only useful thing here, and it is why
              the API distinguishes 503-with-a-hint from a generic 500. */}
          {error.hint && (
            <p style={{ fontSize: "0.9rem" }}>
              <strong>What to do:</strong> {error.hint}
            </p>
          )}

          {error.isConfiguration && (
            <p style={{ fontSize: "0.9rem", color: "var(--text-dim)" }}>
              Every other page on this site works without Earth Engine — the
              validation and model results are read from committed artifacts, so
              they need no credentials at all.
            </p>
          )}

          {error.requestId && (
            <p style={{ fontSize: "0.85rem", color: "var(--text-faint)" }}>
              Request id <code>{error.requestId}</code>
            </p>
          )}

          {error.isTransient && (
            <button className="btn" onClick={onRetry}>
              Try again
            </button>
          )}
        </div>
      </div>
    );
  }

  if (!result) {
    return (
      <div className="page">
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Nothing computed yet</h3>
          <p style={{ marginBottom: 0 }}>
            Pick a point and a window, then press{" "}
            <strong>Compute potential</strong>. Each run takes a few seconds —
            the work happens on Google&apos;s servers and comes back as a dozen
            small round-trips.
          </p>
        </div>
      </div>
    );
  }

  const quality = result.data_quality;
  const contribution = result.penalty_contribution;
  const soilingPenalty = result.soiling_penalty;

  return (
    <div className={`page result-enter${busy ? " result-stale" : ""}`}>
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          gap: "0.8rem",
          flexWrap: "wrap",
          marginBottom: "0.5rem",
        }}
      >
        <h2 style={{ margin: 0 }}>Result</h2>
        {severityBadge(quality)}
        {meta.cache && (
          <span className="badge">cache {meta.cache.toLowerCase()}</span>
        )}
        <span className="badge">
          {result.start_date} → {result.end_date_exclusive}
        </span>
      </div>

      <div className="hero-figure">
        <div className="hero-figure__value">
          {fmt.num(result.period_yield_kwh)}
        </div>
        <div className="hero-figure__label">
          kWh over the window, across {fmt.num(result.roof_area_m2)} m² of
          candidate roof
        </div>
      </div>

      <div className="grid grid--4">
        <div className="card">
          <div className="stat">
            <div className="stat__value">
              {fmt.pct(1 - result.combined_derate_factor, 2)}
            </div>
            <div className="stat__label">
              <Term k="heat island">Heat</Term> +{" "}
              <Term k="soiling">soiling</Term> loss
            </div>
          </div>
        </div>
        <div className="card">
          <div className="stat">
            <div className="stat__value">
              {fmt.pct(result.mean_shadow_fraction, 1)}
            </div>
            <div className="stat__label">Mean roof in shadow</div>
          </div>
        </div>
        <div className="card">
          <div className="stat">
            <div className="stat__value">
              {fmt.fixed(result.mean_sky_view_factor, 3)}
            </div>
            <div className="stat__label">
              <Term k="sky view factor">Sky view factor</Term>
            </div>
            <p className="stat__note">1.0 is an unobstructed horizon.</p>
          </div>
        </div>
        <div className="card">
          <div className="stat">
            <div className="stat__value">
              {fmt.fixed(result.beam_fraction, 3)}
            </div>
            <div className="stat__label">
              <Term k="beam fraction">Beam fraction</Term>
            </div>
            <p className="stat__note">{beamProvenance(result)}</p>
          </div>
        </div>
      </div>

      {/* Data quality first, before the charts. A degraded result that looks
          like a confident one is the failure mode this whole project has been
          unpicking. */}
      {quality?.findings?.length > 0 && (
        <div className="callout callout--caution">
          <div className="callout__title">
            {quality.findings.length} input
            {quality.findings.length === 1 ? "" : "s"} came from a fallback
          </div>
          <ul className="prose-list">
            {quality.findings.map((finding, i) => (
              <li key={i}>
                <strong>{finding.field ?? finding.source ?? "input"}</strong> —{" "}
                {finding.explanation ?? finding.message}
              </li>
            ))}
          </ul>
        </div>
      )}

      {quality?.coverage && quality.coverage.status !== "complete" && (
        <div className="callout callout--caution">
          <div className="callout__title">Partial coverage</div>
          <p>
            {quality.coverage.days_with_data} of{" "}
            {quality.coverage.days_requested} days had data. The window probably
            runs past the reanalysis publication lag, so this figure covers less
            than you asked for — which is why it is shown rather than quietly
            returned as a smaller number.
          </p>
        </div>
      )}

      <div className="grid grid--2">
        <Figure
          title="Where the energy goes"
          finding={cascadeFinding(result)}
          caption="Bar height is the loss at that stage, not the surviving total — the survivors differ by too little to see. The axis is zoomed to the band the losses occupy."
          columns={[
            { key: "stage", label: "Stage" },
            {
              key: "kwh",
              label: "kWh",
              numeric: true,
              render: (r) => fmt.num(r.kwh),
            },
          ]}
          rows={[
            { key: "b", stage: "Baseline", kwh: result.baseline_yield_kwh },
            {
              key: "s",
              stage: "After shadow",
              kwh: result.after_shadow_yield_kwh,
            },
            {
              key: "v",
              stage: "After sky view",
              kwh: result.after_svf_yield_kwh,
            },
            {
              key: "u",
              stage: "After heat island",
              kwh: result.after_uhi_yield_kwh,
            },
            { key: "n", stage: "Net", kwh: result.after_soiling_yield_kwh },
          ]}
        >
          <CascadeChart result={result} />
        </Figure>

        <Figure
          title="Which penalty dominates here"
          finding={contributionFinding(result.penalty_contribution)}
          caption="Share of total modelled loss. This split varies by city: geometry dominates in a dense core, soiling in an arid one."
          columns={[
            { key: "name", label: "Penalty" },
            {
              key: "pct",
              label: "Share",
              numeric: true,
              render: (r) => `${r.pct?.toFixed(1)}%`,
            },
            {
              key: "kwh",
              label: "kWh lost",
              numeric: true,
              render: (r) => fmt.num(r.kwh),
            },
          ]}
          rows={[
            {
              key: "sh",
              name: "Shadow",
              pct: contribution?.shadow_contribution_pct,
              kwh: contribution?.shadow_loss_kwh,
            },
            {
              key: "sv",
              name: "Sky view",
              pct: contribution?.svf_contribution_pct,
              kwh: contribution?.svf_loss_kwh,
            },
            {
              key: "uh",
              name: "Heat island",
              pct: contribution?.uhi_contribution_pct,
              kwh: contribution?.uhi_loss_kwh,
            },
            {
              key: "so",
              name: "Soiling",
              pct: contribution?.soiling_contribution_pct,
              kwh: contribution?.soiling_loss_kwh,
            },
          ]}
        >
          <ContributionChart contribution={contribution} />
        </Figure>
      </div>

      {result.shade_intervals?.length > 0 && (
        <Figure
          title="Shade through the day"
          finding={shadeFinding(result.shade_intervals)}
          caption="Local time, not UTC. Shaded bands mark intervals with no sun above the horizon, where shade is undefined rather than zero."
          columns={[
            { key: "label_ist", label: "Interval (IST)" },
            { key: "label_utc", label: "UTC" },
            {
              key: "shade_fraction",
              label: "In shade",
              numeric: true,
              render: (r) => fmt.pct(r.shade_fraction, 1),
            },
          ]}
          rows={result.shade_intervals.map((interval, i) => ({
            key: i,
            ...interval,
          }))}
        >
          <ShadeChart intervals={result.shade_intervals} />
        </Figure>
      )}

      {soilingPenalty && (
        <div className="card" style={{ marginTop: "1.5rem" }}>
          <h3 style={{ marginTop: 0 }}>Soiling, in detail</h3>
          <div className="grid grid--4">
            <div className="stat">
              <div className="stat__value">
                {fmt.pct(soilingPenalty.soiling_loss_fraction, 2)}
              </div>
              <div className="stat__label">
                <Term k="soiling">Soiling</Term> loss
              </div>
            </div>
            <div className="stat">
              <div className="stat__value">
                {soilingPenalty.cleaning_rain_days}
              </div>
              <div className="stat__label">Cleaning-rain days</div>
            </div>
            <div className="stat">
              <div className="stat__value">
                {fmt.fixed(soilingPenalty.mean_dry_spell_days, 1)}d
              </div>
              <div className="stat__label">Mean dry spell</div>
            </div>
            <div className="stat">
              <div className="stat__value">
                {fmt.pct(
                  1 - (soilingPenalty.legacy_retention_for_comparison ?? 1),
                  2,
                )}
              </div>
              <div className="stat__label">Previous model said</div>
            </div>
          </div>
          {soilingPenalty.notes?.length > 0 && (
            <ul
              className="prose-list"
              style={{
                marginTop: "1rem",
                fontSize: "0.9rem",
                color: "var(--text-dim)",
              }}
            >
              {soilingPenalty.notes.map((note, i) => (
                <li key={i}>{note}</li>
              ))}
            </ul>
          )}
          <p className="source-note">
            Rainfall from {soilingPenalty.rainfall_source}, dry spells computed
            by the <code>{soilingPenalty.method}</code> path. Model:{" "}
            <code>{soilingPenalty.model}</code>.
          </p>
        </div>
      )}

      {series?.points?.length > 0 && <SeriesFigure series={series} />}

      <PayloadInspector request={request} response={result} meta={meta} />

      <p className="source-note">
        This result is reproducible by link — the URL carries the query. The
        panel above holds the exact request and response, and a curl command
        that reproduces it outside the browser.
      </p>
    </div>
  );
}

/**
 * The spread across the window: which period generates most and least.
 *
 * The ratio is the useful number. A roof whose best month produces twice its
 * worst behaves very differently for a household sizing a system against a
 * monthly bill than one that is flat across the year.
 */
function seriesFinding(series) {
  const points = (series?.points ?? [])
    .map((pt) => ({
      label: pt.label ?? pt.period,
      value: pt.yield_kwh ?? pt.period_yield_kwh ?? 0,
    }))
    .filter((pt) => pt.value > 0);
  if (points.length < 2) return null;
  const peak = points.reduce((a, b) => (b.value > a.value ? b : a));
  const low = points.reduce((a, b) => (b.value < a.value ? b : a));
  const ratio = peak.value / low.value;
  return (
    <>
      <strong>
        {peak.label} generates {ratio.toFixed(1)}× as much as {low.label}
      </strong>{" "}
      — {fmt.num(Math.round(peak.value))} kWh against{" "}
      {fmt.num(Math.round(low.value))} kWh. A system sized to the average will
      overproduce in the best period and fall short in the worst.
    </>
  );
}

function SeriesFigure({ series }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current || !series?.points?.length) return undefined;
    const p = palette();
    const chart = new Chart(ref.current, {
      type: "line",
      data: {
        labels: series.points.map((pt) => pt.label ?? pt.period),
        datasets: [
          {
            label: "Net yield",
            data: series.points.map(
              (pt) => pt.yield_kwh ?? pt.period_yield_kwh,
            ),
            borderColor: p.accent,
            backgroundColor: `color-mix(in srgb, ${p.accent} 18%, transparent)`,
            fill: true,
            tension: 0.28,
            pointRadius: 3,
          },
        ],
      },
      options: {
        ...baseOptions({ yTitle: "kWh" }),
        // Index mode: the whole column answers "what was it then", which is
        // the question a time series is for.
        interaction: { mode: "index", intersect: false },
      },
      plugins: [crosshair],
    });
    const releaseScrub = enableKeyboardScrub(
      chart,
      ref.current,
      "Generation across the window",
    );
    return () => {
      releaseScrub();
      chart.destroy();
    };
  }, [series]);

  return (
    <Figure
      title="Generation across the window"
      finding={seriesFinding(series)}
      caption="Net yield per sub-period after every loss term. Drag across the chart, or focus it and use the arrow keys, to read each period."
      columns={[
        { key: "label", label: "Period" },
        {
          key: "yield_kwh",
          label: "kWh",
          numeric: true,
          render: (r) => fmt.num(r.yield_kwh ?? r.period_yield_kwh),
        },
      ]}
      rows={series.points.map((pt, i) => ({ key: i, ...pt }))}
    >
      <canvas ref={ref} />
    </Figure>
  );
}
