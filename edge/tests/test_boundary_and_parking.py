"""
Unit & Integration Tests for Task 2.1:
Lease Boundary & Safe Parking Enforcement (FR-5, FR-6, NFR-6).
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
import sys

# Ensure repository root containing 'edge' is on sys.path
_current = Path(__file__).resolve().parent
while _current.parent != _current:
    if (_current / "edge" / "supervisor").is_dir():
        if str(_current) not in sys.path:
            sys.path.insert(0, str(_current))
        break
    _current = _current.parent

from edge.supervisor.geofence import SpatialGeofenceValidator, DEFAULT_ZONE_REGISTRY
from edge.supervisor.parking import SafeParkingEngine, DEFAULT_SAFE_PARKING_SPOTS
from edge.supervisor.supervisor import EdgeSupervisor
from edge.mock.mock_robot import MockRobot


def _make_lease(
    lease_id="lease-test-boundary",
    version=1,
    allowed_zones=None,
    forbidden_zones=None,
    task_sequence=None,
    expired=False,
    assigned_robots=None
):
    now = datetime.now(timezone.utc)
    if expired:
        deadline = now - timedelta(minutes=5)
        expiry = now - timedelta(minutes=1)
    else:
        deadline = now + timedelta(minutes=10)
        expiry = now + timedelta(minutes=15)

    return {
        "lease_id": lease_id,
        "version": version,
        "priority": "urgent",
        "deadline_ts": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expiry_ts": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "allowed_zones": allowed_zones or ["central-supply", "corridor-2B", "2B-12"],
        "forbidden_zones": forbidden_zones or ["ward-2C-isolation"],
        "speed_limit": 0.8,
        "assigned_robots": assigned_robots or ["R1"],
        "task_sequence": task_sequence or [
            {"cmd": "go_to", "robot": "R1", "params": {"to": "central-supply"}},
            {"cmd": "go_to", "robot": "R1", "params": {"to": "2B-12"}},
            {"cmd": "photo", "robot": "R1", "params": {}}
        ],
        "cluster_id": "east-wing",
        "when_to_escalate": ["door_closed", "deadline_at_risk", "robot_failed"]
    }


# ==============================================================================
# 1. SPATIAL GEOFENCE TESTS (FR-5)
# ==============================================================================

def test_geofence_validator_allowed_and_forbidden_zones():
    """Validates that destinations outside allowed zones or in forbidden zones are rejected."""
    geofence = SpatialGeofenceValidator()
    allowed = ["central-supply", "corridor-2B", "2B-12"]
    forbidden = ["ward-2C-isolation"]

    # 1. Permitted destination
    valid, reason, resolved = geofence.validate_destination("2B-12", allowed, forbidden)
    assert valid
    assert resolved == "2B-12"

    # 2. Destination in forbidden zones
    valid, reason, resolved = geofence.validate_destination("ward-2C-isolation", allowed, forbidden)
    assert not valid
    assert "forbidden_zones" in reason

    # 3. Destination not in allowed zones
    valid, reason, resolved = geofence.validate_destination("pediatric-icu", allowed, forbidden)
    assert not valid
    assert "not permitted in allowed_zones" in reason


def test_geofence_validator_metric_coordinates():
    """Validates coordinate-based [x, y] geofence resolution."""
    geofence = SpatialGeofenceValidator()
    allowed = ["central-supply", "corridor-2B", "2B-12"]
    forbidden = ["ward-2C-isolation"]

    # [14.0, 4.0] is inside 2B-12 (allowed)
    valid, reason, zone = geofence.validate_destination([14.0, 4.0], allowed, forbidden)
    assert valid
    assert zone == "2B-12"

    # [25.0, 0.0] is inside ward-2C-isolation (forbidden)
    valid, reason, zone = geofence.validate_destination([25.0, 0.0], allowed, forbidden)
    assert not valid
    assert "forbidden zone" in reason

    # [100.0, 100.0] is outside hospital building
    valid, reason, zone = geofence.validate_destination([100.0, 100.0], allowed, forbidden)
    assert not valid
    assert "outside any recognized" in reason


def test_supervisor_blocks_destination_outside_allowed_zones(tmp_path):
    """Supervisor must reject any sub-task targeting a zone not in allowed_zones (FR-5)."""
    db_file = str(tmp_path / "test_geofence.db")
    supervisor = EdgeSupervisor(cluster_id="east-wing", db_path=db_file, slm_mode="mock")

    # Lease allows only central-supply and corridor-2B, but task requests pediatric-icu
    bad_lease = _make_lease(
        lease_id="lease-unallowed-001",
        allowed_zones=["central-supply", "corridor-2B"],
        forbidden_zones=["ward-2C-isolation"],
        task_sequence=[
            {"cmd": "go_to", "robot": "R1", "params": {"to": "pediatric-icu"}}
        ]
    )

    result = supervisor.process_lease(bad_lease)
    assert result["status"] == "FAILED_BOUNDARY"
    assert "Boundary violation" in result["reason"]

    # Verify decision logged in SQLite
    with supervisor.store._get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT decision_action, decided_by FROM decision_log WHERE lease_id = 'lease-unallowed-001'")
        row = cursor.fetchone()
        assert row is not None
        assert row["decision_action"] == "REJECT_BOUNDARY"
        assert row["decided_by"] == "RULE"

    # Verify UNEXPECTED_EVENT escalation was queued
    pending = supervisor.store.get_pending_outbox()
    assert len(pending) > 0
    assert pending[-1]["message_type"] == "UNEXPECTED_EVENT"


def test_supervisor_blocks_forbidden_intermediate_waypoints(tmp_path):
    """Supervisor must reject trajectory with forbidden intermediate waypoints (FR-5)."""
    db_file = str(tmp_path / "test_waypoints.db")
    supervisor = EdgeSupervisor(cluster_id="east-wing", db_path=db_file, slm_mode="mock")

    lease_with_bad_wp = _make_lease(
        lease_id="lease-waypoint-001",
        task_sequence=[
            {
                "cmd": "go_to",
                "robot": "R1",
                "params": {
                    "to": "2B-12",
                    "waypoints": ["corridor-2B", "ward-2C-isolation"]  # Second waypoint is forbidden!
                }
            }
        ]
    )

    result = supervisor.process_lease(lease_with_bad_wp)
    assert result["status"] == "FAILED_BOUNDARY"
    assert "Intermediate waypoint #2 invalid" in result["reason"]


# ==============================================================================
# 2. SAFE PARKING ENGINE TESTS (FR-6)
# ==============================================================================

def test_safe_parking_prohibits_doorways_elevators_and_corridors():
    """Asserts that robots NEVER park in doorways, corridors, or elevator vestibules (FR-6 Invariant 4)."""
    parking = SafeParkingEngine()

    # Prohibited zones
    assert not parking.is_safe_for_parking("doorway-fire-exit-1")
    assert not parking.is_safe_for_parking("elevator-vestibule-east")
    assert not parking.is_safe_for_parking("corridor-2B")

    # Permitted safe parking bays (recesses)
    assert parking.is_safe_for_parking("safe-parking-bay-1")
    assert parking.is_safe_for_parking("safe-parking-bay-2")
    assert parking.is_safe_for_parking("safe-parking-bay-east")


def test_nearest_safe_parking_spot_selection():
    """Verifies that the closest safe parking spot is dynamically selected."""
    parking = SafeParkingEngine()

    # Robot at central-supply [0.0, 0.0] -> nearest is safe-parking-bay-1 (at [-1.0, 4.0])
    spot1 = parking.find_nearest_safe_parking_spot(current_pose=[0.0, 0.0])
    assert spot1["zone_name"] == "safe-parking-bay-1"

    # Robot near ward 2B [12.0, 0.0] -> nearest is safe-parking-bay-2 (at [11.0, -1.8])
    spot2 = parking.find_nearest_safe_parking_spot(current_pose=[12.0, 0.0])
    assert spot2["zone_name"] == "safe-parking-bay-2"

    # Robot at far east wing [21.0, 2.0] -> nearest is safe-parking-bay-east (at [20.0, 3.0])
    spot3 = parking.find_nearest_safe_parking_spot(current_pose=[21.0, 2.0])
    assert spot3["zone_name"] == "safe-parking-bay-east"


def test_nearest_safe_parking_spot_excludes_forbidden_zones():
    """If the closest safe parking spot is inside lease forbidden_zones, choose the next closest."""
    parking = SafeParkingEngine()

    # If safe-parking-bay-1 is forbidden, robot at [0.0, 0.0] must choose safe-parking-bay-2
    spot = parking.find_nearest_safe_parking_spot(
        current_pose=[0.0, 0.0],
        forbidden_zones=["safe-parking-bay-1"]
    )
    assert spot["zone_name"] == "safe-parking-bay-2"


# ==============================================================================
# 3. EXPIRY WATCHDOG & INTERRUPT TESTS (FR-6)
# ==============================================================================

def test_in_flight_expiry_interrupt(tmp_path):
    """
    Simulates in-flight expiry interrupt (FR-6):
    - In-flight commands are cancelled.
    - Robot stops and navigates to the nearest safe parking recess outside corridors/doors.
    - Audit decision is recorded in SQLite.
    - Status transitions to EXPIRED_PARKED.
    """
    db_file = str(tmp_path / "test_interrupt.db")
    supervisor = EdgeSupervisor(cluster_id="east-wing", db_path=db_file, slm_mode="mock")

    # Ingest active lease
    lease = _make_lease(lease_id="lease-watchdog-001", version=1)
    supervisor.store.store_lease(lease)
    supervisor.active_lease = lease

    # Queue an in-flight command
    cmd_data = {
        "cmd_id": "cmd-inflight-001",
        "lease_id": "lease-watchdog-001",
        "robot_id": "R1",
        "action": "go_to",
        "params": {"to": "2B-12"}
    }
    supervisor.store.store_robot_command(cmd_data)
    supervisor.store.update_robot_command_status("cmd-inflight-001", "DISPATCHED")

    # Move robot to position [13.0, 1.0] (inside corridor-2B)
    robot = supervisor.robots["R1"]
    robot.pose = [13.0, 1.0, 0.0]
    robot.current_zone = "corridor-2B"

    # Trigger watchdog expiry interrupt!
    res = supervisor.trigger_expiry_interrupt(
        reason="Watchdog timer: valid_until exceeded"
    )

    assert res["status"] == "EXPIRED_PARKED"
    assert res["cancelled_commands"] == 1
    assert res["primary_spot"] == "safe-parking-bay-2"

    # Robot must have arrived at safe-parking-bay-2
    assert robot.current_zone == "safe-parking-bay-2"
    # Guaranteed not left standing in corridor or doorway
    assert not robot.current_zone.startswith("corridor")
    assert not robot.current_zone.startswith("doorway")

    # Verify SQLite lease status updated
    lease_rec = supervisor.store.get_lease("lease-watchdog-001")
    assert lease_rec is not None

    # Verify decision log has SAFE_PARK entry
    with supervisor.store._get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT decision_action, decided_by FROM decision_log WHERE lease_id = 'lease-watchdog-001'")
        row = cursor.fetchone()
        assert row is not None
        assert row["decision_action"] == "SAFE_PARK"
        assert row["decided_by"] == "RULE"

    # Verify UNEXPECTED_EVENT escalation was enqueued
    pending = supervisor.store.get_pending_outbox()
    assert any(p["message_type"] == "UNEXPECTED_EVENT" for p in pending)


def test_multi_robot_safe_parking_on_expiry(tmp_path):
    """When a multi-robot lease expires, all assigned robots are safely parked (FR-6)."""
    db_file = str(tmp_path / "test_multi_park.db")
    supervisor = EdgeSupervisor(cluster_id="east-wing", db_path=db_file, slm_mode="mock")

    # Lease assigns R1 and R2
    multi_lease = _make_lease(
        lease_id="lease-multi-001",
        version=1,
        assigned_robots=["R1", "R2"]
    )
    supervisor.store.store_lease(multi_lease)
    supervisor.active_lease = multi_lease

    # Setup R1 and R2 at different locations
    supervisor.robots["R1"] = MockRobot(robot_id="R1", initial_pos=[0.0, 0.0, 0.0])
    supervisor.robots["R2"] = MockRobot(robot_id="R2", initial_pos=[19.0, 2.0, 0.0])

    res = supervisor.trigger_expiry_interrupt()

    assert res["status"] == "EXPIRED_PARKED"
    assert len(res["parking_results"]) == 2

    # R1 (at [0,0]) should park at safe-parking-bay-1
    assert supervisor.robots["R1"].current_zone == "safe-parking-bay-1"
    # R2 (at [19,2]) should park at safe-parking-bay-east
    assert supervisor.robots["R2"].current_zone == "safe-parking-bay-east"
