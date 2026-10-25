"""
Test Suite: Store-and-Forward WAN Outage Resilience (Task 2.4 - FR-10, FR-12, NFR-2)
Validates:
1. Store-and-Forward Invariant (FR-10): All escalations and outputs written to SQLite before network send.
2. 10-minute WAN outage simulation: Zero decision logs or outbox messages lost during partition.
3. Exponential backoff retry mechanism with FIFO ordering.
4. Reconnection protocol (FR-12): Reconnection Summary generation, outage duration tracking, and lease reconciliation.
5. Automatic queue drain upon reconnection: 100% of pending outbox messages delivered without duplicates.
"""

from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
from pathlib import Path
import pytest
import sys
import threading
import time

# Ensure repository root containing 'edge' is on sys.path
_current = Path(__file__).resolve().parent
while _current.parent != _current:
    if (_current / "edge" / "supervisor").is_dir():
        if str(_current) not in sys.path:
            sys.path.insert(0, str(_current))
        break
    _current = _current.parent

from edge.store.local_store import EdgeLocalStore
from edge.supervisor.supervisor import EdgeSupervisor
from edge.supervisor.outbox_worker import OutboxUploadWorker
from edge.supervisor.reconnection import ReconnectionManager
from edge.mock.mock_robot import MockRobot


class MockCloudServer(BaseHTTPRequestHandler):
    received_requests = []
    response_code = 201

    def log_message(self, format, *args):
        pass  # suppress console noise

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        parsed = json.loads(body.decode("utf-8")) if body else {}
        MockCloudServer.received_requests.append({
            "path": self.path,
            "body": parsed
        })

        self.send_response(MockCloudServer.response_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"accepted": True, "duplicate": False}).encode("utf-8"))


@pytest.fixture
def mock_cloud():
    """Spins up a lightweight local HTTP server stand-in for Cloud."""
    MockCloudServer.received_requests.clear()
    MockCloudServer.response_code = 201
    server = HTTPServer(("127.0.0.1", 0), MockCloudServer)
    port = server.server_port
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def clean_db(tmp_path):
    db_file = tmp_path / "test_resilience.db"
    return str(db_file)


@pytest.fixture
def supervisor(clean_db, mock_cloud, tmp_path):
    artifacts_dir = tmp_path / "artifacts"
    sup = EdgeSupervisor(
        cluster_id="east-wing",
        cloud_url=mock_cloud,
        db_path=clean_db,
        slm_mode="mock"
    )
    sup.artifact_packager.base_dir = artifacts_dir
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    return sup


def _make_lease(lease_id="lease-sf-001", version=1):
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(minutes=10)
    expiry = now + timedelta(minutes=15)
    return {
        "lease_id": lease_id,
        "version": version,
        "priority": "routine",
        "deadline_ts": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expiry_ts": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "allowed_zones": ["central-supply", "corridor-2B", "2B-12"],
        "forbidden_zones": ["mri-suite"],
        "speed_limit": 0.8,
        "assigned_robots": ["R1"],
        "task_sequence": [
            {"cmd": "go_to", "robot": "R1", "params": {"to": "central-supply"}},
            {"cmd": "go_to", "robot": "R1", "params": {"to": "2B-12"}}
        ],
        "cluster_id": "east-wing",
        "when_to_escalate": ["door_closed", "deadline_at_risk"]
    }


def test_store_and_forward_persists_before_network_send(clean_db):
    """FR-10, Invariant 5: Messages are persisted to SQLite BEFORE any network call."""
    store = EdgeLocalStore(clean_db)
    # Point to an unreachable port to guarantee network error
    supervisor = EdgeSupervisor(
        cluster_id="east-wing",
        cloud_url="http://127.0.0.1:59999",
        db_path=clean_db
    )

    esc_msg = {
        "type": "CLARIFY",
        "message_id": "msg-persist-first-001",
        "lease_version": 1,
        "task_id": "task-test-01",
        "payload": {
            "question": "Which room?",
            "possible_readings": ["Room 1", "Room 2"]
        }
    }

    # Send should return False due to unreachable network
    delivered = supervisor.send_escalation_to_cloud(esc_msg)
    assert not delivered, "Expected failed network delivery"

    # Crucial assertion: Message MUST still exist safely on local SQLite disk
    pending = store.get_pending_outbox()
    assert len(pending) == 1
    assert pending[0]["message_id"] == "msg-persist-first-001"
    assert pending[0]["message_type"] == "CLARIFY"


def test_simulated_10min_outage_zero_data_loss(supervisor):
    """FR-6, FR-10: 10-minute network outage simulation with zero message/decision loss."""
    # 1. Sever network connection (simulating 10-minute WAN partition)
    supervisor.simulate_network_partition()
    assert not supervisor.outbox_worker.is_network_available()

    # 2. Execute multiple missions and escalations while completely disconnected
    lease_1 = _make_lease("lease-outage-01")
    res_1 = supervisor.process_lease(lease_1)
    assert res_1["status"] == "COMPLETED"

    # Trigger a CLARIFY escalation
    clarify_res = supervisor.trigger_clarify(
        task_id="task-outage-clarify",
        question="Which delivery cabinet?",
        possible_readings=["Cabinet A", "Cabinet B"],
        lease_version=1
    )
    assert clarify_res["status"] == "ESCALATED_CLARIFY"

    # Trigger an UNEXPECTED_EVENT escalation
    ue_res = supervisor.trigger_unexpected_event(
        task_id="task-outage-ue",
        what="Water leak in transit corridor",
        where="corridor-2B",
        robots_saw=["R1 detected surface reflection"],
        lease_version=1
    )
    assert ue_res["status"] == "ESCALATED_UNEXPECTED"

    # 3. Verify Local SQLite State (Zero Data Loss)
    # Check decision log contains rows
    with supervisor.store._get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM decision_log")
        dec_count = cursor.fetchone()["count"]
        assert dec_count > 0, "All decisions must be saved to disk during outage"

        cursor.execute("SELECT COUNT(*) as count FROM robot_commands")
        cmd_count = cursor.fetchone()["count"]
        assert cmd_count > 0, "All robot commands must be recorded during outage"

    # Check pending outbox has exactly 3 records (1 OUTPUT_READY + 1 CLARIFY + 1 UNEXPECTED_EVENT)
    pending = supervisor.store.get_pending_outbox()
    assert len(pending) == 3
    msg_types = [p["message_type"] for p in pending]
    assert "OUTPUT_READY" in msg_types
    assert "CLARIFY" in msg_types
    assert "UNEXPECTED_EVENT" in msg_types


def test_exponential_backoff_on_connection_failures(clean_db):
    """NFR-2, FR-10: Outbox worker increases backoff delay exponentially on failures."""
    store = EdgeLocalStore(clean_db)
    # Enqueue a test escalation
    store.enqueue_outbox(
        message_id="msg-backoff-01",
        lease_id="lease-bo",
        message_type="OUTPUT_READY",
        payload={
            "type": "OUTPUT_READY",
            "message_id": "msg-backoff-01",
            "lease_version": 1,
            "task_id": "task-bo",
            "payload": {"result_ref": "storage://results/task-bo/proof.jpg"}
        }
    )

    # Point to closed port
    worker = OutboxUploadWorker(
        store=store,
        cloud_url="http://127.0.0.1:59998",
        poll_interval_sec=0.1,
        base_backoff_sec=0.2,
        max_backoff_sec=5.0,
        backoff_factor=2.0
    )

    # Failure 1
    worker.process_pending_outbox_once()
    assert worker.consecutive_failures == 1
    assert worker.current_backoff_delay == pytest.approx(0.2, rel=0.1)

    # Failure 2 (backoff doubles)
    worker.process_pending_outbox_once()
    assert worker.consecutive_failures == 2
    assert worker.current_backoff_delay == pytest.approx(0.4, rel=0.1)

    # Failure 3 (backoff quadruples)
    worker.process_pending_outbox_once()
    assert worker.consecutive_failures == 3
    assert worker.current_backoff_delay == pytest.approx(0.8, rel=0.1)

    # Message must still be in outbox
    assert len(store.get_pending_outbox()) == 1


def test_reconnection_summary_generation_and_fleet_reconciliation(supervisor):
    """FR-12: Verify Reconnection Summary adheres to SPEC-P2 Sec 2.4."""
    # 1. Simulate 600-second (10-minute) disconnection
    now = time.time()
    disconnection_time = now - 600.0
    supervisor.simulate_network_partition()
    supervisor.reconnection_mgr.disconnected_at = disconnection_time

    # 2. Run and complete a lease during this outage
    lease = _make_lease("lease-recon-01")
    supervisor.process_lease(lease)

    # 3. Trigger network recovery
    summary = supervisor.simulate_network_recovery()

    # 4. Verify Summary Shape & Data
    assert summary["cluster_id"] == "east-wing"
    assert summary["disconnected_duration_seconds"] >= 600.0
    assert "lease-recon-01" in summary["completed_tasks"]
    assert summary["pending_uploads_count"] >= 1

    # Check active leases robot positions
    assert len(summary["active_leases"]) >= 1
    lease_summary = summary["active_leases"][0]
    assert lease_summary["lease_id"] == "lease-recon-01"
    assert "R1" in lease_summary["robot_positions"]
    assert len(lease_summary["robot_positions"]["R1"]) == 2


def test_automatic_outbox_replay_and_drain_upon_reconnect(supervisor, mock_cloud):
    """FR-10, FR-12: Outbox worker drains all pending messages in FIFO order upon reconnect."""
    store = supervisor.store

    # 1. Pause network
    supervisor.set_network_available(False)

    # 2. Enqueue 4 escalations
    for i in range(1, 5):
        msg = {
            "type": "CLARIFY",
            "message_id": f"msg-drain-{i:03d}",
            "lease_version": 1,
            "task_id": f"task-drain-{i}",
            "payload": {
                "question": f"Question {i}",
                "possible_readings": [f"Choice A{i}", f"Choice B{i}"]
            }
        }
        supervisor.send_escalation_to_cloud(msg)

    # Verify all 4 are PENDING in SQLite
    pending_before = store.get_pending_outbox()
    assert len(pending_before) == 4
    assert len(MockCloudServer.received_requests) == 0

    # 3. Restore network & flush
    supervisor.set_network_available(True)
    drained = supervisor.flush_outbox(timeout=5.0)
    assert drained, "Expected outbox to drain cleanly"

    # 4. Verify outbox is now empty (0 pending items)
    pending_after = store.get_pending_outbox()
    assert len(pending_after) == 0

    # 5. Verify all 4 messages arrived at Cloud in exact FIFO order
    assert len(MockCloudServer.received_requests) == 4
    received_ids = [r["body"]["message_id"] for r in MockCloudServer.received_requests]
    assert received_ids == ["msg-drain-001", "msg-drain-002", "msg-drain-003", "msg-drain-004"]

    # 6. Verify SQLite table status has been updated to UPLOADED with uploaded_at timestamp
    with store._get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, uploaded_at FROM outbox WHERE message_id LIKE 'msg-drain-%'")
        rows = cursor.fetchall()
        assert len(rows) == 4
        for r in rows:
            assert r["status"] == "UPLOADED"
            assert r["uploaded_at"] is not None
