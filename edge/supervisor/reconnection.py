"""
Nimbus Edge Reconnection Protocol & Summary Generator (FR-12, NFR-2)
Generates structured Reconnection Summary upon WAN recovery to reconcile
active lease authority, completed errands, and robot fleet positions.
"""

from datetime import datetime, timezone
import json
import logging
import time
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.request

from edge.store.local_store import EdgeLocalStore
from edge.mock.mock_robot import MockRobot

logger = logging.getLogger("NimbusReconnection")


class ReconnectionManager:
    def __init__(
        self,
        cluster_id: str,
        store: EdgeLocalStore,
        robots: Dict[str, MockRobot],
        cloud_url: str = "http://localhost:8000"
    ):
        self.cluster_id = cluster_id
        self.store = store
        self.robots = robots
        self.cloud_url = cloud_url.rstrip("/")

        self.is_connected = True
        self.disconnected_at: Optional[float] = None
        self.last_reconnect_ts: Optional[float] = None

    def record_disconnection(self, timestamp: Optional[float] = None):
        """Signals that WAN connection was severed / partitioned."""
        if self.is_connected:
            self.is_connected = False
            self.disconnected_at = timestamp or time.time()

    def build_reconnection_summary(
        self,
        reconnect_ts: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Builds the canonical Reconnection Summary according to SPEC-P2 Sec 2.4.
        """
        now = reconnect_ts or time.time()
        disconnected_duration = 0.0
        if self.disconnected_at:
            disconnected_duration = max(0.0, now - self.disconnected_at)

        # 1. Inspect completed tasks from local SQLite store
        completed_tasks: List[str] = []
        with self.store._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT lease_id, task_id FROM leases WHERE status = 'COMPLETED'")
            rows = cursor.fetchall()
            for r in rows:
                tid = r["task_id"] if r["task_id"] and r["task_id"] != "unknown" else r["lease_id"]
                if tid and tid not in completed_tasks:
                    completed_tasks.append(tid)

        # 2. Inspect active or recent leases
        active_leases_summary: List[Dict[str, Any]] = []
        with self.store._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT lease_id, version, status, lease_json
                FROM leases
                ORDER BY version DESC
            """)
            rows = cursor.fetchall()
            seen_lease_ids = set()
            for r in rows:
                lid = r["lease_id"]
                if lid in seen_lease_ids:
                    continue
                seen_lease_ids.add(lid)

                # Collect robot positions for assigned robots
                lease_data = json.loads(r["lease_json"]) if r["lease_json"] else {}
                assigned_robots = lease_data.get("assigned_robots", ["R1"])
                robot_positions = {}
                for rid in assigned_robots:
                    if rid in self.robots:
                        robot_positions[rid] = list(self.robots[rid].pose[:2])
                    else:
                        robot_positions[rid] = [0.0, 0.0]

                active_leases_summary.append({
                    "lease_id": lid,
                    "current_version": r["version"],
                    "status": r["status"],
                    "robot_positions": robot_positions
                })

        # 3. Count pending outbox uploads
        pending_outbox = self.store.get_pending_outbox()
        pending_count = len(pending_outbox)

        summary = {
            "cluster_id": self.cluster_id,
            "reconnect_timestamp": round(now, 2),
            "disconnected_duration_seconds": round(disconnected_duration, 2),
            "completed_tasks": completed_tasks,
            "active_leases": active_leases_summary,
            "pending_uploads_count": pending_count
        }
        return summary

    def record_reconnection(
        self,
        reconnect_ts: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Signals that WAN connection was restored.
        Generates and returns the Reconnection Summary.
        """
        now = reconnect_ts or time.time()
        summary = self.build_reconnection_summary(reconnect_ts=now)
        self.is_connected = True
        self.last_reconnect_ts = now
        self.disconnected_at = None
        return summary

    def send_reconnection_summary(
        self,
        summary: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Transmits Reconnection Summary to Cloud fleet heartbeat endpoint.
        """
        payload = summary or self.build_reconnection_summary()
        url = f"{self.cloud_url}/fleet/heartbeat"
        req_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=req_bytes,
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                return resp.status in (200, 201, 204)
        except Exception as e:
            logger.warning(f"Failed to post reconnection summary to {url}: {e}")
            return False
