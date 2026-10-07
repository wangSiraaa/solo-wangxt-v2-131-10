import math

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import AnalysisTask, CalibrationVersion, Report
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
    upload_order = list(reversed(range(len(chunks))))
    upload_chunks(client, manifest["id"], chunks, order=upload_order)
    client.post(f"/manifests/{manifest['id']}/finalize")
    calibration = make_calibration(client, manifest["channel_set_hash"])
    return manifest, calibration, chunks


def preview_request(client, manifest_id, baseline_id, coefficients=None, **overrides):
    payload = {
        "start_seconds": overrides.get("start_seconds", 0.0),
        "duration_seconds": overrides.get("duration_seconds", 0.12),
        "candidate_coefficients": coefficients
        or {
            channel: {"gain": 1.0, "offset": 0.0, "phase_shift_rad": 0.0}
            for channel in CHANNELS
        },
        "baseline_calibration_version_id": baseline_id,
    }
    payload.update({key: value for key, value in overrides.items() if key not in {"start_seconds", "duration_seconds"}})
    return client.post(f"/manifests/{manifest_id}/calibration-preview", json=payload)


def by_channel(result):
    return {entry["channel"]: entry for entry in result["channels"]}


def codes(result, severity=None):
    return {
        item["code"]
        for item in result["diagnostics"]
        if severity is None or item["severity"] == severity
    }


def test_preview_gain_doubling_changes_rms_by_formula(client: TestClient):
    manifest, calibration, _ = completed_recording(client)
    candidate = {
        channel: {"gain": 2.0 if channel == "Va" else 1.0, "offset": 0.0, "phase_shift_rad": 0.0}
        for channel in CHANNELS
    }
    response = preview_request(client, manifest["id"], calibration["id"], candidate)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "ok"
    va = by_channel(result)["Va"]
    # y = gain*x with zero offset: RMS(y) = gain * RMS(x), including DC/noise.
    assert math.isclose(va["candidate"]["rms"], 2.0 * va["baseline"]["rms"], rel_tol=1e-9)
    assert math.isclose(va["delta"]["rms_ratio"], 2.0, rel_tol=1e-9)
    assert math.isclose(va["delta"]["rms"], va["baseline"]["rms"], rel_tol=1e-9)
    # Unchanged channels are exactly unchanged; gain sign/positive real scale
    # leaves the fundamental phase angle untouched.
    vb = by_channel(result)["Vb"]
    assert math.isclose(vb["delta"]["rms"], 0.0, abs_tol=1e-9)
    assert abs(va["delta"]["fundamental_phase_deg"]) < 1e-6


def test_preview_offset_shifts_rms_and_dc_but_not_fundamental(client: TestClient):
    manifest, calibration, _ = completed_recording(client)
    candidate = {
        "Va": {"gain": 1.0, "offset": 5.0, "phase_shift_rad": 0.0},
        "Vb": {"gain": 1.0, "offset": 0.0, "phase_shift_rad": 0.0},
        "Vc": {"gain": 1.0, "offset": 0.0, "phase_shift_rad": 0.0},
    }
    result = preview_request(client, manifest["id"], calibration["id"], candidate).json()
    va = by_channel(result)["Va"]
    assert math.isclose(va["candidate"]["dc"], 5.0, abs_tol=1e-9)
    # RMS follows sqrt(RMS0^2 + offset^2) for a zero-DC baseline signal.
    expected = math.sqrt(va["baseline"]["rms"] ** 2 + 25.0)
    assert math.isclose(va["candidate"]["rms"], expected, rel_tol=1e-9)
    assert math.isclose(va["candidate"]["fundamental_rms"], va["baseline"]["fundamental_rms"], rel_tol=1e-9)
    assert abs(va["delta"]["fundamental_phase_deg"]) < 1e-6


def test_preview_phase_shift_moves_fundamental_phase(client: TestClient):
    manifest, calibration, _ = completed_recording(client)
    shift = math.pi / 6
    candidate = {
        channel: {"gain": 1.0, "offset": 0.0, "phase_shift_rad": shift if channel == "Va" else 0.0}
        for channel in CHANNELS
    }
    result = preview_request(client, manifest["id"], calibration["id"], candidate).json()
    va = by_channel(result)["Va"]
    # Conventions: Y[k] = X[k] * exp(-j*shift), so the phase angle decreases.
    assert math.isclose(va["delta"]["fundamental_phase_rad"], -shift, abs_tol=1e-6)
    assert math.isclose(va["candidate"]["fundamental_rms"], va["baseline"]["fundamental_rms"], rel_tol=1e-9)


def test_preview_window_covers_complete_integer_cycles(client: TestClient):
    # One 6000 Hz block; 6 cycles of 50 Hz at 6000 Hz is 720 samples = 0.12 s.
    manifest, calibration, _ = completed_recording(client, sample_chunks=(720,))
    result = preview_request(client, manifest["id"], calibration["id"]).json()
    assert result["status"] == "ok"
    assert result["window"]["samples"] == 720
    assert "non_integer_cycle" not in codes(result)
    va = by_channel(result)["Va"]
    assert math.isclose(va["baseline"]["rms"], math.sqrt(300**2 + 10**2 + 20**2), rel_tol=1e-7)


def test_preview_non_integer_cycle_window_is_explicit_warning(client: TestClient):
    manifest, calibration, _ = completed_recording(client, sample_chunks=(720,))
    result = preview_request(
        client, manifest["id"], calibration["id"], start_seconds=0.0, duration_seconds=0.1
    ).json()
    assert result["status"] == "warning"
    warning = next(item for item in result["diagnostics"] if item["code"] == "non_integer_cycle")
    assert warning["details"]["samples"] == 600
    assert warning["details"]["expected_samples"] == 720
    # Metrics are still produced for diagnostic comparison.
    assert by_channel(result)["Va"]["candidate"]["rms"] > 0


def completed_rate_change_recording(client):
    chunks = make_rate_change_chunks()
    manifest = create_manifest(client, chunks, nominal_sample_rate=6000.0)
    upload_chunks(client, manifest["id"], chunks)
    finalized = client.post(f"/manifests/{manifest['id']}/finalize")
    assert finalized.status_code == 200, finalized.text
    calibration = make_calibration(client, manifest["channel_set_hash"])
    return manifest, calibration


def test_preview_crossing_rate_boundary_is_error_without_metrics(client: TestClient):
    manifest, calibration = completed_rate_change_recording(client)
    result = preview_request(
        client,
        manifest["id"],
        calibration["id"],
        start_seconds=0.03,
        duration_seconds=0.05,
    ).json()
    assert result["status"] == "error"
    assert "sample_rate_cross_segment" in codes(result, "error")
    boundary_item = next(item for item in result["diagnostics"] if item["code"] == "sample_rate_cross_segment")
    assert boundary_item["details"]["sample_rates"] == [6000.0, 7000.0]
    assert result["channels"] == []


def test_preview_window_inside_one_rate_segment_warns_about_rate_change(client: TestClient):
    manifest, calibration = completed_rate_change_recording(client)
    result = preview_request(
        client,
        manifest["id"],
        calibration["id"],
        start_seconds=0.0,
        duration_seconds=0.05,
    ).json()
    assert result["status"] == "warning"
    assert "sample_rate_changed" in codes(result, "warning")
    assert "sample_rate_cross_segment" not in codes(result)
    assert result["window"]["sample_rate"] == 6000.0


def test_preview_missing_chunk_in_window_is_diagnostic(client: TestClient):
    chunks = make_chunks(sample_chunks=(720, 720))
    manifest = create_manifest(client, chunks)
    # Only sequence 0 arrives; leave the manifest open so preview stays possible.
    upload_chunks(client, manifest["id"], [chunks[0]])
    calibration = make_calibration(client, manifest["channel_set_hash"])
    result = preview_request(
        client, manifest["id"], calibration["id"], start_seconds=0.12, duration_seconds=0.05
    ).json()
    assert result["status"] == "error"
    missing = next(item for item in result["diagnostics"] if item["code"] == "missing_chunk")
    assert missing["details"]["sequences"] == [1]
    assert result["channels"] == []


def test_preview_missing_chunk_outside_window_is_only_warning(client: TestClient):
    chunks = make_chunks(sample_chunks=(720, 720))
    manifest = create_manifest(client, chunks)
    upload_chunks(client, manifest["id"], [chunks[0]])
    calibration = make_calibration(client, manifest["channel_set_hash"])
    result = preview_request(
        client, manifest["id"], calibration["id"], start_seconds=0.0, duration_seconds=0.12
    ).json()
    assert result["status"] == "warning"
    assert "manifest_not_completed" in codes(result, "warning")
    outside = next(
        item for item in result["diagnostics"] if item["code"] == "missing_chunk_outside_window"
    )
    assert outside["details"]["sequences"] == [1]
    assert by_channel(result)["Va"]["baseline"]["rms"] > 0


def test_preview_window_too_short_for_fundamental_keeps_rms_only(client: TestClient):
    # 1/300 s at 6000 Hz = 20 samples: 50 Hz cannot occupy even the first DFT bin.
    manifest, calibration, _ = completed_recording(client, sample_chunks=(300, 420))
    candidate = {
        channel: {"gain": 2.0, "offset": 0.0, "phase_shift_rad": 0.0} for channel in CHANNELS
    }
    result = preview_request(
        client, manifest["id"], calibration["id"], candidate,
        start_seconds=0.0, duration_seconds=1.0 / 300.0,
    ).json()
    assert result["status"] == "error"
    assert "fundamental_unresolvable" in codes(result, "error")
    va = by_channel(result)["Va"]
    # Time-domain RMS is still exact under y = gain*x; phase is explicitly null.
    assert math.isclose(va["candidate"]["rms"], 2.0 * va["baseline"]["rms"], rel_tol=1e-9)
    assert va["candidate"]["fundamental_phase_rad"] is None
    assert va["delta"]["fundamental_phase_deg"] is None


def test_preview_rejects_bad_requests(client: TestClient):
    manifest, calibration, _ = completed_recording(client)
    coefficients = {
        channel: {"gain": 1.0, "offset": 0.0, "phase_shift_rad": 0.0} for channel in CHANNELS
    }
    # Window beyond recording (720 samples at 6000 Hz = 0.12 s).
    response = preview_request(
        client, manifest["id"], calibration["id"], coefficients,
        start_seconds=0.2, duration_seconds=0.01,
    )
    assert response.status_code == 422
    # Both duration and end given.
    response = preview_request(
        client, manifest["id"], calibration["id"], coefficients,
        start_seconds=0.0, duration_seconds=0.01, end_seconds=0.02,
    )
    assert response.status_code == 422
    # Coefficient set does not cover all channels.
    response = client.post(
        f"/manifests/{manifest['id']}/calibration-preview",
        json={
            "start_seconds": 0.0,
            "duration_seconds": 0.12,
            "candidate_coefficients": {"Va": coefficients["Va"]},
            "baseline_calibration_version_id": calibration["id"],
        },
    )
    assert response.status_code == 422
    # Unknown manifest.
    response = client.post(
        "/manifests/does-not-exist/calibration-preview",
        json={
            "start_seconds": 0.0,
            "duration_seconds": 0.12,
            "candidate_coefficients": coefficients,
        },
    )
    assert response.status_code == 404


def test_preview_is_readonly_and_does_not_freeze_anything(client: TestClient):
    manifest, calibration, _ = completed_recording(client)
    task_id = create_and_run_task(client, manifest["id"], calibration["id"])
    report_before = client.get(f"/reports?manifest_id={manifest['id']}").json()[0]

    candidate = {
        channel: {"gain": 3.0, "offset": 1.0, "phase_shift_rad": 0.2} for channel in CHANNELS
    }
    response = preview_request(client, manifest["id"], calibration["id"], candidate)
    assert response.status_code == 200
    body = response.json()
    assert body["persisted"] is False

    with SessionLocal() as db:
        # No task and no new calibration version were created by the preview.
        assert db.query(AnalysisTask).filter(AnalysisTask.manifest_id == manifest["id"]).count() == 1
        assert db.query(CalibrationVersion).count() == 1
        assert db.query(Report).filter(Report.task_id == task_id).count() == 1

    # Cancelling the preview (client stops asking; here we simply re-fetch)
    # leaves the old calibration and report byte-identical.
    report_after = client.get(f"/reports/{report_before['id']}").json()
    assert report_after["status"] == report_before["status"] == "published"
    assert report_after["result"] == report_before["result"]
    assert report_after["calibration_version_id"] == calibration["id"]
    calibrations = client.get(
        f"/calibrations?channel_set_hash={manifest['channel_set_hash']}"
    ).json()
    assert len(calibrations) == 1
    assert calibrations[0]["coefficients"] == calibration["coefficients"]


def test_formal_task_ignores_previewed_candidate_until_version_selected(client: TestClient):
    manifest, calibration, _ = completed_recording(client)
    candidate = {
        channel: {"gain": 5.0, "offset": 0.0, "phase_shift_rad": 0.0} for channel in CHANNELS
    }
    assert preview_request(client, manifest["id"], calibration["id"], candidate).status_code == 200

    # A formal task created without explicitly choosing a (nonexistent) new
    # version still freezes the old active calibration.
    task_id = create_and_run_task(client, manifest["id"], calibration["id"])
    task = client.get(f"/analysis-tasks/{task_id}").json()
    assert task["calibration_version_id"] == calibration["id"]
    frozen = task["manifest_snapshot"]["calibration_coefficients"]
    assert all(abs(float(coef["gain"]) - 1.0) < 1e-12 for coef in frozen.values())

    # Publishing the candidate as a real version and explicitly selecting it is
    # the only path that changes task coefficients.
    published = client.post(
        "/calibrations",
        json={
            "channel_set_hash": manifest["channel_set_hash"],
            "coefficients": candidate,
            "change_note": "promoted after preview",
        },
    ).json()
    new_task_id = create_and_run_task(client, manifest["id"], published["id"])
    new_task = client.get(f"/analysis-tasks/{new_task_id}").json()
    assert new_task["calibration_version_id"] == published["id"]
    frozen = new_task["manifest_snapshot"]["calibration_coefficients"]
    assert all(abs(float(coef["gain"]) - 5.0) < 1e-12 for coef in frozen.values())
