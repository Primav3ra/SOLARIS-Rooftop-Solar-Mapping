"""
Data-quality reporting: making a degraded result visibly degraded.

The problem
-----------
Every data path in the model has a fallback that returns a *plausible* number
when the real one is unavailable, distinguishable only by a ``source`` string
buried in a nested dict:

============================  ==============================================
``fallback_zero``             GHI reduction found no valid pixels -> 0.0
``fallback_no_sample``        beam fraction sampling failed -> 0.60
``fallback_null_band``        beam fraction band was null -> 0.60
``fallback_zero`` (UHI)       LST anomaly unavailable -> 0.0 (no derate)
``fallback_urban_midpoint``   MAIAC AOD unavailable -> 0.50
``aoi_fallback``              no building polygon found -> the whole AOI
empty shade bucket            no sun in the bucket -> reported as 0.0 shade
============================  ==============================================

A caller that does not go looking for those strings cannot tell a computed
result from a stack of defaults. The empty-shade-bucket case is the sharpest:
"0.0 shade" and "no sun at all" are opposite meanings reported identically.

The ``aoi_fallback`` is the most consequential: it silently swaps one roof for
a ~2 km AOI box, changing the denominator of every per-building figure by
orders of magnitude.

This module collects those signals into one top-level ``data_quality`` block so
a consumer sees the degradation without knowing where to look, and so the
frontend can mark a result as provisional.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    """How much a degradation should be trusted."""

    #: Everything was computed from real data.
    OK = "ok"
    #: A documented default stood in for something minor; the result is usable
    #: but not fully data-driven.
    DEGRADED = "degraded"
    #: A substituted value changes the meaning of the result. Do not quote the
    #: number without the caveat.
    UNRELIABLE = "unreliable"


#: Maps a ``source`` tag to its severity and a caller-facing explanation. Any
#: tag not listed here is treated as a real computation.
FALLBACK_CATALOGUE: dict[str, tuple[Severity, str]] = {
    "fallback_zero": (
        Severity.UNRELIABLE,
        "Irradiance reduction found no valid pixels and returned zero. Any "
        "energy figure derived from it is zero for the wrong reason.",
    ),
    "no_sample": (
        Severity.UNRELIABLE,
        "Point sampling returned nothing, so irradiance defaulted to zero.",
    ),
    "null_band": (
        Severity.UNRELIABLE,
        "The sampled band was null, so irradiance defaulted to zero.",
    ),
    "fallback_no_sample": (
        Severity.DEGRADED,
        "Beam fraction could not be sampled; substituted the 0.60 annual "
        "midpoint. Reference data puts the Indian mean nearer 0.54, so this "
        "over-attributes energy to the direct beam and over-applies shadowing.",
    ),
    "fallback_null_band": (
        Severity.DEGRADED,
        "The beam-fraction band was null; substituted the 0.60 annual midpoint.",
    ),
    "fallback_urban_midpoint": (
        Severity.DEGRADED,
        "No valid MAIAC aerosol retrievals; substituted an AOD of 0.50. "
        "Soiling loss is therefore assumed rather than measured.",
    ),
    "aoi_fallback": (
        Severity.UNRELIABLE,
        "No Open Buildings footprint was found at the selected point, so the "
        "whole area of interest was used instead of one roof. Per-building "
        "figures refer to the entire AOI and are not comparable to a single "
        "building.",
    ),
    "centroid_sample": (
        Severity.DEGRADED,
        "The area reduction failed, so a single centroid pixel stood in for "
        "the whole area of interest.",
    ),
    "reduceRegion_bestEffort": (
        Severity.DEGRADED,
        "The reduction was coarsened to stay inside Earth Engine's pixel "
        "limit, so the result is computed at lower resolution than requested.",
    ),
}


@dataclass
class Finding:
    """One thing that was substituted, coarsened or missing."""

    field: str
    source: str
    severity: Severity
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {
            "field": self.field,
            "source": self.source,
            "severity": self.severity.value,
            "detail": self.detail,
        }


@dataclass
class DataQuality:
    """
    Accumulates quality findings over the course of one request.

    Built up as each data source is consulted, then serialised into the
    response under ``data_quality``.
    """

    findings: list[Finding] = field(default_factory=list)
    coverage: dict[str, object] | None = None
    notes: list[str] = field(default_factory=list)

    # -- recording --------------------------------------------------------

    def record_source(self, field_name: str, source: str | None) -> None:
        """
        Note the provenance of one value.

        A ``source`` absent from :data:`FALLBACK_CATALOGUE` is a real
        computation and records nothing, which keeps a clean response clean.
        """
        if not source:
            return
        entry = FALLBACK_CATALOGUE.get(source)
        if entry is None:
            return
        severity, detail = entry
        self.findings.append(
            Finding(field=field_name, source=source, severity=severity, detail=detail)
        )

    def record_stats(self, field_name: str, stats: dict | None) -> None:
        """Record from a stats dict that carries its own ``source`` key."""
        if isinstance(stats, dict):
            self.record_source(field_name, stats.get("source"))

    def record_empty_shade_buckets(self, labels: list[str]) -> None:
        """
        Flag buckets that contain no sun.

        Without this a bucket with no sun above the horizon reports
        ``shade_fraction = 0.0``, which reads as "fully sunlit" -- the opposite
        of what it means.
        """
        if not labels:
            return
        self.findings.append(
            Finding(
                field="shade_intervals",
                source="empty_bucket",
                severity=Severity.DEGRADED,
                detail=(
                    f"No sun above the altitude floor in bucket(s) {', '.join(labels)}. "
                    "These report a shade fraction of 0.0, which means 'no sun', "
                    "not 'no shade'."
                ),
            )
        )

    def record_clamp(self, field_name: str, detail: str) -> None:
        """Flag a value that hit a guard rail."""
        self.findings.append(
            Finding(
                field=field_name,
                source="clamped",
                severity=Severity.DEGRADED,
                detail=detail,
            )
        )

    def note(self, message: str) -> None:
        self.notes.append(message)

    def set_coverage(self, coverage: dict[str, object] | None) -> None:
        self.coverage = coverage
        if coverage and coverage.get("status") == "partial":
            self.findings.append(
                Finding(
                    field="temporal_window",
                    source="partial_coverage",
                    severity=Severity.UNRELIABLE,
                    detail=str(coverage.get("warning") or "Window is only partly covered."),
                )
            )
        elif coverage and coverage.get("status") == "no_data":
            self.findings.append(
                Finding(
                    field="temporal_window",
                    source="no_coverage",
                    severity=Severity.UNRELIABLE,
                    detail=str(coverage.get("warning") or "No data for this window."),
                )
            )

    # -- derived ----------------------------------------------------------

    @property
    def severity(self) -> Severity:
        """The worst severity recorded."""
        if any(f.severity is Severity.UNRELIABLE for f in self.findings):
            return Severity.UNRELIABLE
        if self.findings:
            return Severity.DEGRADED
        return Severity.OK

    @property
    def is_fully_computed(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict[str, object]:
        return {
            "severity": self.severity.value,
            "fully_computed": self.is_fully_computed,
            "findings": [f.as_dict() for f in self.findings],
            "coverage": self.coverage,
            "notes": self.notes,
            "summary": self.summary,
        }

    @property
    def summary(self) -> str:
        if not self.findings:
            return "All inputs computed from data."
        unreliable = [f for f in self.findings if f.severity is Severity.UNRELIABLE]
        if unreliable:
            fields = ", ".join(sorted({f.field for f in unreliable}))
            return (
                f"Result is unreliable: substituted or missing data for {fields}. "
                "Do not quote these figures without the caveat."
            )
        fields = ", ".join(sorted({f.field for f in self.findings}))
        return f"Result is usable but not fully data-driven; defaults used for {fields}."
