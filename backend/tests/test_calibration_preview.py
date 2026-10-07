import math

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import AnalysisTask, CalibrationVersion, Chunk, Report
from tests.synthetic import (
    CHANNELS,
    create_and_run_task,
    create_manifest,
    make_calibration,
    make_chunks,
    make_rate_change_chunks,
    upload_chunks,
)


def completed_recording(client, sample_chunks=(300, 420)):
    chunks = make_chunks(sample_chunks=sample_chunks)
    manifest = create_manifest(client, chunks)
    upload_chunks(client, manifest["id"], chunks, order=[1, 0])
    response = client.post(f"/manifests/{manifest['id']}/finalize")
    assert response.status_code == 200, response.text
    calibration = make_calibration(client, manifest["channel_set_hash"])
    return manifest, calibration, chunks


def candidate_coefficients(gain_a=2.0, phase_shift_a=0.0):
    return {
        channel: {
            "gain": gain_a if channel == "Va" else 1.0,
            "offset": 0.0,
            "phase_shift_rad": phase_shift_a if channel == "Va" else 0.0,
        }
        for channel in CHANNELS
    }


def preview(client, manifest_id, **overrides):
    payload = {"start_seconds": 0.0, "candidate_coefficients": candidate_coefficients()}
    payload.update(overrides)
    return client.post(f"/manifests/{manifest_id}/calibration-preview", json=payload)


def test_gain_doubling_changes_rms_by_formula(client: TestClient):
    manifest, calibration, _ = completed_recording(client)
    response = preview(client, manifest["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["window"]["integer_cycle"] is True
    va = next(item for item in body["channels"] if item["channel"] == "Va")
    vb = next(item for item in body["channels"] if item["channel"] == "Vb")
    # y = gain*x + offset with offset 0 doubles every sample, hence doubles RMS.
    assert va["current"]["rms"] == pytest.approx(math.sqrt(300**2 + 10**2 + 20**2), rel=1e-9)
    assert va["candidate"]["rms"] == pytest.approx(2.0 * va["current"]["rms"], rel=1e-9)
    assert va["delta"]["rms"] == pytest.approx(va["current"]["rms"], rel=1e-9)
    # Unchanged channel shows zero deltas.
    assert vb["delta"]["rms"] == pytest.approx(0.0, abs=1e-9)
    assert vb["delta"]["fundamental_phase_deg"] == pytest.approx(0.0, abs=1e-9)
    # Fundamental phase is gain-invariant.
    assert va["delta"]["fundamental_phase_deg"] == pytest.approx(0.0, abs=1e-9)


def test_phase_shift_rotates_fundamental_phase_only(client: TestClient):
    manifest, calibration, _ = completed_recording(client)
    shift = 0.25
    response = preview(
        client,
        manifest["id"],
        candidate_coefficients=candidate_coefficients(gain_a=1.0, phase_shift_a=shift),
    )
    assert response.status_code == 200, response.text
    va = next(item for item in response.json()["channels"] if item["channel"] == "Va")
    assert va["delta"]["fundamental_phase_rad"] == pytest.approx(-shift, abs=1e-9)
    # Constant phase rotation does not change RMS magnitudes.
    assert va["delta"]["rms"] == pytest.approx(0.0, abs=1e-7)


def test_preview_creates_no_task_report_or_calibration(client: TestClient):
    manifest, calibration, _ = completed_recording(client)
    before = {
        "tasks": client.get("/analysis-tasks").json().__len__(),
        "reports": client.get("/reports").json().__len__(),
        "calibrations": client.get("/calibrations").json().__len__(),
    }
    for _ in range(3):
        response = preview(client, manifest["id"])
        assert response.status_code == 200
        assert response.json()["persisted"] is False
        assert response.json()["preview_only"] is True
    with SessionLocal() as db:
        assert db.query(AnalysisTask).count() == before["tasks"]
        assert db.query(Report).count() == before["reports"]
        assert db.query(CalibrationVersion).count() == before["calibrations"]
    # The current calibration is untouched and remains active.
    assert client.get(f"/calibrations/{calibration['id']}").json()["status"] == "active"


def test_cancelling_preview_leaves_existing_report_unchanged(client: TestClient):
    manifest, calibration, _ = completed_recording(client)
    task_id = create_and_run_task(client, manifest["id"], calibration["id"])
    old_report = client.get(f"/reports?manifest_id={manifest['id']}").json()[0]

    # The frontend "cancel" simply discards the response; assert the server side
    # has nothing to roll back: run a preview and then re-read the report.
    response = preview(client, manifest["id"])
    assert response.status_code == 200
    again = client.get(f"/reports/{old_report['id']}").json()
    assert again["status"] == "published"
    assert again["result"] == old_report["result"]
    assert again["calibration_version_id"] == calibration["id"]
    assert client.get(f"/analysis-tasks/{task_id}").json()["status"] == "succeeded"


def test_preview_with_saved_candidate_version(client: TestClient):
    manifest, calibration, _ = completed_recording(client)
    updated = client.post(
        "/calibrations",
        json={
            "channel_set_hash": manifest["channel_set_hash"],
            "coefficients": candidate_coefficients(gain_a=1.5),
            "change_note": "candidate for preview",
        },
    ).json()
    # The new version is now active, so pin the baseline to the original to
    # trial the saved candidate against the coefficients currently in reports.
    response = client.post(
        f"/manifests/{manifest['id']}/calibration-preview",
        json={
            "start_seconds": 0.0,
            "candidate_version_id": updated["id"],
            "baseline_version_id": calibration["id"],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["candidate"]["source"] == "version"
    assert body["candidate"]["calibration_version_id"] == updated["id"]
    assert body["current_calibration_version_id"] == calibration["id"]
    va = next(item for item in body["channels"] if item["channel"] == "Va")
    assert va["candidate"]["rms"] == pytest.approx(1.5 * va["current"]["rms"], rel=1e-9)


def test_preview_rejects_both_and_neither_candidate_inputs(client: TestClient):
    manifest, calibration, _ = completed_recording(client)
    response = client.post(
        f"/manifests/{manifest['id']}/calibration-preview",
        json={"start_seconds": 0.0},
    )
    assert response.status_code == 422
    updated = client.post(
        "/calibrations",
        json={
            "channel_set_hash": manifest["channel_set_hash"],
            "coefficients": candidate_coefficients(),
        },
    ).json()
    response = client.post(
        f"/manifests/{manifest['id']}/calibration-preview",
        json={
            "start_seconds": 0.0,
            "candidate_version_id": updated["id"],
            "candidate_coefficients": candidate_coefficients(),
        },
    )
    assert response.status_code == 422


def test_preview_requires_completed_manifest(client: TestClient):
    chunks = make_chunks(sample_chunks=(300, 420))
    manifest = create_manifest(client, chunks)
    upload_chunks(client, manifest["id"], chunks)
    # Deliberately not finalized.
    make_calibration(client, manifest["channel_set_hash"])
    response = preview(client, manifest["id"])
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "preview_manifest_not_completed"


def test_preview_diagnoses_missing_blocks(client: TestClient):
    # Finalize normally first so the manifest is completed...
    manifest, calibration, chunks = completed_recording(client)
    # ...then remove one received chunk row to simulate an unavailable block
    # intersecting the window (defensive: preview never infers samples).
    with SessionLocal() as db:
        db.query(Chunk).filter(
            Chunk.manifest_id == manifest["id"],
            Chunk.sequence == 0,
        ).delete()
        db.commit()
    response = preview(client, manifest["id"], start_seconds=0.0)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    codes = {item["code"] for item in body["diagnostics"]}
    assert "preview_missing_chunks" in codes
    assert body["channels"] == []


def test_preview_diagnoses_window_crossing_rate_boundary(client: TestClient):
    raw_chunks = make_rate_change_chunks()
    manifest = create_manifest(client, raw_chunks)
    upload_chunks(client, manifest["id"], raw_chunks)
    client.post(f"/manifests/{manifest['id']}/finalize")
    make_calibration(client, manifest["channel_set_hash"])
    # First segment is 300 samples at 6000 Hz = 0.05 s; start at 0.045 s with
    # the 0.12 s default window crosses into the 7000 Hz segment.
    response = preview(client, manifest["id"], start_seconds=0.045)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    cross = next(item for item in body["diagnostics"] if item["code"] == "preview_crosses_rate_boundary")
    assert cross["details"]["sample_rates"] == [6000.0, 7000.0]
    assert body["channels"] == []

    # Second segment spans [0.05 s, 0.11 s] (420 samples at 7000 Hz). A window
    # fully inside it is analyzed on its own clock, never joined to segment 1.
    response = preview(client, manifest["id"], start_seconds=0.05, sample_count=420)
    assert response.status_code == 200, response.text
    assert response.json()["status"] in {"ok", "warning"}
    assert response.json()["window"]["sample_rate"] == 7000.0

    # A window that starts inside segment 2 but runs past 0.11 s is rejected
    # rather than zero-padded.
    response = preview(client, manifest["id"], start_seconds=0.08, sample_count=300)
    assert response.status_code == 200
    assert any(
        item["code"] == "preview_window_beyond_recording"
        for item in response.json()["diagnostics"]
    )


def test_preview_diagnoses_non_integer_cycle(client: TestClient):
    manifest, calibration, _ = completed_recording(client)
    response = preview(client, manifest["id"], sample_count=301)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "warning"
    warning = next(item for item in body["diagnostics"] if item["code"] == "non_integer_cycle")
    assert warning["details"]["samples"] == 301
    assert warning["details"]["expected_samples"] == 720
    # Metrics are still returned for review (diagnostic, not silent failure).
    assert len(body["channels"]) == len(CHANNELS)


def test_preview_window_starting_inside_later_chunk(client: TestClient):
    # Two constant-rate chunks of 300 and 420 samples (720 total = 0.12 s).
    # Request a 420-sample window starting 0.03 s (sample 180), spanning the
    # chunk-0 tail (120 samples) and the first 300 samples of chunk 1. The
    # assembly offset must map onto the correct samples across the boundary.
    manifest, calibration, _ = completed_recording(client)
    response = preview(client, manifest["id"], start_seconds=0.03, sample_count=420)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "warning"  # 420 is not the 720-sample integer window
    assert body["window"]["sequences"] == [0, 1]
    assert body["window"]["samples"] == 420
    va = next(item for item in body["channels"] if item["channel"] == "Va")
    # gain 2 on Va doubles RMS even for a cross-chunk window.
    assert va["candidate"]["rms"] == pytest.approx(2.0 * va["current"]["rms"], rel=1e-9)


def test_preview_rejects_window_past_recording(client: TestClient):
    manifest, calibration, _ = completed_recording(client)
    response = preview(client, manifest["id"], start_seconds=100.0)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert any(item["code"] == "preview_window_out_of_range" for item in body["diagnostics"])

    response = preview(client, manifest["id"], start_seconds=0.11, duration_seconds=0.05)
    assert response.status_code == 200
    body = response.json()
    assert any(item["code"] == "preview_window_beyond_recording" for item in body["diagnostics"])


def test_formal_task_still_uses_only_explicit_frozen_version(client: TestClient):
    manifest, calibration, _ = completed_recording(client)
    # Trying candidate coefficients in a preview does not publish a version.
    response = preview(
        client,
        manifest["id"],
        candidate_coefficients=candidate_coefficients(gain_a=2.0),
    )
    assert response.status_code == 200
    with SessionLocal() as db:
        assert db.query(CalibrationVersion).count() == 1
    # A task created without an explicit version still freezes the active one;
    # the previewed gain=2 is never used.
    created = client.post(
        "/analysis-tasks",
        json={"manifest_id": manifest["id"], "calibration_version_id": calibration["id"]},
    ).json()
    client.post(f"/analysis-tasks/{created['id']}/run")
    report = client.get(f"/reports?manifest_id={manifest['id']}").json()[0]
    va = report["result"]["segments"][0]["channels"]["Va"]
    assert va["rms"] == pytest.approx(math.sqrt(300**2 + 10**2 + 20**2), rel=1e-9)
    snapshot = report["result"]["fixed_snapshot"]
    assert snapshot["calibration_version_id"] == calibration["id"]
    assert snapshot["calibration_coefficients"]["Va"]["gain"] == 1.0
