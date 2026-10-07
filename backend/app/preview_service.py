"""Read-only calibration preview.

A calibration preview reuses the exact block reading path
(`group_chunks_by_rate`) and the exact calibration formula
(`y = gain*x + offset` plus the constant `phase_shift_rad` DFT rotation) used
by formal analysis, but applies them to one short window of an already
completed manifest with *candidate* coefficients.

It never creates an AnalysisTask, a Report, or a CalibrationVersion, and it
never writes to the database. Missing blocks, windows that cross a sample-rate
boundary, and non-integer-cycle windows get explicit diagnostics instead of
silent resampling or zero padding.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .dsp import calibrate_series, group_chunks_by_rate, harmonic_analysis, wrap_phase
from .models import CalibrationVersion, Chunk, Manifest
from .storage import get_object_store

DEFAULT_DURATION_SECONDS = 0.12  # six 50 Hz cycles at common lab rates


def _diag(severity: str, code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"severity": severity, "code": code, "message": message, "details": details}


def _chunk_meta(chunk: Chunk) -> dict[str, Any]:
    return {
        "sequence": chunk.sequence,
        "sha256": chunk.sha256,
        "byte_offset": chunk.byte_offset,
        "byte_length": chunk.byte_length,
        "sample_count": chunk.sample_count,
        "sample_rate": chunk.sample_rate,
        "channels": list(chunk.channels),
        "start_time": chunk.start_time,
        "end_time": chunk.end_time,
        "encoding": chunk.encoding,
        "object_key": chunk.object_key,
    }


def _rate_segments(expected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split the declared chunk timeline into constant-rate segments.

    Sample times are contiguous across chunks, so a segment's clock is the
    cumulative sample count of its constant-rate chunks. A rate change starts
    a new segment with its own zero-second origin; clocks are never joined.
    """

    segments: list[dict[str, Any]] = []
    for entry in expected:
        rate = float(entry["sample_rate"])
        count = int(entry["sample_count"])
        if segments and abs(segments[-1]["sample_rate"] - rate) < 1e-12:
            current = segments[-1]
            current["sequences"].append(int(entry["sequence"]))
            current["sample_count"] += count
        else:
            segments.append(
                {
                    "sample_rate": rate,
                    "sequences": [int(entry["sequence"])],
                    "sample_count": count,
                    "segment_index": len(segments),
                }
            )
    cumulative = 0.0
    for segment in segments:
        segment["duration_seconds"] = segment["sample_count"] / segment["sample_rate"]
        segment["start_seconds"] = cumulative
        segment["end_seconds"] = cumulative + segment["duration_seconds"]
        cumulative += segment["duration_seconds"]
    return segments


def _plan_window(
    expected: list[dict[str, Any]],
    *,
    start_seconds: float,
    duration_seconds: float | None,
    sample_count: int | None,
    fundamental_hz: float,
    cycles_per_window: int,
    recording_seconds: float,
) -> dict[str, Any]:
    """Locate the preview window on the declared timeline and diagnose it."""

    diagnostics: list[dict[str, Any]] = []
    if start_seconds >= recording_seconds:
        return {
            "ok": False,
            "status": "error",
            "diagnostics": [
                _diag(
                    "error",
                    "preview_window_out_of_range",
                    "window start is at or beyond the end of the recording",
                    start_seconds=start_seconds,
                    recording_seconds=recording_seconds,
                )
            ],
        }

    segments = _rate_segments(expected)
    # A start exactly on a boundary belongs to the segment that follows it; the
    # earlier start>=recording_seconds check rejects a start past the final end.
    segment = next(
        (
            item
            for item in segments
            if item["start_seconds"] - 1e-12 <= start_seconds < item["end_seconds"] - 1e-12
        ),
        None,
    )
    if segment is None:
        segment = next(
            (item for item in segments if abs(start_seconds - item["start_seconds"]) < 1e-12),
            None,
        )
    if segment is None:
        return {
            "ok": False,
            "status": "error",
            "diagnostics": [
                _diag(
                    "error",
                    "preview_window_out_of_range",
                    "window start is outside the recording timeline",
                )
            ],
        }

    rate = segment["sample_rate"]
    local_start = start_seconds - segment["start_seconds"]
    if sample_count is not None:
        window_samples = int(sample_count)
        duration = window_samples / rate
    else:
        duration = float(duration_seconds or DEFAULT_DURATION_SECONDS)
        window_samples = int(round(duration * rate))
    window_samples = max(1, window_samples)
    duration = window_samples / rate
    end_seconds = start_seconds + duration

    if end_seconds > segment["end_seconds"] + 1e-9:
        boundary = next(
            (item for item in segments if abs(item["start_seconds"] - segment["end_seconds"]) < 1e-12),
            None,
        )
        if boundary is not None and end_seconds > boundary["start_seconds"] + 1e-9:
            return {
                "ok": False,
                "status": "error",
                "diagnostics": [
                    _diag(
                        "error",
                        "preview_crosses_rate_boundary",
                        "preview window crosses a sample-rate change; segments are analyzed separately and never interpolated",
                        start_seconds=start_seconds,
                        end_seconds=end_seconds,
                        boundary_seconds=segment["end_seconds"],
                        sample_rates=sorted({rate, boundary["sample_rate"]}),
                    )
                ],
            }
        return {
            "ok": False,
            "status": "error",
            "diagnostics": [
                _diag(
                    "error",
                    "preview_window_beyond_recording",
                    "window extends past the end of the recording; samples are never zero-padded",
                    start_seconds=start_seconds,
                    end_seconds=end_seconds,
                    recording_seconds=recording_seconds,
                )
            ],
        }

    first_sample = int(round(local_start * rate))
    # Snap a near-boundary start onto the exact boundary sample.
    if abs(local_start * rate - first_sample) > 1e-6:
        diagnostics.append(
            _diag(
                "warning",
                "preview_start_off_sample",
                "window start does not land on a sample instant and was rounded to the nearest sample",
                requested_seconds=start_seconds,
                actual_seconds=segment["start_seconds"] + first_sample / rate,
            )
        )

    expected_n = int(round(cycles_per_window * rate / float(fundamental_hz)))
    integer_cycle = window_samples == expected_n
    if not integer_cycle:
        diagnostics.append(
            _diag(
                "warning",
                "non_integer_cycle",
                "preview window is not an exact integer-cycle record; fundamental phase and bin metrics are diagnostic",
                samples=window_samples,
                expected_samples=expected_n,
                sample_rate=rate,
                fundamental_hz=fundamental_hz,
                cycles_per_window=cycles_per_window,
            )
        )

    sequences = []
    cursor = first_sample
    remaining = window_samples
    assembly_offset = 0
    for sequence in segment["sequences"]:
        entry = next(item for item in expected if int(item["sequence"]) == sequence)
        count = int(entry["sample_count"])
        if cursor >= count:
            cursor -= count
            if not sequences:
                # Entire preceding chunk is skipped; the assembled array starts
                # at the first selected chunk, not at the segment origin.
                assembly_offset += count
            continue
        take = min(remaining, count - cursor)
        if take > 0:
            if not sequences:
                assembly_offset += cursor
            sequences.append(sequence)
            remaining -= take
            cursor = 0
        if remaining == 0:
            break

    return {
        "ok": True,
        "status": "warning" if any(item["severity"] == "warning" for item in diagnostics) else "ok",
        "sample_rate": rate,
        "segment_index": segment["segment_index"],
        "first_sample": first_sample,
        "assembly_offset": assembly_offset,
        "window_samples": window_samples,
        "actual_start_seconds": segment["start_seconds"] + first_sample / rate,
        "actual_end_seconds": segment["start_seconds"] + (first_sample + window_samples) / rate,
        "integer_cycle": integer_cycle,
        "expected_samples": expected_n,
        "sequences": sequences,
        "diagnostics": diagnostics,
    }


def _channel_metrics(raw: np.ndarray, coefficient: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    calibrated = calibrate_series(np.asarray(raw, dtype=np.float64), coefficient)
    result = harmonic_analysis(calibrated, float(params["sample_rate"]), params, complete_window=False)
    fundamental = result.get("fundamental") or {}
    return {
        "rms": result.get("rms"),
        "dc": result.get("dc"),
        "fundamental_rms": fundamental.get("rms"),
        "fundamental_phase_rad": fundamental.get("phase_rad"),
        "fundamental_phase_deg": fundamental.get("phase_deg"),
        "quality": result.get("quality", []),
    }


def build_calibration_preview(
    db: Session,
    manifest: Manifest,
    payload,
) -> dict[str, Any]:
    expected = sorted(manifest.expected_chunks, key=lambda item: item["sequence"])
    recording_seconds = sum(int(item["sample_count"]) / float(item["sample_rate"]) for item in expected)

    if bool(payload.candidate_version_id) == bool(payload.candidate_coefficients):
        raise ValueError(
            "provide exactly one of candidate_version_id or candidate_coefficients"
        )

    current = db.scalar(
        select(CalibrationVersion)
        .where(
            CalibrationVersion.channel_set_hash == manifest.channel_set_hash,
            CalibrationVersion.status == "active",
        )
        .order_by(CalibrationVersion.activated_at.desc(), CalibrationVersion.created_at.desc())
        .limit(1)
    )
    if payload.baseline_version_id is not None:
        pinned = db.get(CalibrationVersion, payload.baseline_version_id)
        if pinned is None:
            raise LookupError("baseline calibration version not found")
        if pinned.channel_set_hash != manifest.channel_set_hash:
            raise ValueError("baseline calibration version belongs to a different channel set")
        current = pinned
    if current is None:
        raise LookupError("no active calibration version for the manifest channel set")

    candidate_source: str
    if payload.candidate_version_id:
        candidate = db.get(CalibrationVersion, payload.candidate_version_id)
        if candidate is None:
            raise LookupError("candidate calibration version not found")
        if candidate.channel_set_hash != manifest.channel_set_hash:
            raise ValueError("candidate calibration version belongs to a different channel set")
        candidate_coefficients = candidate.coefficients
        candidate_source = "version"
    else:
        candidate_coefficients = {
            channel: value.model_dump(exclude_none=False)
            for channel, value in payload.candidate_coefficients.items()
        }
        candidate_source = "adhoc"

    channels = sorted(manifest.channel_set)
    missing_current = [channel for channel in channels if channel not in current.coefficients]
    missing_candidate = [channel for channel in channels if channel not in candidate_coefficients]
    if missing_current or missing_candidate:
        raise ValueError(
            f"calibration coefficients must cover every channel; "
            f"missing current={missing_current}, candidate={missing_candidate}"
        )

    plan = _plan_window(
        expected,
        start_seconds=float(payload.start_seconds),
        duration_seconds=payload.duration_seconds,
        sample_count=payload.sample_count,
        fundamental_hz=float(payload.fundamental_hz),
        cycles_per_window=int(payload.cycles_per_window),
        recording_seconds=recording_seconds,
    )

    diagnostics = list(plan["diagnostics"])
    channels_out: list[dict[str, Any]] = []
    sample_rate = plan.get("sample_rate")
    actual_start = plan.get("actual_start_seconds")
    actual_end = plan.get("actual_end_seconds")
    window_samples = plan.get("window_samples")

    if plan["ok"]:
        received = db.scalars(
            select(Chunk).where(
                Chunk.manifest_id == manifest.id,
                Chunk.sequence.in_(plan["sequences"]),
            )
        ).all()
        received_by_seq = {int(chunk.sequence): chunk for chunk in received}
        absent = [seq for seq in plan["sequences"] if seq not in received_by_seq]
        if absent:
            plan["ok"] = False
            plan["status"] = "error"
            diagnostics.append(
                _diag(
                    "error",
                    "preview_missing_chunks",
                    "raw blocks intersecting the preview window are missing; refusing to infer samples",
                    sequences=sorted(absent),
                )
            )

    if plan["ok"]:
        selected_meta = [_chunk_meta(received_by_seq[seq]) for seq in plan["sequences"]]
        settings = get_settings()
        Path(settings.spool_dir).mkdir(parents=True, exist_ok=True)
        store = get_object_store()
        assembled: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(dir=settings.spool_dir, prefix="cal-preview-") as scratch:
            raw_dir = Path(scratch) / "raw"

            def fetch_raw(item: dict[str, Any]) -> str:
                target = raw_dir / f"{int(item['sequence']):09d}.bin"
                try:
                    store.get_to_path(item["object_key"], target)
                except KeyError as exc:
                    raise _MissingRaw(int(item["sequence"])) from exc
                return str(target)

            try:
                assembled = group_chunks_by_rate(selected_meta, fetch_raw=fetch_raw)
            except _MissingRaw as exc:
                plan["ok"] = False
                plan["status"] = "error"
                diagnostics.append(
                    _diag(
                        "error",
                        "preview_missing_chunks",
                        "raw block bytes intersecting the preview window are unavailable; refusing to infer samples",
                        sequences=[exc.sequence],
                    )
                )

            try:
                if plan["ok"]:
                    # All selected chunks share the window segment's constant rate.
                    segment_data = assembled[0]["data"]
                    segment_channels = list(assembled[0]["channels"])
                    offset = plan["first_sample"] - plan["assembly_offset"]
                    window = np.asarray(segment_data[offset : offset + plan["window_samples"], :])
                    if window.shape[0] != plan["window_samples"]:
                        plan["ok"] = False
                        plan["status"] = "error"
                        diagnostics.append(
                            _diag(
                                "error",
                                "preview_window_incomplete",
                                "assembled window contains fewer samples than declared metadata",
                                samples=int(window.shape[0]),
                                expected_samples=plan["window_samples"],
                            )
                        )
                    else:
                        params = {
                            "fundamental_hz": float(payload.fundamental_hz),
                            "cycles_per_window": int(payload.cycles_per_window),
                            "max_harmonic": 15,
                            "fundamental_tolerance": 5e-3,
                            "saturation_warning_fraction": 0.01,
                            "sample_rate": sample_rate,
                        }
                        seen_quality: set[tuple[str, str]] = set()
                        for channel in channels:
                            index = segment_channels.index(channel)
                            raw = window[:, index]
                            current_metrics = _channel_metrics(raw, current.coefficients[channel], params)
                            candidate_metrics = _channel_metrics(raw, candidate_coefficients[channel], params)
                            for metrics in (current_metrics, candidate_metrics):
                                for item in metrics["quality"]:
                                    # The window-level non_integer_cycle diagnostic is
                                    # already reported; avoid repeating it per channel.
                                    key = (channel, item.get("code", ""))
                                    if item.get("code") == "non_integer_cycle" or key in seen_quality:
                                        continue
                                    seen_quality.add(key)
                                    diagnostics.append({"channel": channel, **item})

                            def delta(name: str) -> float | None:
                                old = current_metrics.get(name)
                                new = candidate_metrics.get(name)
                                if old is None or new is None:
                                    return None
                                return float(new) - float(old)

                            phase_delta = None
                            if (
                                current_metrics["fundamental_phase_rad"] is not None
                                and candidate_metrics["fundamental_phase_rad"] is not None
                            ):
                                phase_delta = wrap_phase(
                                    candidate_metrics["fundamental_phase_rad"]
                                    - current_metrics["fundamental_phase_rad"]
                                )
                            channels_out.append(
                                {
                                    "channel": channel,
                                    "current": {
                                        "coefficient": current.coefficients[channel],
                                        "rms": current_metrics["rms"],
                                        "fundamental_phase_rad": current_metrics["fundamental_phase_rad"],
                                        "fundamental_phase_deg": current_metrics["fundamental_phase_deg"],
                                    },
                                    "candidate": {
                                        "coefficient": candidate_coefficients[channel],
                                        "rms": candidate_metrics["rms"],
                                        "fundamental_phase_rad": candidate_metrics["fundamental_phase_rad"],
                                        "fundamental_phase_deg": candidate_metrics["fundamental_phase_deg"],
                                    },
                                    "delta": {
                                        "rms": delta("rms"),
                                        "fundamental_phase_rad": phase_delta,
                                        "fundamental_phase_deg": math.degrees(phase_delta)
                                        if phase_delta is not None
                                        else None,
                                    },
                                }
                            )
            finally:
                for item in assembled:
                    item["data"]._mmap.close()

    status = plan.get("status", "error")
    if any(item.get("severity") == "error" for item in diagnostics):
        status = "error"
    elif any(item.get("severity") == "warning" for item in diagnostics):
        status = "warning"

    return {
        "manifest_id": manifest.id,
        "status": status,
        "preview_only": True,
        "persisted": False,
        "window": {
            "requested_start_seconds": float(payload.start_seconds),
            "start_seconds": actual_start,
            "end_seconds": actual_end,
            "duration_seconds": (actual_end - actual_start) if actual_start is not None else None,
            "sample_rate": sample_rate,
            "samples": window_samples,
            "integer_cycle": plan.get("integer_cycle"),
            "expected_samples": plan.get("expected_samples"),
            "sequences": plan.get("sequences", []),
        },
        "current_calibration_version_id": current.id,
        "candidate": {
            "source": candidate_source,
            "calibration_version_id": payload.candidate_version_id,
            "coefficients": candidate_coefficients,
        },
        "channels": channels_out,
        "diagnostics": diagnostics,
        "manifest_issues": [
            {
                "severity": issue.severity,
                "code": issue.code,
                "message": issue.message,
                "details": issue.details,
            }
            for issue in sorted(manifest.issues, key=lambda item: item.created_at)
        ],
    }


class _MissingRaw(Exception):
    def __init__(self, sequence: int) -> None:
        super().__init__(f"raw bytes missing for sequence {sequence}")
        self.sequence = sequence
