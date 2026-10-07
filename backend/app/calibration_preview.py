"""Read-only calibration preview.

The preview answers "what would the first window of this recording look like
with candidate gain/offset/phase coefficients?" using exactly the same block
reader (``group_chunks_by_rate``) and calibration/DSP formulas
(``calibrate_series`` / ``harmonic_analysis``) as a real analysis task.

It never writes: no AnalysisTask, no Report and no CalibrationVersion row is
created, and the database session is only read. Missing blocks, windows that
cross a sample-rate boundary and non-integer-cycle windows are reported as
explicit diagnostics instead of being silently repaired.
"""

from __future__ import annotations

import hashlib
import math
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .dsp import calibrate_series, default_params, group_chunks_by_rate, harmonic_analysis
from .models import CalibrationVersion, Chunk, Manifest
from .storage import get_object_store

# Sample times are floating point second offsets; this tolerance is far below
# one sample interval at the supported rates.
TIME_EPSILON = 1e-9


class CalibrationPreviewError(ValueError):
    """Request-level problem that should be surfaced as HTTP 422."""


def _diagnostic(severity: str, code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"severity": severity, "code": code, "message": message, "details": details}


def _declared_segments(manifest: Manifest) -> list[dict[str, Any]]:
    """Timeline of the declaration, including chunks that were never uploaded.

    Same-rate adjacent blocks form one physical segment, exactly as the real
    pipeline groups them; a rate change starts a new segment. Each entry also
    carries the per-chunk starting offsets so the window can later be sliced
    from assembled data.
    """

    ordered = sorted(manifest.expected_chunks, key=lambda item: int(item["sequence"]))
    segments: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    offset = 0.0
    for item in ordered:
        fs = float(item["sample_rate"])
        count = int(item["sample_count"])
        duration = count / fs
        if current is None or not math.isclose(current["sample_rate"], fs, rel_tol=0.0):
            current = {
                "sample_rate": fs,
                "channels": list(item["channels"]),
                "start_seconds": offset,
                "items": [],
            }
            segments.append(current)
        current["items"].append(
            {
                "sequence": int(item["sequence"]),
                "meta": item,
                "start_seconds": offset,
                "duration": duration,
            }
        )
        offset += duration
    for segment in segments:
        segment["end_seconds"] = segment["start_seconds"] + sum(
            entry["duration"] for entry in segment["items"]
        )
    return segments


def _intersects_segment(segment: dict[str, Any], start: float, end: float) -> bool:
    return end > segment["start_seconds"] + TIME_EPSILON and start < segment["end_seconds"] - TIME_EPSILON


def _wrap_phase(value: float) -> float:
    return (value + math.pi) % (2.0 * math.pi) - math.pi


def _rms_only_metrics(calibrated: np.ndarray) -> dict[str, Any]:
    """Same RMS/DC formulas as harmonic_analysis, without a DFT fundamental.

    Used when the window cannot resolve the configured fundamental frequency;
    the time-domain true RMS remains exact and comparable across coefficients.
    """

    return {
        "rms": float(np.sqrt(np.mean(np.square(calibrated)))),
        "dc": float(np.mean(calibrated)),
        "fundamental": {"frequency_hz": None, "rms": None, "phase_rad": None, "phase_deg": None},
        "thd_percent": None,
        "harmonics": [],
        "quality": [],
    }


def build_calibration_preview(
    db: Session,
    manifest: Manifest,
    *,
    start_seconds: float,
    duration_seconds: float | None,
    end_seconds: float | None,
    candidate_coefficients: dict[str, dict[str, Any]],
    baseline_calibration_version_id: str | None,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    diagnostics: list[dict[str, Any]] = []

    if (duration_seconds is None) == (end_seconds is None):
        raise CalibrationPreviewError(
            "provide exactly one of duration_seconds or end_seconds"
        )
    window_start = float(start_seconds)
    window_end = float(end_seconds) if end_seconds is not None else window_start + float(duration_seconds)
    if window_end <= window_start:
        raise CalibrationPreviewError("preview window must have a positive duration")

    analysis_params = {**default_params(), **(params or {})}

    if set(candidate_coefficients) != set(manifest.channel_set):
        raise CalibrationPreviewError(
            "candidate coefficients must cover exactly the manifest channel set; "
            f"expected {sorted(manifest.channel_set)}, got {sorted(candidate_coefficients)}"
        )

    baseline: CalibrationVersion | None = None
    if baseline_calibration_version_id:
        baseline = db.get(CalibrationVersion, baseline_calibration_version_id)
        if baseline is None:
            raise CalibrationPreviewError("baseline calibration version not found")
        if baseline.channel_set_hash != manifest.channel_set_hash:
            raise CalibrationPreviewError(
                "baseline calibration does not belong to this manifest channel set"
            )
        missing = [channel for channel in manifest.channel_set if channel not in baseline.coefficients]
        if missing:
            raise CalibrationPreviewError(
                f"baseline calibration is missing coefficients for {sorted(missing)}"
            )

    if manifest.status != "completed":
        diagnostics.append(
            _diagnostic(
                "warning",
                "manifest_not_completed",
                "manifest has not passed immutable upload completion; preview only covers received blocks",
                status=manifest.status,
            )
        )

    declared_segments = _declared_segments(manifest)
    total_duration = declared_segments[-1]["end_seconds"] if declared_segments else 0.0
    if window_start < -TIME_EPSILON or window_end > total_duration + TIME_EPSILON:
        raise CalibrationPreviewError(
            f"window [{window_start}, {window_end}] is outside the recording "
            f"(duration {total_duration:.9f}s)"
        )

    touched_segments = [
        segment for segment in declared_segments if _intersects_segment(segment, window_start, window_end)
    ]
    needed_sequences = {
        int(entry["sequence"])
        for segment in touched_segments
        for entry in segment["items"]
        if window_end > entry["start_seconds"] + TIME_EPSILON
        and window_start < entry["start_seconds"] + entry["duration"] - TIME_EPSILON
    }

    received = {
        chunk.sequence: chunk
        for chunk in db.scalars(
            select(Chunk).where(Chunk.manifest_id == manifest.id)
        ).all()
    }

    missing_in_window = sorted(needed_sequences - set(received))
    if missing_in_window:
        diagnostics.append(
            _diagnostic(
                "error",
                "missing_chunk",
                "declared blocks inside the preview window have not been uploaded; "
                "samples cannot be inferred and no metric is computed",
                sequences=missing_in_window,
            )
        )

    # Holes elsewhere still matter: the timeline offset is built from the
    # declaration, but a gap outside the window means this is not one verified
    # continuous recording. It does not block window math.
    declared_sequences = {int(item["sequence"]) for item in manifest.expected_chunks}
    missing_anywhere = sorted(declared_sequences - set(received))
    outside_missing = [seq for seq in missing_anywhere if seq not in missing_in_window]
    if outside_missing:
        diagnostics.append(
            _diagnostic(
                "warning",
                "missing_chunk_outside_window",
                "declared blocks outside the preview window are missing; they do not enter the metrics",
                sequences=outside_missing,
            )
        )

    if len(touched_segments) > 1:
        rates = [segment["sample_rate"] for segment in touched_segments]
        diagnostics.append(
            _diagnostic(
                "error",
                "sample_rate_cross_segment",
                "window crosses a sample-rate boundary; the platform never resamples or joins rates, "
                "choose a window inside one constant-rate segment",
                sample_rates=rates,
                boundary_seconds=touched_segments[0]["end_seconds"],
            )
        )
    elif len(touched_segments) == 1 and len(declared_segments) > 1:
        diagnostics.append(
            _diagnostic(
                "warning",
                "sample_rate_changed",
                "recording contains multiple sampling-rate segments; this preview evaluates the touched segment only",
                sample_rates=[segment["sample_rate"] for segment in declared_segments],
            )
        )

    has_error = any(item["severity"] == "error" for item in diagnostics)
    channels_out: list[dict[str, Any]] = []
    window_info: dict[str, Any] = {}

    if not has_error and touched_segments:
        segment_decl = touched_segments[0]
        fs = segment_decl["sample_rate"]
        channels = segment_decl["channels"]
        start_index = max(0, int(round((window_start - segment_decl["start_seconds"]) * fs)))
        end_index = min(
            sum(int(entry["meta"]["sample_count"]) for entry in segment_decl["items"]),
            int(math.ceil((window_end - segment_decl["start_seconds"]) * fs - TIME_EPSILON)),
        )
        sample_count = max(0, end_index - start_index)
        window_info = {
            "sample_rate": fs,
            "start_seconds": segment_decl["start_seconds"] + start_index / fs,
            "end_seconds": segment_decl["start_seconds"] + end_index / fs,
            "samples": sample_count,
            "sequences": [
                entry["sequence"]
                for entry in segment_decl["items"]
                if entry["start_seconds"] + entry["duration"] > window_start + TIME_EPSILON
                and entry["start_seconds"] < window_end - TIME_EPSILON
            ],
        }

        cycles = int(analysis_params["cycles_per_window"])
        f0 = float(analysis_params["fundamental_hz"])
        expected_n = int(round(cycles * fs / f0))
        fundamental_resolvable = 0 < int(round(f0 * sample_count / fs)) < sample_count // 2 + 1
        if sample_count != expected_n:
            diagnostics.append(
                _diagnostic(
                    "warning",
                    "non_integer_cycle",
                    "window length is not an exact integer-cycle record; metrics are diagnostic and "
                    "differ from formal integer-cycle results",
                    samples=sample_count,
                    expected_samples=expected_n,
                )
            )
        if not fundamental_resolvable:
            diagnostics.append(
                _diagnostic(
                    "error",
                    "fundamental_unresolvable",
                    "window is too short (or the rate too low) to place the configured fundamental "
                    "on a DFT bin; RMS is still computed but the fundamental phase cannot be",
                    samples=sample_count,
                    sample_rate=fs,
                    fundamental_hz=f0,
                    minimum_samples=int(math.ceil(fs / f0)),
                )
            )
        if f0 >= fs / 2.0:
            diagnostics.append(
                _diagnostic(
                    "error",
                    "fundamental_above_nyquist",
                    "configured fundamental frequency is at or above the segment Nyquist frequency",
                    sample_rate=fs,
                    nyquist_hz=fs / 2.0,
                    fundamental_hz=f0,
                )
            )

        # Only assemble the blocks this window actually touches, in declaration
        # order. group_chunks_by_rate demands contiguous sequences, which holds
        # inside one segment after the missing-block check.
        ordered_meta = [
            {
                "sequence": entry["sequence"],
                "sha256": entry["meta"]["sha256"],
                "byte_offset": entry["meta"]["byte_offset"],
                "byte_length": entry["meta"]["byte_length"],
                "sample_count": entry["meta"]["sample_count"],
                "sample_rate": entry["meta"]["sample_rate"],
                "channels": list(entry["meta"]["channels"]),
                "start_time": entry["meta"]["start_time"],
                "end_time": entry["meta"]["end_time"],
                "encoding": entry["meta"].get("encoding", "float32le-interleaved"),
                "object_key": received[int(entry["sequence"])].object_key,
                "_start_seconds": entry["start_seconds"],
            }
            for entry in segment_decl["items"]
            if int(entry["sequence"]) in needed_sequences
        ]

        store = get_object_store()
        settings = get_settings()
        Path(settings.spool_dir).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=settings.spool_dir, prefix="cal-preview-") as scratch:
            raw_dir = Path(scratch) / "raw"

            def fetch_raw(item: dict[str, Any]) -> str:
                target = raw_dir / f"{int(item['sequence']):09d}.bin"
                store.get_to_path(item["object_key"], target)
                digest = hashlib.sha256()
                with target.open("rb") as block_file:
                    for block in iter(lambda: block_file.read(8 * 1024 * 1024), b""):
                        digest.update(block)
                if digest.hexdigest() != item["sha256"]:
                    raise CalibrationPreviewError(
                        f"object digest mismatch at sequence {int(item['sequence'])}"
                    )
                return str(target)

            assembled = group_chunks_by_rate(ordered_meta, fetch_raw=fetch_raw)
            try:
                data = assembled[0]["data"]
                assembled_start_seconds = min(float(entry["_start_seconds"]) for entry in ordered_meta)
                local_start = start_index - int(round((assembled_start_seconds - segment_decl["start_seconds"]) * fs))
                local_end = local_start + sample_count
                if local_start < 0 or local_end > data.shape[0]:
                    # Defensive: window arithmetic should always stay inside.
                    raise CalibrationPreviewError("window slice fell outside assembled blocks")

                candidate_sets: list[tuple[str | None, dict[str, dict[str, Any]]]] = [
                    (
                        baseline.id if baseline is not None else None,
                        dict(baseline.coefficients) if baseline is not None else {
                            channel: {"gain": 1.0, "offset": 0.0, "phase_shift_rad": 0.0}
                            for channel in channels
                        },
                    ),
                    ("candidate", dict(candidate_coefficients)),
                ]
                evaluated: list[dict[str, Any]] = []
                for label, coefficient_set in candidate_sets:
                    per_channel: dict[str, dict[str, Any]] = {}
                    for channel_index, channel in enumerate(channels):
                        raw_series = np.asarray(data[local_start:local_end, channel_index])
                        calibrated = calibrate_series(raw_series, coefficient_set[channel])
                        metrics: dict[str, Any]
                        if fundamental_resolvable and f0 < fs / 2.0:
                            # Same formula path as the formal pipeline; saturation
                            # limits belong to frozen task params and are not probed.
                            try:
                                metrics = harmonic_analysis(
                                    calibrated, fs, analysis_params,
                                    complete_window=sample_count == expected_n,
                                )
                            except ValueError as exc:
                                metrics = _rms_only_metrics(calibrated)
                                diagnostics.append(
                                    _diagnostic(
                                        "error",
                                        "fundamental_unresolvable",
                                        f"fundamental analysis failed: {exc}",
                                        channel=channel,
                                    )
                                )
                        else:
                            metrics = _rms_only_metrics(calibrated)
                        # The window-level non_integer_cycle item is emitted once
                        # above; keep only channel-specific findings here.
                        for quality in metrics["quality"]:
                            if quality["code"] != "non_integer_cycle":
                                diagnostics.append({"channel": channel, **quality})
                        per_channel[channel] = metrics
                    evaluated.append({"label": label, "channels": per_channel})

                baseline_result, candidate_result = evaluated
                for channel in channels:
                    base_metrics = baseline_result["channels"][channel]
                    cand_metrics = candidate_result["channels"][channel]
                    base_phase = base_metrics["fundamental"]["phase_rad"]
                    cand_phase = cand_metrics["fundamental"]["phase_rad"]
                    channels_out.append(
                        {
                            "channel": channel,
                            "baseline": {
                                "rms": base_metrics["rms"],
                                "dc": base_metrics["dc"],
                                "fundamental_phase_rad": base_phase,
                                "fundamental_phase_deg": base_metrics["fundamental"]["phase_deg"],
                                "fundamental_rms": base_metrics["fundamental"]["rms"],
                            },
                            "candidate": {
                                "rms": cand_metrics["rms"],
                                "dc": cand_metrics["dc"],
                                "fundamental_phase_rad": cand_phase,
                                "fundamental_phase_deg": cand_metrics["fundamental"]["phase_deg"],
                                "fundamental_rms": cand_metrics["fundamental"]["rms"],
                            },
                            "delta": {
                                "rms": cand_metrics["rms"] - base_metrics["rms"],
                                "rms_ratio": (
                                    cand_metrics["rms"] / base_metrics["rms"]
                                    if base_metrics["rms"] > 1e-12
                                    else None
                                ),
                                "dc": cand_metrics["dc"] - base_metrics["dc"],
                                "fundamental_rms": (
                                    None
                                    if cand_metrics["fundamental"]["rms"] is None
                                    or base_metrics["fundamental"]["rms"] is None
                                    else cand_metrics["fundamental"]["rms"]
                                    - base_metrics["fundamental"]["rms"]
                                ),
                                "fundamental_phase_rad": (
                                    None
                                    if base_phase is None or cand_phase is None
                                    else _wrap_phase(cand_phase - base_phase)
                                ),
                                "fundamental_phase_deg": (
                                    None
                                    if base_phase is None or cand_phase is None
                                    else math.degrees(_wrap_phase(cand_phase - base_phase))
                                ),
                            },
                        }
                    )
            finally:
                for segment in assembled:
                    segment["data"]._mmap.close()

    # Baseline and candidate reuse the same DSP path and can emit identical
    # channel diagnostics (e.g. off-bin fundamental); keep each once.
    unique_diagnostics: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in diagnostics:
        key = repr((item.get("channel"), item["severity"], item["code"], item.get("message"), item.get("details")))
        if key not in seen:
            seen.add(key)
            unique_diagnostics.append(item)
    diagnostics = unique_diagnostics

    status = "error" if any(item["severity"] == "error" for item in diagnostics) else (
        "warning" if any(item["severity"] == "warning" for item in diagnostics) else "ok"
    )
    return {
        "kind": "calibration_preview",
        "manifest_id": manifest.id,
        "manifest_status": manifest.status,
        "baseline_calibration_version_id": baseline.id if baseline is not None else None,
        "window": {
            "requested_start_seconds": window_start,
            "requested_end_seconds": window_end,
            **window_info,
        },
        "params": analysis_params,
        "candidate_coefficients": {
            channel: dict(coefficient) for channel, coefficient in candidate_coefficients.items()
        },
        "channels": channels_out,
        "diagnostics": diagnostics,
        "status": status,
        "persisted": False,
    }
