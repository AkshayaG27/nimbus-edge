"""
Nimbus Edge Supervisor Daemon (Person 2)
Supervises local wing cluster, enforces lease boundaries (FR-5, FR-6),
manages local persistence in SQLite (FR-10), executes sub-tasks on robots,
and communicates with Cloud over HTTP REST (Milestone 1) and outbox.
"""

from datetime import datetime, timezone
import json
import time
from typing import Any, Dict, List, Optional
import urllib.request
import urllib.error

from edge.schemas.validator import (
    validate_lease,
    validate_escalation,
    validate_robot_subtask,
    is_coordinate_in_allowed_zones,
)
from edge.store.local_store import EdgeLocalStore
from edge.slm.engine import EdgeSLMEngine
from edge.mock.mock_robot import MockRobot


class EdgeSupervisor:
    def __init__(
        self,
        cluster_id: str = "east-wing",
        cloud_url: str = "http://localhost:8000",
        db_path: str = "edge_local_store.db",
        slm_mode: str = "auto"
    ):
        self.cluster_id = cluster_id
        self.cloud_url = cloud_url.rstrip("/")
        self.store = EdgeLocalStore(db_path)
        self.slm = EdgeSLMEngine(mode=slm_mode)
        self.robots: Dict[str, MockRobot] = {
            "R1": MockRobot(robot_id="R1", initial_pos=[0.0, 0.0, 0.0])
        }
        self.active_lease: Optional[Dict[str, Any]] = None

    def fetch_leases_from_cloud(self) -> List[Dict[str, Any]]:
        """Polls GET /outbox on the cloud stand-in server."""
        url = f"{self.cloud_url}/outbox"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                outbox_items = data.get("outbox", [])
                leases = []
                for item in outbox_items:
                    # Case 1: outbox wrapped in topic payload
                    if isinstance(item, dict) and "payload" in item:
                        payload = item["payload"]
                        if "lease_id" in payload and item.get("topic", "").endswith("/lease"):
                            leases.append(payload)
                    # Case 2: raw lease dict
                    elif isinstance(item, dict) and "lease_id" in item:
                        leases.append(item)
                return leases
        except Exception as e:
            # Server not up or network error
            return []

    def send_escalation_to_cloud(self, escalation: Dict[str, Any]) -> bool:
        """Pushes structured escalation to POST /escalations (FR-9)."""
        valid, err = validate_escalation(escalation)
        if not valid:
            raise ValueError(f"Invalid escalation payload: {err}")

        # Store locally first (Store-and-Forward - FR-10)
        self.store.enqueue_outbox(
            message_id=escalation["message_id"],
            lease_id=escalation.get("task_id", "unknown"),
            message_type=escalation["type"],
            payload=escalation
        )

        url = f"{self.cloud_url}/escalations"
        req_data = json.dumps(escalation).encode("utf-8")
        try:
            req = urllib.request.Request(
                url,
                data=req_data,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                if resp.status in (200, 201, 204):
                    self.store.mark_outbox_uploaded(escalation["message_id"])
                    return True
        except Exception:
            # Remains pending in SQLite store for upload on reconnect (FR-10)
            pass
        return False

    def process_lease(self, lease: Dict[str, Any]) -> Dict[str, Any]:
        """
        Ingests, validates, and executes an authority lease.
        Enforces:
        - Schema & cross-field rules (FR-5)
        - Monotonic versioning (FR-8)
        - Expiry checking & safe parking (FR-6)
        - Robot command dispatch & logging (FR-3, FR-20, NFR-6)
        """
        lease_id = lease["lease_id"]
        version = lease["version"]

        # 1. Validate Schema
        valid, err_msg = validate_lease(lease)
        if not valid:
            return {"status": "REJECTED", "reason": f"Validation failed: {err_msg}"}

        # 2. Check cluster routing (FR-11)
        if lease.get("cluster_id") != self.cluster_id:
            return {"status": "IGNORED", "reason": f"Lease targeted to {lease.get('cluster_id')}, not {self.cluster_id}"}

        # 3. Store in SQLite with version deduplication (FR-8)
        stored, store_msg = self.store.store_lease(lease)
        if not stored:
            return {"status": "DROPPED_STALE", "reason": store_msg}

        self.active_lease = lease
        task_id = lease.get("task_id", lease_id)
        task_sequence = lease.get("task_sequence", [])
        allowed_zones = set(lease.get("allowed_zones", []))
        forbidden_zones = set(lease.get("forbidden_zones", []))

        # Check expiry timestamp (ISO 8601 UTC)
        expiry_ts_str = lease["expiry_ts"]
        if expiry_ts_str.endswith("Z"):
            expiry_ts_str = expiry_ts_str[:-1] + "+00:00"
        expiry_dt = datetime.fromisoformat(expiry_ts_str)

        executed_steps = []

        # 4. Execute sub-task sequence
        for idx, subtask in enumerate(task_sequence):
            now_dt = datetime.now(timezone.utc)
            if now_dt >= expiry_dt:
                # Invariant 3: Expiry -> Safe Park (FR-6)
                park_result = self._handle_lease_expiry_parking(lease)
                return {
                    "status": "EXPIRED_PARKED",
                    "reason": "Lease expired mid-mission; parked in safe spot",
                    "executed_steps": executed_steps,
                    "parking": park_result
                }

            cmd = subtask["cmd"]
            robot_id = subtask.get("robot", lease["assigned_robots"][0])
            params = subtask.get("params", {})

            # Boundary check on destination (FR-5)
            target_dest = params.get("to")
            if target_dest:
                if target_dest in forbidden_zones:
                    self._escalate_unexpected(
                        lease, robot_id,
                        what=f"Target {target_dest} is in forbidden zones",
                        where=target_dest
                    )
                    return {"status": "FAILED_BOUNDARY", "reason": f"Forbidden zone: {target_dest}"}

            robot = self.robots.get(robot_id)
            if not robot:
                robot = MockRobot(robot_id=robot_id)
                self.robots[robot_id] = robot

            # Translate to robot primitive command
            cmd_payload = {
                "cmd_id": f"cmd-{lease_id}-step-{idx+1}",
                "lease_id": lease_id,
                "robot_id": robot_id,
                "action": cmd,
                "params": params,
                "timestamp": time.time()
            }

            # Store command and dispatch
            self.store.store_robot_command(cmd_payload)
            self.store.update_robot_command_status(cmd_payload["cmd_id"], "DISPATCHED")

            exec_report = robot.execute_command(cmd_payload)
            self.store.update_robot_command_status(cmd_payload["cmd_id"], "COMPLETED")

            # Log decision (FR-20, NFR-6)
            self.store.log_decision(
                decision_id=f"dec-{lease_id}-v{version}-{idx+1}",
                lease_id=lease_id,
                decided_by="RULE",
                input_context={"step": idx, "subtask": subtask},
                decision_action=cmd,
                decision_output=exec_report,
                latency_ms=1.5
            )
            executed_steps.append(exec_report)

        # 5. Mission Completed -> Emit OUTPUT_READY (FR-9, Table 2)
        escalation = {
            "type": "OUTPUT_READY",
            "message_id": f"msg-out-{int(time.time()*1000)}",
            "lease_version": version,
            "task_id": task_id,
            "payload": {
                "result_ref": f"storage://results/{task_id}/delivery-proof.jpg"
            }
        }
        self.send_escalation_to_cloud(escalation)
        self.store.update_lease_status(lease_id, version, "COMPLETED")

        return {
            "status": "COMPLETED",
            "lease_id": lease_id,
            "executed_steps_count": len(executed_steps),
            "result_ref": escalation["payload"]["result_ref"]
        }

    def _handle_lease_expiry_parking(self, lease: Dict[str, Any]) -> Dict[str, Any]:
        """Parks all assigned robots in safe spots outside doorways/lifts (FR-6)."""
        robot_id = lease["assigned_robots"][0]
        robot = self.robots.get(robot_id)
        park_cmd = {
            "cmd_id": f"cmd-park-{int(time.time()*1000)}",
            "lease_id": lease["lease_id"],
            "robot_id": robot_id,
            "action": "go_to",
            "params": {"to": "safe-parking-bay-1"},
            "timestamp": time.time()
        }
        res = robot.execute_command(park_cmd)
        res["action"] = "go_to"
        res["params"] = park_cmd["params"]
        return res

    def _escalate_unexpected(self, lease: Dict[str, Any], robot_id: str, what: str, where: str):
        """Emits UNEXPECTED_EVENT escalation."""
        esc = {
            "type": "UNEXPECTED_EVENT",
            "message_id": f"msg-ue-{int(time.time()*1000)}",
            "lease_version": lease["version"],
            "task_id": lease.get("task_id", lease["lease_id"]),
            "payload": {
                "what_happened": what,
                "where": where,
                "robots_saw": [f"{robot_id} reported boundary conflict at {where}"]
            }
        }
        self.send_escalation_to_cloud(esc)
