"""
Tests for Nimbus Edge Tier & Local Autonomy (Person 2)
Validates FR-3, FR-5, FR-6, FR-8, FR-9, FR-10, NFR-6.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import pytest

# Ensure repository root containing 'edge' is on sys.path
_current = Path(__file__).resolve().parent
while _current.parent != _current:
    if (_current / "edge" / "supervisor").is_dir():
        if str(_current) not in sys.path:
            sys.path.insert(0, str(_current))
        break
    _current = _current.parent

from edge.schemas.validator import validate_lease, validate_escalation
from edge.supervisor.supervisor import EdgeSupervisor


def _make_sample_lease(lease_id="lease-t1-test", version=1, expired=False, forbidden_step=False):
    now = datetime.now(timezone.utc)
    if expired:
        deadline = now - timedelta(minutes=5)
        expiry = now - timedelta(minutes=2)
    else:
        deadline = now + timedelta(minutes=10)
        expiry = now + timedelta(minutes=12)

    destination = "ward-2C-isolation" if forbidden_step else "2B-12"

    return {
        "lease_id": lease_id,
        "version": version,
        "priority": "routine",
        "deadline_ts": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expiry_ts": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "allowed_zones": ["central-supply", "corridor-2B", "2B-12"],
        "forbidden_zones": ["ward-2C-isolation"],
        "speed_limit": 0.8,
        "assigned_robots": ["R1"],
        "task_sequence": [
            {"cmd": "go_to", "robot": "R1", "params": {"to": "central-supply"}},
            {"cmd": "wait", "robot": "R1", "params": {"seconds": 5}},
            {"cmd": "go_to", "robot": "R1", "params": {"to": destination}},
            {"cmd": "photo", "robot": "R1", "params": {}},
            {"cmd": "beep", "robot": "R1", "params": {}}
        ],
        "cluster_id": "east-wing",
        "when_to_escalate": ["door_closed", "deadline_at_risk", "robot_failed"]
    }


def test_lease_schema_validation():
    """Validates lease schema compliance and cross-field constraints."""
    lease = _make_sample_lease()
    valid, msg = validate_lease(lease)
    assert valid, f"Valid lease failed validation: {msg}"

    # Invalid: deadline > expiry
    bad_lease = dict(lease)
    bad_lease["deadline_ts"] = "2026-10-15T15:00:00Z"
    bad_lease["expiry_ts"] = "2026-10-15T14:00:00Z"
    valid, msg = validate_lease(bad_lease)
    assert not valid
    assert "cannot exceed" in msg


def test_task_t1_execution_end_to_end(tmp_path):
    """Executes Task T1 routine delivery end-to-end (Milestone 1)."""
    db_file = str(tmp_path / "test_edge.db")
    supervisor = EdgeSupervisor(cluster_id="east-wing", db_path=db_file, slm_mode="mock")

    lease = _make_sample_lease(lease_id="lease-task-t1-001", version=1)
    result = supervisor.process_lease(lease)

    assert result["status"] == "COMPLETED"
    assert result["executed_steps_count"] == 5
    assert "storage://results/" in result["result_ref"]

    # Verify SQLite store populated
    lease_record = supervisor.store.get_lease("lease-task-t1-001")
    assert lease_record is not None
    assert lease_record["version"] == 1


def test_monotonic_versioning_and_deduplication(tmp_path):
    """Verifies that stale or duplicate lease versions are rejected (FR-8)."""
    db_file = str(tmp_path / "test_edge.db")
    supervisor = EdgeSupervisor(cluster_id="east-wing", db_path=db_file, slm_mode="mock")

    # Ingest v1
    lease_v1 = _make_sample_lease(lease_id="lease-dedup-01", version=1)
    res1 = supervisor.process_lease(lease_v1)
    assert res1["status"] == "COMPLETED"

    # Re-inject duplicate v1
    res1_dup = supervisor.process_lease(lease_v1)
    assert res1_dup["status"] == "DROPPED_STALE"
    assert "Stale/duplicate version 1" in res1_dup["reason"]

    # Ingest v2
    lease_v2 = _make_sample_lease(lease_id="lease-dedup-01", version=2)
    res2 = supervisor.process_lease(lease_v2)
    assert res2["status"] == "COMPLETED"


def test_forbidden_zone_boundary_enforcement(tmp_path):
    """Verifies that attempting to enter forbidden zones is blocked and escalated (FR-5)."""
    db_file = str(tmp_path / "test_edge.db")
    supervisor = EdgeSupervisor(cluster_id="east-wing", db_path=db_file, slm_mode="mock")

    forbidden_lease = _make_sample_lease(lease_id="lease-forbidden-01", version=1, forbidden_step=True)
    res = supervisor.process_lease(forbidden_lease)

    assert res["status"] == "FAILED_BOUNDARY"
    assert "Forbidden zone: ward-2C-isolation" in res["reason"]


def test_expiry_watchdog_safe_parking(tmp_path):
    """Verifies that expired lease forces robots to park in safe spot (FR-6)."""
    db_file = str(tmp_path / "test_edge.db")
    supervisor = EdgeSupervisor(cluster_id="east-wing", db_path=db_file, slm_mode="mock")

    expired_lease = _make_sample_lease(lease_id="lease-expired-01", version=1, expired=True)
    res = supervisor.process_lease(expired_lease)

    assert res["status"] == "EXPIRED_PARKED"
    assert "parked in safe spot" in res["reason"]
    assert res["parking"]["action"] == "go_to"
    assert res["parking"]["params"]["to"] == "safe-parking-bay-1"
