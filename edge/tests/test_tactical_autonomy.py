"""
Unit & Integration Tests for Task 2.2:
Local Tactical Autonomy & Edge SLM Detour Handling (FR-3, FR-4, FR-5, NFR-1, NFR-6).
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

from edge.supervisor.supervisor import EdgeSupervisor
from edge.slm.engine import EdgeSLMEngine
from edge.mock.mock_robot import MockRobot


def _make_tactical_lease(
    lease_id="lease-tac-001",
    version=1,
    allowed_zones=None,
    forbidden_zones=None,
    assigned_robots=None,
    destination="2B-12"
):
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(minutes=10)
    expiry = now + timedelta(minutes=15)

    primary_robot = (assigned_robots or ["R1"])[0]

    return {
        "lease_id": lease_id,
        "version": version,
        "priority": "routine",
        "deadline_ts": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expiry_ts": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "allowed_zones": allowed_zones or ["central-supply", "corridor-2B", "corridor-2B-north", "2B-12"],
        "forbidden_zones": forbidden_zones or ["ward-2C-isolation"],
        "speed_limit": 0.8,
        "assigned_robots": assigned_robots or ["R1"],
        "task_sequence": [
            {"cmd": "go_to", "robot": primary_robot, "params": {"to": "central-supply"}},
            {"cmd": "go_to", "robot": primary_robot, "params": {"to": destination}},
            {"cmd": "photo", "robot": primary_robot, "params": {}}
        ],
        "cluster_id": "east-wing",
        "when_to_escalate": ["door_closed", "deadline_at_risk", "robot_failed"]
    }


def test_local_wait_on_transient_obstacle(tmp_path):
    """
    FR-4: Dynamic transient obstacle (e.g. cleaning trolley in corridor-2B) triggers LOCAL_WAIT.
    After waiting, obstacle clears and mission completes autonomously.
    """
    db_file = str(tmp_path / "test_wait.db")
    supervisor = EdgeSupervisor(cluster_id="east-wing", db_path=db_file, slm_mode="mock")

    # Inject transient obstacle into corridor-2B that clears after 1 wait
    robot = supervisor.robots["R1"]
    robot.inject_obstacle(
        zone="corridor-2B",
        obstacle_type="transient",
        description="Mobile laundry trolley moving through corridor",
        clears_after_waits=1
    )

    lease = _make_tactical_lease(
        lease_id="lease-transient-01",
        destination="2B-12"
    )

    res = supervisor.process_lease(lease)

    assert res["status"] == "COMPLETED"
    assert "storage://results/" in res["result_ref"]

    # Verify decision log has LOCAL_WAIT entry
    with supervisor.store._get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT decision_action, decided_by FROM decision_log WHERE lease_id = 'lease-transient-01' AND decision_action = 'LOCAL_WAIT'")
        row = cursor.fetchone()
        assert row is not None
        assert row["decision_action"] == "LOCAL_WAIT"
        assert row["decided_by"] in ("SLM", "RULE")


def test_local_detour_around_persistent_obstacle(tmp_path):
    """
    FR-4, FR-5: Persistent obstacle in corridor-2B triggers LOCAL_DETOUR via corridor-2B-north.
    Robot routes through permitted bypass and completes delivery.
    """
    db_file = str(tmp_path / "test_detour.db")
    supervisor = EdgeSupervisor(cluster_id="east-wing", db_path=db_file, slm_mode="mock")

    # Corridor-2B is persistently blocked
    robot = supervisor.robots["R1"]
    robot.inject_obstacle(
        zone="corridor-2B",
        obstacle_type="persistent",
        description="Scaffolding maintenance blockage in corridor-2B"
    )

    lease = _make_tactical_lease(
        lease_id="lease-detour-01",
        allowed_zones=["central-supply", "corridor-2B", "corridor-2B-north", "2B-12"],
        forbidden_zones=["ward-2C-isolation"],
        destination="2B-12"
    )

    res = supervisor.process_lease(lease)

    assert res["status"] == "COMPLETED"

    # Verify decision log recorded LOCAL_DETOUR
    with supervisor.store._get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT decision_action, decision_output FROM decision_log WHERE lease_id = 'lease-detour-01' AND decision_action = 'LOCAL_DETOUR'")
        row = cursor.fetchone()
        assert row is not None
        assert row["decision_action"] == "LOCAL_DETOUR"


def test_escalate_unexpected_when_detour_is_unavailable(tmp_path):
    """
    FR-4, FR-9: Route blocked and no alternate bypass exists in allowed_zones -> ESCALATE_UNEXPECTED.
    """
    db_file = str(tmp_path / "test_escalate_blocked.db")
    supervisor = EdgeSupervisor(cluster_id="east-wing", db_path=db_file, slm_mode="mock")

    robot = supervisor.robots["R1"]
    robot.inject_obstacle(
        zone="corridor-2B",
        obstacle_type="persistent",
        description="Security fire shutter closed"
    )

    # Allowed zones do NOT include corridor-2B-north (no bypass permitted!)
    lease = _make_tactical_lease(
        lease_id="lease-nobypass-01",
        allowed_zones=["central-supply", "corridor-2B", "2B-12"],  # No bypass!
        forbidden_zones=["ward-2C-isolation"],
        destination="2B-12"
    )

    res = supervisor.process_lease(lease)

    assert res["status"] == "ESCALATED_UNEXPECTED"

    # Verify UNEXPECTED_EVENT was queued in outbox for Cloud
    pending = supervisor.store.get_pending_outbox()
    assert any(p["message_type"] == "UNEXPECTED_EVENT" for p in pending)


def test_swap_robot_on_hardware_fault(tmp_path):
    """
    FR-3, FR-4: Active robot R1 suffers motor failure; task sequence transfers to standby R2.
    """
    db_file = str(tmp_path / "test_swap.db")
    supervisor = EdgeSupervisor(cluster_id="east-wing", db_path=db_file, slm_mode="mock")

    # Assigned robots: R1 and R2
    lease = _make_tactical_lease(
        lease_id="lease-swap-01",
        assigned_robots=["R1", "R2"],
        destination="2B-12"
    )

    # Initialize R1 and R2
    supervisor.robots["R1"] = MockRobot(robot_id="R1", initial_pos=[0.0, 0.0, 0.0])
    supervisor.robots["R2"] = MockRobot(robot_id="R2", initial_pos=[0.0, 0.0, 0.0])

    # Inject hardware fault on R1
    supervisor.robots["R1"].set_fault("Motor encoder failure")

    res = supervisor.process_lease(lease)

    assert res["status"] == "COMPLETED"

    # Verify R2 executed steps
    assert len(supervisor.robots["R2"].history) > 0
    assert supervisor.robots["R1"].status in ("FAILED", "IDLE")

    # Verify decision log has SWAP_ROBOT entry
    with supervisor.store._get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT decision_action FROM decision_log WHERE lease_id = 'lease-swap-01' AND decision_action = 'SWAP_ROBOT'")
        row = cursor.fetchone()
        assert row is not None
        assert row["decision_action"] == "SWAP_ROBOT"


def test_slm_timeout_triggers_deterministic_rule_fallback(tmp_path):
    """
    SPEC-P2 Sec 4.3: If SLM engine fails or times out (>8s), deterministic rules take over immediately.
    """
    engine = EdgeSLMEngine(mode="rules_only")

    # Context: dynamic obstacle
    ctx_transient = {
        "lease_id": "lease-timeout-01",
        "version": 1,
        "time_remaining_sec": 400.0,
        "robot_id": "R1",
        "current_pos": [10.0, 0.0],
        "current_zone": "corridor-2B",
        "allowed_zones": ["corridor-2B", "corridor-2B-north"],
        "forbidden_zones": [],
        "allowed_waypoints": ["corridor-2B-north"],
        "event_description": "Transient cleaning cart in corridor-2B",
        "wait_count": 0
    }

    dec, decided_by, latency = engine.evaluate_decision(ctx_transient)

    assert decided_by == "RULE"
    assert dec["action"] == "LOCAL_WAIT"
    assert latency < 50.0  # Deterministic rule evaluates in < 50ms


def test_boundary_guard_intercepts_unauthorized_slm_detour(tmp_path):
    """
    FR-5, Invariant 1: If SLM proposes a detour waypoint outside allowed_zones,
    the supervisor intercepts the violation and rejects it cleanly.
    """
    db_file = str(tmp_path / "test_hallucination.db")
    supervisor = EdgeSupervisor(cluster_id="east-wing", db_path=db_file, slm_mode="mock")

    # Robot encounters obstacle
    robot = supervisor.robots["R1"]
    robot.inject_obstacle("corridor-2B", obstacle_type="persistent", description="Hallway closed")

    lease = _make_tactical_lease(
        lease_id="lease-bad-detour",
        allowed_zones=["central-supply", "corridor-2B", "2B-12"],
        forbidden_zones=["ward-2C-isolation"],
        destination="2B-12"
    )

    # Monkeypatch SLM evaluate_decision to simulate an invalid detour to a forbidden zone
    supervisor.slm.evaluate_decision = lambda ctx: (
        {"action": "LOCAL_DETOUR", "target_waypoint": "ward-2C-isolation", "reason": "Hallucinated shortcut"},
        "SLM",
        15.0
    )

    res = supervisor.process_lease(lease)

    assert res["status"] == "FAILED_BOUNDARY"
    assert "Detour waypoint 'ward-2C-isolation' violates lease boundaries" in res["reason"]
