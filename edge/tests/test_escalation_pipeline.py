"""
Test Suite: Escalation Pipeline (Task 2.3 - FR-9)
Validates:
1. Strict schema compliance for CLARIFY, UNEXPECTED_EVENT, and OUTPUT_READY (Draft 2020-12, Table 2).
2. Ambiguous destination / item handling triggering CLARIFY.
3. Unsolvable corridor blockage triggering UNEXPECTED_EVENT.
4. Camera artifact packaging, JPEG binary synthesis, SHA256 hashing, and metrics calculation.
5. Cloud re-planning loop (Lease v1 escalation -> Lease v2 replan -> monotonic execution).
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import pytest
import sys
import time

# Ensure repository root containing 'edge' is on sys.path
_current = Path(__file__).resolve().parent
while _current.parent != _current:
    if (_current / "edge" / "supervisor").is_dir():
        if str(_current) not in sys.path:
            sys.path.insert(0, str(_current))
        break
    _current = _current.parent

from edge.schemas.validator import validate_escalation, validate_lease
from edge.supervisor.supervisor import EdgeSupervisor
from edge.supervisor.artifacts import ArtifactPackager
from edge.store.local_store import EdgeLocalStore
from edge.mock.mock_robot import MockRobot


@pytest.fixture
def clean_db(tmp_path):
    db_file = tmp_path / "test_escalation.db"
    return str(db_file)


@pytest.fixture
def supervisor(clean_db, tmp_path):
    artifacts_dir = tmp_path / "artifacts"
    sup = EdgeSupervisor(
        cluster_id="east-wing",
        cloud_url="http://localhost:8000",
        db_path=clean_db,
        slm_mode="mock"
    )
    sup.artifact_packager = ArtifactPackager(artifacts_dir=str(artifacts_dir))
    return sup


def _make_sample_lease(lease_id="lease-esc-001", version=1, allowed=None, forbidden=None, task_seq=None):
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(minutes=10)
    expiry = now + timedelta(minutes=15)
    return {
        "lease_id": lease_id,
        "version": version,
        "priority": "routine",
        "deadline_ts": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expiry_ts": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "allowed_zones": allowed or ["central-supply", "corridor-2B", "2B-12"],
        "forbidden_zones": forbidden or ["mri-suite", "operating-theatre-1"],
        "speed_limit": 0.8,
        "assigned_robots": ["R1"],
        "task_sequence": task_seq or [
            {"cmd": "go_to", "robot": "R1", "params": {"to": "central-supply"}},
            {"cmd": "go_to", "robot": "R1", "params": {"to": "2B-12"}}
        ],
        "cluster_id": "east-wing",
        "when_to_escalate": ["door_closed", "deadline_at_risk", "robot_failed"]
    }


def test_clarify_escalation_schema_compliance():
    """FR-9: Verify CLARIFY escalation validates and rejects schema violations."""
    valid_clarify = {
        "type": "CLARIFY",
        "message_id": "msg-clarify-001",
        "lease_version": 1,
        "task_id": "task-clarify-01",
        "payload": {
            "question": "Clarify destination bed: 2B-12 or 2B-14?",
            "possible_readings": ["Bed 2B-12", "Bed 2B-14"]
        }
    }
    is_valid, msg = validate_escalation(valid_clarify)
    assert is_valid, f"Expected valid CLARIFY: {msg}"

    # Violation 1: Missing possible_readings
    invalid_clarify = {
        "type": "CLARIFY",
        "message_id": "msg-clarify-002",
        "lease_version": 1,
        "task_id": "task-clarify-02",
        "payload": {
            "question": "Which bed?"
        }
    }
    is_valid, _ = validate_escalation(invalid_clarify)
    assert not is_valid, "Expected schema error when missing possible_readings"

    # Violation 2: Extra top-level property (additionalProperties: false)
    invalid_extra = dict(valid_clarify)
    invalid_extra["extra_rogue_field"] = "illegal"
    is_valid, _ = validate_escalation(invalid_extra)
    assert not is_valid, "Expected rejection of additionalProperties"


def test_unexpected_event_escalation_schema_compliance():
    """FR-9: Verify UNEXPECTED_EVENT escalation validates and enforces required fields."""
    valid_ue = {
        "type": "UNEXPECTED_EVENT",
        "message_id": "msg-ue-001",
        "lease_version": 2,
        "task_id": "task-ue-01",
        "payload": {
            "what_happened": "Corridor blocked by biohazard spill",
            "where": "corridor-2B-east",
            "robots_saw": ["R1 optical sensor detected hazard cones at (10.5, 3.2)"]
        }
    }
    is_valid, msg = validate_escalation(valid_ue)
    assert is_valid, f"Expected valid UNEXPECTED_EVENT: {msg}"

    # Missing robots_saw
    invalid_ue = {
        "type": "UNEXPECTED_EVENT",
        "message_id": "msg-ue-002",
        "lease_version": 1,
        "task_id": "task-ue-02",
        "payload": {
            "what_happened": "Path blocked",
            "where": "corridor-2B"
        }
    }
    is_valid, _ = validate_escalation(invalid_ue)
    assert not is_valid, "Expected schema error when missing robots_saw"


def test_output_ready_escalation_schema_compliance():
    """FR-9: Verify OUTPUT_READY escalation validates result_ref."""
    valid_out = {
        "type": "OUTPUT_READY",
        "message_id": "msg-out-001",
        "lease_version": 1,
        "task_id": "task-out-01",
        "payload": {
            "result_ref": "storage://results/task-out-01/delivery-proof.jpg"
        }
    }
    is_valid, msg = validate_escalation(valid_out)
    assert is_valid, f"Expected valid OUTPUT_READY: {msg}"

    # Missing result_ref
    invalid_out = {
        "type": "OUTPUT_READY",
        "message_id": "msg-out-002",
        "lease_version": 1,
        "task_id": "task-out-02",
        "payload": {}
    }
    is_valid, _ = validate_escalation(invalid_out)
    assert not is_valid, "Expected schema error when missing result_ref"


def test_ambiguous_destination_triggers_clarify(supervisor):
    """FR-9: Ambiguous subtask instruction halts execution and emits CLARIFY escalation."""
    lease = _make_sample_lease(
        lease_id="lease-ambig-01",
        task_seq=[
            {"cmd": "go_to", "robot": "R1", "params": {"to": "central-supply"}},
            {
                "cmd": "go_to",
                "robot": "R1",
                "params": {
                    "to": "ward-2B-room-4",
                    "ambiguous": True,
                    "question": "Patient has two beds: Bed 4A or Bed 4B?",
                    "possible_readings": ["Bed 4A", "Bed 4B"]
                }
            }
        ]
    )

    res = supervisor.process_lease(lease)
    assert res["status"] == "ESCALATED_CLARIFY"
    assert "Bed 4A" in res["possible_readings"]

    # Verify outbox has stored the CLARIFY escalation
    pending = supervisor.store.get_pending_outbox()
    clarify_items = [p for p in pending if p["message_type"] == "CLARIFY"]
    assert len(clarify_items) >= 1
    clarify_msg = clarify_items[0]["payload"]

    is_valid, err = validate_escalation(clarify_msg)
    assert is_valid, f"Outbox CLARIFY message failed schema: {err}"
    assert clarify_msg["payload"]["question"] == "Patient has two beds: Bed 4A or Bed 4B?"
    assert clarify_msg["payload"]["possible_readings"] == ["Bed 4A", "Bed 4B"]


def test_unsolvable_corridor_blockage_triggers_unexpected_event(supervisor):
    """FR-9: Persistent obstacle with NO viable detour triggers UNEXPECTED_EVENT."""
    robot = supervisor.robots["R1"]
    # Inject persistent obstacle in corridor-2B
    robot.inject_obstacle("corridor-2B", obstacle_type="persistent", description="Scaffolding collapse", clears_after_waits=99)

    # Allow corridor-2B, but forbid north/south bypasses
    lease = _make_sample_lease(
        lease_id="lease-no-detour-01",
        allowed=["central-supply", "corridor-2B", "2B-12"],
        forbidden=["corridor-2B-north", "corridor-2B-south", "mri-suite"]
    )

    res = supervisor.process_lease(lease)
    assert res["status"] == "ESCALATED_UNEXPECTED"
    assert "no alternate detour" in res["reason"].lower() or "blocked" in res["reason"].lower()

    # Verify outbox payload
    pending = supervisor.store.get_pending_outbox()
    ue_items = [p for p in pending if p["message_type"] == "UNEXPECTED_EVENT"]
    assert len(ue_items) >= 1
    ue_msg = ue_items[0]["payload"]

    is_valid, err = validate_escalation(ue_msg)
    assert is_valid, f"Outbox UNEXPECTED_EVENT message failed schema: {err}"
    assert "where" in ue_msg["payload"]
    assert len(ue_msg["payload"]["robots_saw"]) >= 1


def test_camera_artifact_packaging_and_output_ready(supervisor):
    """FR-9: Completed mission synthesizes valid JPEG photo and metrics on disk and emits OUTPUT_READY."""
    lease = _make_sample_lease(
        lease_id="lease-photo-01",
        task_seq=[
            {"cmd": "go_to", "robot": "R1", "params": {"to": "central-supply"}},
            {"cmd": "go_to", "robot": "R1", "params": {"to": "2B-12"}},
            {"cmd": "photo", "robot": "R1", "params": {"photo_resolution": "1280x720"}}
        ]
    )

    res = supervisor.process_lease(lease)
    assert res["status"] == "COMPLETED"
    tid = lease.get("task_id", lease["lease_id"])
    assert res["result_ref"] == f"storage://results/{tid}/delivery-proof.jpg"

    # 1. Verify artifact exists on disk
    artifact_path = Path(res["artifact_path"])
    assert artifact_path.exists(), f"Expected artifact file at {artifact_path}"
    assert artifact_path.name == "delivery-proof.jpg"

    # 2. Verify binary JPEG magic bytes (FF D8)
    data = artifact_path.read_bytes()
    assert data.startswith(b"\xff\xd8"), "Artifact must be a valid JPEG file"
    assert data.endswith(b"\xff\xd9"), "Artifact must end with JPEG EOI marker"

    # 3. Verify sidecar metadata and metrics
    meta_path = artifact_path.parent / "proof_metadata.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["task_id"] == tid
    assert meta["sha256"] == res["artifact_sha256"]

    metrics_path = artifact_path.parent / "metrics.json"
    assert metrics_path.exists()
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert metrics["subtasks_completed"] == 3
    assert metrics["distance_travelled_m"] > 0.0

    # 4. Verify outbox OUTPUT_READY message complies with schema
    pending = supervisor.store.get_pending_outbox()
    out_items = [p for p in pending if p["message_type"] == "OUTPUT_READY"]
    assert len(out_items) >= 1
    out_msg = out_items[0]["payload"]

    is_valid, err = validate_escalation(out_msg)
    assert is_valid, f"OUTPUT_READY schema validation failed: {err}"
    assert out_msg["payload"]["result_ref"] == res["result_ref"]


def test_cloud_replanning_and_monotonic_version_execution(supervisor):
    """FR-8, FR-9: End-to-end Escalation -> Re-plan -> Monotonic Lease v2 execution."""
    robot = supervisor.robots["R1"]
    robot.inject_obstacle("corridor-2B", obstacle_type="persistent", description="Blocked by repair works", clears_after_waits=99)

    # 1. First lease (v1) fails because corridor-2B-north is not in allowed_zones
    lease_v1 = _make_sample_lease(
        lease_id="lease-replan-001",
        version=1,
        allowed=["central-supply", "corridor-2B", "2B-12"],
        forbidden=["corridor-2B-north", "mri-suite"]
    )
    res_v1 = supervisor.process_lease(lease_v1)
    assert res_v1["status"] == "ESCALATED_UNEXPECTED"

    # 2. Cloud orchestrator receives UNEXPECTED_EVENT and issues replanned Lease v2
    # Authorizing corridor-2B-north as detour bypass
    lease_v2 = _make_sample_lease(
        lease_id="lease-replan-001",
        version=2,
        allowed=["central-supply", "corridor-2B", "corridor-2B-north", "2B-12"],
        forbidden=["mri-suite"],
        task_seq=[
            {"cmd": "go_to", "robot": "R1", "params": {"to": "central-supply"}},
            {"cmd": "go_to", "robot": "R1", "params": {"to": "corridor-2B-north"}},
            {"cmd": "go_to", "robot": "R1", "params": {"to": "2B-12"}}
        ]
    )

    # Execute replan
    res_v2 = supervisor.handle_cloud_replan(lease_v2)
    assert res_v2["status"] == "COMPLETED"
    assert res_v2["executed_steps_count"] == 3

    # 3. If a stale v1 or v2 lease is delivered after v2, it must be rejected (FR-8)
    stale_v1 = _make_sample_lease(lease_id="lease-replan-001", version=1)
    res_stale = supervisor.handle_cloud_replan(stale_v1)
    assert res_stale["status"] == "REJECTED_STALE"
