"""
Nimbus Edge Supervisor Daemon (Person 2)
Supervises local wing cluster, enforces lease boundaries (FR-5, FR-6),
manages local persistence in SQLite (FR-10), executes sub-tasks on robots,
runs local tactical autonomy via SLM (FR-3, FR-4), packages camera artifacts (FR-9),
manages store-and-forward outbox uploads (FR-10, NFR-2), and handles
reconnection protocols with Cloud (FR-12).
"""

from datetime import datetime, timezone
import json
import re
import uuid
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
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
from edge.supervisor.geofence import SpatialGeofenceValidator
from edge.supervisor.parking import SafeParkingEngine
from edge.supervisor.artifacts import ArtifactPackager
from edge.supervisor.outbox_worker import OutboxUploadWorker
from edge.supervisor.reconnection import ReconnectionManager


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
        self.geofence = SpatialGeofenceValidator()
        self.parking_engine = SafeParkingEngine()
        self.artifact_packager = ArtifactPackager()
        self.robots: Dict[str, MockRobot] = {
            "R1": MockRobot(robot_id="R1", initial_pos=[0.0, 0.0, 0.0])
        }
        self.active_lease: Optional[Dict[str, Any]] = None

        # Store-and-Forward Outbox Worker (FR-10, NFR-2)
        self.outbox_worker = OutboxUploadWorker(
            store=self.store,
            cloud_url=self.cloud_url,
            auto_start=False
        )

        # Reconnection Manager (FR-12)
        self.reconnection_mgr = ReconnectionManager(
            cluster_id=self.cluster_id,
            store=self.store,
            robots=self.robots,
            cloud_url=self.cloud_url
        )

    @staticmethod
    def _extract_task_id(lease: Dict[str, Any]) -> str:
        """Extracts canonical task_id ('task-000001') from lease document or lease_id ('lease-task-000001-v1')."""
        if "task_id" in lease:
            return lease["task_id"]
        lid = lease.get("lease_id", "")
        if lid.startswith("lease-") and "-v" in lid:
            return lid[len("lease-"):lid.rindex("-v")]
        return lid

    def start_outbox_worker(self):
        """Starts background upload worker thread."""
        self.outbox_worker.start()

    def stop_outbox_worker(self, timeout: float = 3.0):
        """Stops background upload worker thread."""
        self.outbox_worker.stop(timeout=timeout)

    def flush_outbox(self, timeout: float = 5.0) -> bool:
        """Synchronously drains all pending outbox entries."""
        return self.outbox_worker.flush(timeout=timeout)

    def set_network_available(self, available: bool):
        """Toggles network connectivity simulation."""
        self.outbox_worker.set_network_available(available)
        if not available:
            self.reconnection_mgr.record_disconnection()
        else:
            self.reconnection_mgr.record_reconnection()

    def simulate_network_partition(self, duration_sec: Optional[float] = None):
        """Simulates WAN disconnection for resilience testing (FR-6, FR-10)."""
        self.set_network_available(False)

    def simulate_network_recovery(self) -> Dict[str, Any]:
        """Simulates WAN restoration and triggers reconnection summary (FR-12)."""
        summary = self.reconnection_mgr.record_reconnection()
        self.outbox_worker.resume_network()
        return summary

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
        except Exception:
            return []

    def send_escalation_to_cloud(self, escalation: Dict[str, Any]) -> bool:
        """
        Pushes structured escalation to POST /escalations (FR-9).
        Enforces Store-and-Forward (Invariant 5, FR-10): writes to local SQLite disk
        BEFORE attempting network transmission.
        """
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

        # If network is currently offline/paused, leave in SQLite for worker replay
        if not self.outbox_worker.is_network_available():
            return False

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
        except urllib.error.HTTPError as he:
            try:
                err_text = he.read().decode("utf-8", errors="ignore")
                err_body = json.loads(err_text)
                if err_body.get("duplicate") is True:
                    self.store.mark_outbox_uploaded(escalation["message_id"])
                    return True
                print(f"[{self.cluster_id}] Escalation HTTP error {he.code}: {err_text}")
            except Exception:
                print(f"[{self.cluster_id}] Escalation HTTP error {he.code}")
        except Exception as e:
            # Remains pending in SQLite store for upload on reconnect (FR-10)
            print(f"[{self.cluster_id}] Escalation network error: {e}")
        return False

    def trigger_clarify(
        self,
        task_id: str,
        question: str,
        possible_readings: List[str],
        lease_version: int = 1
    ) -> Dict[str, Any]:
        """
        Emits a strictly compliant CLARIFY escalation (FR-9, Table 2).
        Payload requires: question, possible_readings.
        """
        esc = {
            "type": "CLARIFY",
            "message_id": f"msg-clarify-{task_id}-{int(time.time()*1000)}",
            "lease_version": lease_version,
            "task_id": task_id,
            "payload": {
                "question": question,
                "possible_readings": possible_readings
            }
        }
        self.send_escalation_to_cloud(esc)
        if self.active_lease and self.active_lease.get("lease_id"):
            self.store.update_lease_status(
                self.active_lease["lease_id"],
                lease_version,
                "ESCALATED_CLARIFY"
            )
        return {
            "status": "ESCALATED_CLARIFY",
            "task_id": task_id,
            "question": question,
            "possible_readings": possible_readings,
            "escalation": esc
        }

    def trigger_unexpected_event(
        self,
        task_id: str,
        what: str,
        where: str,
        robots_saw: Optional[List[str]] = None,
        lease_version: int = 1
    ) -> Dict[str, Any]:
        """
        Emits a strictly compliant UNEXPECTED_EVENT escalation (FR-9, Table 2).
        Payload requires: what_happened, where, robots_saw.
        """
        saw = robots_saw or [f"Robot observed: {what} at {where}"]
        esc = {
            "type": "UNEXPECTED_EVENT",
            "message_id": f"msg-ue-{task_id}-{int(time.time()*1000)}",
            "lease_version": lease_version,
            "task_id": task_id,
            "payload": {
                "what_happened": what,
                "where": where,
                "robots_saw": saw
            }
        }
        self.send_escalation_to_cloud(esc)
        if self.active_lease and self.active_lease.get("lease_id"):
            self.store.update_lease_status(
                self.active_lease["lease_id"],
                lease_version,
                "ESCALATED_UNEXPECTED"
            )
        return {
            "status": "ESCALATED_UNEXPECTED",
            "task_id": task_id,
            "reason": what,
            "where": where,
            "escalation": esc
        }

    def trigger_output_ready(
        self,
        task_id: str,
        lease_version: int = 1,
        result_ref: Optional[str] = None,
        metrics: Optional[Dict[str, Any]] = None,
        executed_steps_count: int = 0
    ) -> Dict[str, Any]:
        """
        Emits a strictly compliant OUTPUT_READY escalation (FR-9, Table 2).
        Payload requires: result_ref.
        """
        ref = result_ref or f"storage://results/{task_id}/delivery-proof.jpg"
        esc = {
            "type": "OUTPUT_READY",
            "message_id": f"msg-out-{task_id}-{int(time.time()*1000)}",
            "lease_version": lease_version,
            "task_id": task_id,
            "payload": {
                "result_ref": ref
            }
        }
        self.send_escalation_to_cloud(esc)
        if self.active_lease and self.active_lease.get("lease_id"):
            self.store.update_lease_status(
                self.active_lease["lease_id"],
                lease_version,
                "COMPLETED"
            )
        res = {
            "status": "COMPLETED",
            "task_id": task_id,
            "lease_id": self.active_lease.get("lease_id", task_id) if self.active_lease else task_id,
            "executed_steps_count": executed_steps_count,
            "result_ref": ref,
            "escalation": esc
        }
        if metrics:
            res["metrics"] = metrics
        return res

    def handle_cloud_replan(self, new_lease: Dict[str, Any]) -> Dict[str, Any]:
        """
        Receives a revised lease from Cloud following an escalation (FR-8, Invariant 2).
        Validates that new lease version is strictly higher than previous version,
        cancels previous in-flight commands, and executes the re-planned mission.
        """
        new_version = new_lease.get("version", 1)
        lease_id = new_lease.get("lease_id", "")
        old_lease = self.store.get_lease(lease_id)
        if old_lease:
            old_version = old_lease.get("version", 0)
            if new_version <= old_version:
                return {
                    "status": "REJECTED_STALE",
                    "reason": f"Replanned lease version {new_version} <= current version {old_version}"
                }
            # Cancel any remaining commands from older version
            self.store.cancel_active_commands(lease_id)

        # Process the replanned lease
        return self.process_lease(new_lease)

    def process_lease(
        self,
        lease: Dict[str, Any],
        simulated_now: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Ingests, validates, and executes an authority lease.
        Enforces:
        - Schema & cross-field rules (FR-5)
        - Spatial boundary & forbidden zone confinement (FR-5)
        - Monotonic versioning (FR-8)
        - Local tactical autonomy on obstacles/faults (FR-3, FR-4)
        - Ambiguity detection & CLARIFY escalation (FR-9)
        - Expiry watchdog & safe parking in verified non-transit recesses (FR-6)
        - Camera artifact creation and OUTPUT_READY escalation (FR-9)
        - Robot command dispatch & logging (FR-3, FR-20, NFR-6)
        """
        mission_start_time = time.time()
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
        task_id = self._extract_task_id(lease)
        task_sequence = lease.get("task_sequence", [])
        allowed_zones = lease.get("allowed_zones", [])
        forbidden_zones = lease.get("forbidden_zones", [])

        # Initial check-in on lease acceptance (Item 1)
        self.send_checkin_to_cloud(lease)
        last_checkin_time = time.time()

        # Check expiry timestamp (ISO 8601 UTC)
        expiry_ts_str = lease["expiry_ts"]
        if expiry_ts_str.endswith("Z"):
            expiry_ts_str = expiry_ts_str[:-1] + "+00:00"
        expiry_dt = datetime.fromisoformat(expiry_ts_str)

        executed_steps = []
        active_robot_id = lease.get("assigned_robots", ["R1"])[0]
        active_robot = self.robots.get(active_robot_id)
        if not active_robot:
            active_robot = MockRobot(robot_id=active_robot_id)
            self.robots[active_robot_id] = active_robot
        battery_start = active_robot.battery_pct
        tactical_events_count = 0

        # 4. Execute sub-task sequence
        idx = 0
        while idx < len(task_sequence):
            subtask = task_sequence[idx]
            now_dt = simulated_now or datetime.now(timezone.utc)
            if now_dt >= expiry_dt:
                # Invariant 3: Expiry -> Safe Park (FR-6)
                park_result = self._handle_lease_expiry_parking(lease)
                return {
                    "status": "EXPIRED_PARKED",
                    "reason": "Lease expired mid-mission; parked in safe spot",
                    "executed_steps": executed_steps,
                    "parking": park_result
                }

            # Periodic check-in every 5s during active execution (Item 1)
            now_loop = time.time()
            if now_loop - last_checkin_time >= 5.0:
                ok, status_reason, checkin_reply = self.send_checkin_to_cloud(lease)
                last_checkin_time = now_loop
                if status_reason == "stale_version":
                    self.active_lease = None
                    return {
                        "status": "DROPPED_STALE",
                        "reason": f"Check-in received 409: outdated version {version}, current is {checkin_reply.get('current')}"
                    }
                elif status_reason == "renewed" and "renewed_until" in checkin_reply:
                    new_exp = checkin_reply["renewed_until"]
                    lease["expiry_ts"] = new_exp
                    if new_exp.endswith("Z"):
                        new_exp = new_exp[:-1] + "+00:00"
                    expiry_dt = datetime.fromisoformat(new_exp)

            cmd = subtask["cmd"]
            robot_id = subtask.get("robot", active_robot_id)
            params = subtask.get("params", {})

            # Check for ambiguous destination or instructions (FR-9 CLARIFY trigger)
            if params.get("ambiguous") is True or cmd == "clarify":
                question = params.get("question", f"Destination '{params.get('to')}' is ambiguous")
                readings = params.get("possible_readings", [f"Bed {params.get('to', 'unknown')}-A", f"Bed {params.get('to', 'unknown')}-B"])
                clarify_res = self.trigger_clarify(
                    task_id=task_id,
                    question=question,
                    possible_readings=readings,
                    lease_version=version
                )
                return {
                    "status": "ESCALATED_CLARIFY",
                    "reason": question,
                    "possible_readings": readings,
                    "executed_steps": executed_steps,
                    "escalation": clarify_res["escalation"]
                }

            # Spatial boundary check on sub-task (FR-5)
            is_valid_boundary, boundary_err = self.geofence.validate_subtask(
                subtask,
                allowed_zones=allowed_zones,
                forbidden_zones=forbidden_zones
            )
            if not is_valid_boundary:
                target_dest = params.get("to") or params.get("target") or "unknown"
                if target_dest in forbidden_zones:
                    reason_str = f"Forbidden zone: {target_dest}"
                else:
                    reason_str = f"Boundary violation: {boundary_err}"

                # Log decision for audit (FR-20, NFR-6)
                self.store.log_decision(
                    decision_id=f"dec-bound-{lease_id}-v{version}-{idx+1}",
                    lease_id=lease_id,
                    decided_by="RULE",
                    input_context={"step": idx, "subtask": subtask, "allowed": allowed_zones, "forbidden": forbidden_zones},
                    decision_action="REJECT_BOUNDARY",
                    decision_output={"error": boundary_err, "reason": reason_str},
                    latency_ms=0.5
                )

                # Emit UNEXPECTED_EVENT escalation (FR-9)
                self._escalate_unexpected(
                    lease, robot_id,
                    what=reason_str,
                    where=str(target_dest)
                )
                return {"status": "FAILED_BOUNDARY", "reason": reason_str}

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

            self.store.store_robot_command(cmd_payload)
            self.store.update_robot_command_status(cmd_payload["cmd_id"], "DISPATCHED")

            # Execution & Tactical Autonomy Loop (FR-3, FR-4)
            step_completed = False
            wait_attempts = 0

            while not step_completed:
                exec_report = robot.execute_command(cmd_payload)

                if exec_report["status"] in ("COMPLETED", "IDLE"):
                    self.store.update_robot_command_status(cmd_payload["cmd_id"], "COMPLETED")
                    self.store.log_decision(
                        decision_id=f"dec-{lease_id}-v{version}-{idx+1}-{len(executed_steps)+1}",
                        lease_id=lease_id,
                        decided_by="RULE",
                        input_context={"step": idx, "subtask": subtask},
                        decision_action=cmd,
                        decision_output=exec_report,
                        latency_ms=1.5
                    )
                    executed_steps.append(exec_report)
                    step_completed = True

                elif exec_report["status"] == "BLOCKED" or exec_report["status"] == "FAILED":
                    tactical_events_count += 1
                    self.store.update_robot_command_status(cmd_payload["cmd_id"], exec_report["status"])

                    blocked_zone = exec_report.get("blocked_zone", robot.current_zone)
                    target_dest = params.get("to") or params.get("target") or robot.current_zone
                    alt_wps = self.geofence.find_alternative_waypoints(
                        current_zone=robot.current_zone,
                        destination_zone=str(target_dest),
                        allowed_zones=allowed_zones,
                        forbidden_zones=forbidden_zones,
                        blocked_zone=blocked_zone
                    )
                    time_remaining = (expiry_dt - now_dt).total_seconds()

                    obs_type = exec_report.get("obstacle_type", "transient" if "transient" in exec_report.get("message", "").lower() or "laundry" in exec_report.get("message", "").lower() else "persistent")
                    event_desc = f"{obs_type} obstacle: {exec_report.get('message', 'Obstacle encountered')}"
                    if exec_report.get("fault"):
                        event_desc = f"fault: {exec_report.get('message', 'Hardware fault')}"

                    tactical_ctx = {
                        "lease_id": lease_id,
                        "version": version,
                        "time_remaining_sec": time_remaining,
                        "robot_id": robot_id,
                        "current_pos": list(robot.pose),
                        "current_zone": robot.current_zone,
                        "allowed_zones": allowed_zones,
                        "forbidden_zones": forbidden_zones,
                        "allowed_waypoints": alt_wps,
                        "assigned_robots": lease.get("assigned_robots", [robot_id]),
                        "event_description": event_desc,
                        "wait_count": wait_attempts
                    }

                    # Consult Tactical SLM / fallback rules
                    decision, decided_by, latency_ms = self.slm.evaluate_decision(tactical_ctx)

                    # Log tactical decision in SQLite (FR-20, NFR-6)
                    self.store.log_decision(
                        decision_id=f"dec-tac-{lease_id}-v{version}-{idx+1}-{wait_attempts+1}-{int(time.time()*1000)}",
                        lease_id=lease_id,
                        decided_by=decided_by,
                        input_context=tactical_ctx,
                        decision_action=decision.get("action", "UNKNOWN"),
                        decision_output=decision,
                        latency_ms=latency_ms
                    )

                    action = decision.get("action")

                    # Tactical Action 1: LOCAL_WAIT (FR-4)
                    if action == "LOCAL_WAIT":
                        wait_attempts += 1
                        wait_duration = 5.0
                        wait_cmd = {
                            "cmd_id": f"cmd-wait-{lease_id}-{idx+1}-{wait_attempts}-{int(time.time()*1000)}",
                            "lease_id": lease_id,
                            "robot_id": robot_id,
                            "action": "wait",
                            "params": {"duration_sec": wait_duration},
                            "timestamp": time.time()
                        }
                        self.store.store_robot_command(wait_cmd)
                        robot.execute_command(wait_cmd)
                        self.store.update_robot_command_status(wait_cmd["cmd_id"], "COMPLETED")
                        # Loop back to retry current step
                        continue

                    # Tactical Action 2: LOCAL_DETOUR (FR-4, FR-5)
                    elif action == "LOCAL_DETOUR":
                        detour_wp = decision.get("target_waypoint")
                        # Enforce spatial boundary guard (FR-5, Invariant 1)
                        is_valid_detour, detour_err, _ = self.geofence.validate_destination(
                            detour_wp,
                            allowed_zones=allowed_zones,
                            forbidden_zones=forbidden_zones
                        )
                        if not is_valid_detour:
                            self._escalate_unexpected(
                                lease, robot_id,
                                what=f"Detour boundary violation: {detour_err}",
                                where=str(detour_wp)
                            )
                            return {"status": "FAILED_BOUNDARY", "reason": f"Detour waypoint '{detour_wp}' violates lease boundaries: {detour_err}"}

                        detour_cmd = {
                            "cmd_id": f"cmd-detour-{lease_id}-{idx+1}-{int(time.time()*1000)}",
                            "lease_id": lease_id,
                            "robot_id": robot_id,
                            "action": "go_to",
                            "params": {"to": detour_wp},
                            "timestamp": time.time()
                        }
                        self.store.store_robot_command(detour_cmd)
                        detour_report = robot.execute_command(detour_cmd)
                        self.store.update_robot_command_status(detour_cmd["cmd_id"], "COMPLETED")
                        executed_steps.append(detour_report)

                        # Detour executed! Now continue to original destination
                        continue

                    # Tactical Action 3: SWAP_ROBOT (FR-3, FR-4)
                    elif action == "SWAP_ROBOT":
                        faulted_id = robot_id
                        new_robot_id = decision.get("target_waypoint")
                        assigned_robots = lease.get("assigned_robots", [])
                        if new_robot_id in assigned_robots and new_robot_id != faulted_id:
                            # Halt faulted robot
                            robot.execute_command({
                                "cmd_id": f"cmd-stop-{faulted_id}-{int(time.time()*1000)}",
                                "action": "stop",
                                "params": {"reason": f"Fault occurred; swapping to {new_robot_id}"}
                            })
                            robot_id = new_robot_id
                            active_robot_id = new_robot_id
                            if robot_id not in self.robots:
                                self.robots[robot_id] = MockRobot(robot_id=robot_id)
                            robot = self.robots[robot_id]

                            # Update remaining subtasks in sequence
                            for rem_sub in task_sequence[idx:]:
                                if rem_sub.get("robot") == faulted_id:
                                    rem_sub["robot"] = new_robot_id
                            cmd_payload["robot_id"] = new_robot_id
                            cmd_payload["cmd_id"] = f"cmd-{lease_id}-step-{idx+1}-{new_robot_id}"
                            continue
                        else:
                            self._escalate_unexpected(
                                lease, robot_id,
                                what="Robot swap failed: no candidate standby robot in lease",
                                where=robot.current_zone
                            )
                            return {"status": "ESCALATED_UNEXPECTED", "reason": "No standby robot available for swap"}

                    # Tactical Action 4: ESCALATE_CLARIFY (FR-9)
                    elif action == "ESCALATE_CLARIFY":
                        q_text = decision.get("reason", "Ambiguous location or instructions")
                        possible = [f"Deliver to bed {target_dest}", "Hold at supply depot"]
                        clarify_res = self.trigger_clarify(
                            task_id=task_id,
                            question=q_text,
                            possible_readings=possible,
                            lease_version=version
                        )
                        return {
                            "status": "ESCALATED_CLARIFY",
                            "reason": q_text,
                            "payload": clarify_res["escalation"]["payload"]
                        }

                    # Tactical Action 5: ESCALATE_UNEXPECTED (FR-9)
                    elif action == "ESCALATE_UNEXPECTED":
                        reason_msg = decision.get("reason", "Corridor blocked, no alternate detour")
                        self.trigger_unexpected_event(
                            task_id=task_id,
                            what=reason_msg,
                            where=str(target_dest),
                            robots_saw=[f"{robot_id} reported: {reason_msg} at {target_dest}"],
                            lease_version=version
                        )
                        return {"status": "ESCALATED_UNEXPECTED", "reason": reason_msg}

                    # Tactical Action 6: SAFE_PARK (FR-6)
                    elif action == "SAFE_PARK":
                        park_result = self._handle_lease_expiry_parking(lease)
                        return {
                            "status": "EXPIRED_PARKED",
                            "reason": "Lease expiry imminent (<30s); parked safely",
                            "executed_steps": executed_steps,
                            "parking": park_result
                        }
                    else:
                        return {"status": "FAILED", "reason": f"Unhandled tactical action: {action}"}

            idx += 1

        # 5. Mission Completed -> Generate Artifacts & Emit OUTPUT_READY (FR-9, Table 2)
        end_time = time.time()
        final_robot = self.robots.get(active_robot_id)
        final_pose = list(final_robot.pose) if final_robot else [0.0, 0.0, 0.0]
        battery_end = final_robot.battery_pct if final_robot else 90.0

        pkg = self.artifact_packager.package_output(
            task_id=task_id,
            lease_version=version,
            robot_id=active_robot_id,
            pose=final_pose,
            start_time=mission_start_time,
            end_time=end_time,
            executed_steps=executed_steps,
            battery_start=battery_start,
            battery_end=battery_end,
            tactical_events_count=tactical_events_count
        )

        out_res = self.trigger_output_ready(
            task_id=task_id,
            lease_version=version,
            result_ref=pkg["result_ref"],
            metrics=pkg["metrics"],
            executed_steps_count=len(executed_steps)
        )

        self.active_lease = None

        return {
            "status": "COMPLETED",
            "lease_id": lease_id,
            "task_id": task_id,
            "executed_steps_count": len(executed_steps),
            "result_ref": pkg["result_ref"],
            "artifact_path": pkg["file_path"],
            "artifact_sha256": pkg["sha256"],
            "metrics": pkg["metrics"]
        }

    def trigger_expiry_interrupt(
        self,
        lease_id: Optional[str] = None,
        reason: str = "Watchdog timer expired mid-mission"
    ) -> Dict[str, Any]:
        """
        Actively interrupts all in-flight errands when lease expiry occurs (FR-6).
        Halts robots, finds nearest safe parking recess outside corridors/doors,
        and escalates event.
        """
        target_lease = self.active_lease
        if not target_lease:
            return {"status": "ERROR", "reason": "No active lease held by supervisor"}

        lid = target_lease["lease_id"]
        version = target_lease["version"]

        # 1. Cancel in-flight commands in SQLite
        cancelled_count = self.store.cancel_active_commands(lid)

        # 2. Park all assigned robots safely
        park_results = []
        for r_id in target_lease.get("assigned_robots", ["R1"]):
            robot = self.robots.get(r_id)
            if not robot:
                robot = MockRobot(robot_id=r_id)
                self.robots[r_id] = robot

            park_report = self.parking_engine.execute_safe_parking(
                robot=robot,
                lease=target_lease,
                reason=reason
            )

            # Store park command and log decision
            park_cmd_data = {
                "cmd_id": f"cmd-park-{r_id}-{int(time.time()*1000)}",
                "lease_id": lid,
                "robot_id": r_id,
                "action": "go_to",
                "params": park_report["params"],
                "timestamp": time.time()
            }
            self.store.store_robot_command(park_cmd_data)
            self.store.update_robot_command_status(park_cmd_data["cmd_id"], "COMPLETED")

            self.store.log_decision(
                decision_id=f"dec-park-{lid}-v{version}-{r_id}",
                lease_id=lid,
                decided_by="RULE",
                input_context={"robot_id": r_id, "reason": reason, "cancelled_count": cancelled_count},
                decision_action="SAFE_PARK",
                decision_output=park_report,
                latency_ms=1.0
            )
            park_results.append(park_report)

        # 3. Update lease status in store
        self.store.update_lease_status(lid, version, "EXPIRED_PARKED")

        # 4. Escalate to Cloud
        chosen_spot = park_results[0]["target_spot"]["zone_name"]
        self._escalate_unexpected(
            target_lease,
            robot_id=target_lease.get("assigned_robots", ["R1"])[0],
            what=f"Lease expired; robot safely parked in non-transit alcove ({chosen_spot})",
            where=chosen_spot
        )

        return {
            "status": "EXPIRED_PARKED",
            "cancelled_commands": cancelled_count,
            "parking_results": park_results,
            "primary_spot": chosen_spot
        }

    def _handle_lease_expiry_parking(self, lease: Dict[str, Any]) -> Dict[str, Any]:
        """
        Internal handler invoked when process_lease detects expiry timestamp exceeded.
        Ensures backwards compatibility with existing test expectations.
        """
        robot_id = lease["assigned_robots"][0]
        robot = self.robots.get(robot_id)
        if not robot:
            robot = MockRobot(robot_id=robot_id)
            self.robots[robot_id] = robot

        # Cancel any active commands
        self.store.cancel_active_commands(lease["lease_id"])

        # Execute safe parking
        park_res = self.parking_engine.execute_safe_parking(
            robot=robot,
            lease=lease,
            reason="Lease expired mid-mission"
        )

        # Store command & log decision (FR-20, NFR-6)
        park_cmd_data = {
            "cmd_id": f"cmd-park-{int(time.time()*1000)}",
            "lease_id": lease["lease_id"],
            "robot_id": robot_id,
            "action": "go_to",
            "params": park_res["params"],
            "timestamp": time.time()
        }
        self.store.store_robot_command(park_cmd_data)
        self.store.update_robot_command_status(park_cmd_data["cmd_id"], "COMPLETED")

        self.store.log_decision(
            decision_id=f"dec-park-{lease['lease_id']}-v{lease['version']}",
            lease_id=lease["lease_id"],
            decided_by="RULE",
            input_context={"lease_id": lease["lease_id"], "expiry_ts": lease["expiry_ts"]},
            decision_action="SAFE_PARK",
            decision_output=park_res,
            latency_ms=1.0
        )

        self.store.update_lease_status(lease["lease_id"], lease["version"], "EXPIRED_PARKED")

        # Escalate to Cloud
        chosen_spot = park_res["target_spot"]["zone_name"]
        self._escalate_unexpected(
            lease,
            robot_id=robot_id,
            what=f"Lease expired; robot safely parked in non-transit alcove ({chosen_spot})",
            where=chosen_spot
        )

        return park_res

    def _escalate_unexpected(self, lease: Dict[str, Any], robot_id: str, what: str, where: str):
        """Emits UNEXPECTED_EVENT escalation adhering to wire contract."""
        task_id = self._extract_task_id(lease)
        version = lease.get("version", 1)
        return self.trigger_unexpected_event(
            task_id=task_id,
            what=what,
            where=where,
            robots_saw=[f"{robot_id} reported: {what} at {where}"],
            lease_version=version
        )

    def flush_pending_escalations(self) -> int:
        """Flushes any pending escalations in local outbox (FR-10)."""
        pending = self.store.get_pending_outbox()
        flushed = 0
        for item in pending:
            payload = item.get("payload")
            if not payload:
                continue
            if "task_id" in payload and payload["task_id"].startswith("lease-") and "-v" in payload["task_id"]:
                lid = payload["task_id"]
                payload["task_id"] = lid[len("lease-"):lid.rindex("-v")]
            url = f"{self.cloud_url}/escalations"
            try:
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    if resp.status in (200, 201, 204):
                        self.store.mark_outbox_uploaded(item["message_id"])
                        flushed += 1
            except Exception:
                pass
        return flushed

    def send_heartbeat_to_cloud(
        self,
        robots_digest: Optional[List[Dict[str, Any]]] = None,
        stock_digest: Optional[Dict[str, int]] = None
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Sends idle cluster heartbeat to POST /fleet/heartbeat (Item 2: liveness probe).
        Keeps cluster health in 'healthy' state on cloud (<30s).
        """
        url = f"{self.cloud_url}/fleet/heartbeat"
        if robots_digest is None:
            robots_digest = []
            for rid, r in self.robots.items():
                is_busy = bool(self.active_lease and rid in self.active_lease.get("assigned_robots", []))
                robots_digest.append({
                    "id": rid,
                    "state": "busy" if is_busy else "idle",
                    "zone": getattr(r, "current_zone", "home"),
                    "battery": getattr(r, "battery_pct", 100.0)
                })

        body = {
            "cluster_id": self.cluster_id,
            "robots": robots_digest
        }
        if stock_digest:
            body["stock"] = stock_digest

        req_data = json.dumps(body).encode("utf-8")
        try:
            req = urllib.request.Request(
                url,
                data=req_data,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                data = json.loads(resp.read().decode("utf-8") or "{}")
                if resp.status in (200, 201):
                    return True, data
        except urllib.error.HTTPError as he:
            err_text = he.read().decode("utf-8", errors="ignore")
            print(f"[{self.cluster_id}] Heartbeat HTTP error {he.code}: {err_text}")
        except Exception:
            # Network partition / offline -> keep going (Item 3)
            pass
        return False, {}

    def send_checkin_to_cloud(
        self,
        lease: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Sends active lease check-in to POST /checkins (Item 1: renewal & telemetry).
        Returns (success: bool, status_reason: str, reply_data: dict)
        status_reason: 'accepted', 'renewed', 'stale_version', 'offline'
        """
        target_lease = lease or self.active_lease
        if not target_lease:
            return False, "no_active_lease", {}

        url = f"{self.cloud_url}/checkins"
        lid = target_lease.get("lease_id", "")
        ver = target_lease.get("version", 1)

        # Server requires format lease-<task_id>-v<version>
        if not re.fullmatch(r"lease-(.+)-v\d+", lid):
            task_id = self._extract_task_id(target_lease)
            lid = f"lease-{task_id}-v{ver}"

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        message_id = f"msg-ci-{int(time.time()*1000)}-{uuid.uuid4().hex[:6]}"

        robots_digest = []
        for rid, r in self.robots.items():
            robots_digest.append({
                "id": rid,
                "state": "busy",
                "zone": getattr(r, "current_zone", "transit"),
                "battery": getattr(r, "battery_pct", 95.0)
            })

        body = {
            "cluster_id": self.cluster_id,
            "lease_id": lid,
            "lease_version": ver,
            "timestamp": now_iso,
            "message_id": message_id,
            "robots": robots_digest
        }

        req_data = json.dumps(body).encode("utf-8")
        try:
            req = urllib.request.Request(
                url,
                data=req_data,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                data = json.loads(resp.read().decode("utf-8") or "{}")
                if resp.status in (200, 201):
                    if "renewed_until" in data:
                        return True, "renewed", data
                    return True, "accepted", data
        except urllib.error.HTTPError as he:
            err_text = he.read().decode("utf-8", errors="ignore")
            try:
                err_json = json.loads(err_text)
            except Exception:
                err_json = {}
            if he.code == 409:
                print(f"[{self.cluster_id}] Check-in 409: lease version {ver} is outdated. Current cloud version: {err_json.get('current')}")
                return False, "stale_version", err_json
            print(f"[{self.cluster_id}] Check-in HTTP error {he.code}: {err_text}")
            return False, f"http_{he.code}", err_json
        except Exception:
            # Item 3: If a check-in gets no reply, keep going and retry later
            return False, "offline", {}
