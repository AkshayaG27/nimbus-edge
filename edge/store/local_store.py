"""
Nimbus Edge Local Store & Store-and-Forward Engine
Uses SQLite for persistent, ACID-compliant local storage of leases, commands,
decision logs, and outbox escalations (FR-6, FR-8, FR-10, NFR-6).
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class EdgeLocalStore:
    def __init__(self, db_path: str = "edge_local_store.db"):
        self.db_path = Path(db_path)
        self.init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        """Initializes tables if they do not exist."""
        with self._get_conn() as conn:
            cursor = conn.cursor()

            # 1. Leases table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS leases (
                    lease_id TEXT,
                    version INTEGER,
                    cluster_id TEXT,
                    task_id TEXT,
                    status TEXT,
                    valid_until REAL,
                    lease_json TEXT,
                    received_at REAL,
                    PRIMARY KEY (lease_id, version)
                )
            """)

            # 2. Version tracker (to reject stale / duplicate versions - FR-8)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS lease_versions (
                    lease_id TEXT PRIMARY KEY,
                    highest_version INTEGER,
                    updated_at REAL
                )
            """)

            # 3. Message deduplication table (FR-8)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS seen_messages (
                    message_id TEXT PRIMARY KEY,
                    received_at REAL
                )
            """)

            # 4. Robot sub-task commands table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS robot_commands (
                    cmd_id TEXT PRIMARY KEY,
                    lease_id TEXT,
                    robot_id TEXT,
                    action TEXT,
                    params_json TEXT,
                    status TEXT, -- PENDING, DISPATCHED, COMPLETED, FAILED, CANCELLED
                    created_at REAL,
                    dispatched_at REAL,
                    completed_at REAL
                )
            """)

            # 5. Decision log table (FR-20, NFR-6)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS decision_log (
                    decision_id TEXT PRIMARY KEY,
                    lease_id TEXT,
                    decided_by TEXT, -- 'SLM', 'RULE', 'CLOUD'
                    input_context TEXT,
                    decision_action TEXT,
                    decision_output TEXT,
                    latency_ms REAL,
                    timestamp REAL
                )
            """)

            # 6. Outbox for Store-and-Forward (FR-10)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS outbox (
                    message_id TEXT PRIMARY KEY,
                    lease_id TEXT,
                    message_type TEXT, -- CLARIFY, UNEXPECTED_EVENT, OUTPUT_READY, CHECKIN
                    payload_json TEXT,
                    status TEXT, -- PENDING, UPLOADED, FAILED
                    created_at REAL,
                    uploaded_at REAL
                )
            """)
            conn.commit()

    def record_incoming_message(self, message_id: str) -> bool:
        """
        Deduplication filter (FR-8).
        Returns True if this is a new message.
        Returns False if message_id has already been processed.
        """
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM seen_messages WHERE message_id = ?",
                (message_id,)
            )
            if cursor.fetchone():
                return False  # Duplicate!
            cursor.execute(
                "INSERT INTO seen_messages (message_id, received_at) VALUES (?, ?)",
                (message_id, time.time())
            )
            conn.commit()
            return True

    def store_lease(self, lease_data: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Stores an incoming lease if its version is strictly higher than seen before (FR-8).
        """
        lease_id = lease_data["lease_id"]
        version = int(lease_data["version"])
        now = time.time()

        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT highest_version FROM lease_versions WHERE lease_id = ?",
                (lease_id,)
            )
            row = cursor.fetchone()

            if row:
                highest_seen = row["highest_version"]
                if version <= highest_seen:
                    return False, f"Stale/duplicate version {version} <= {highest_seen}. Dropped."
                cursor.execute(
                    "UPDATE lease_versions SET highest_version = ?, updated_at = ? WHERE lease_id = ?",
                    (version, now, lease_id)
                )
            else:
                cursor.execute(
                    "INSERT INTO lease_versions (lease_id, highest_version, updated_at) VALUES (?, ?, ?)",
                    (lease_id, version, now)
                )

            expires_at = lease_data.get("expiry_ts") or lease_data.get("valid_until")
            if isinstance(expires_at, str):
                # ISO 8601 string -> store as string or float
                pass

            cursor.execute("""
                INSERT INTO leases (lease_id, version, cluster_id, task_id, status, valid_until, lease_json, received_at)
                VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?, ?)
            """, (
                lease_id,
                version,
                lease_data.get("cluster_id", "unknown"),
                lease_data.get("task_id", "unknown"),
                str(expires_at),
                json.dumps(lease_data),
                now
            ))
            conn.commit()
            return True, f"Lease {lease_id} v{version} stored successfully."

    def get_lease(self, lease_id: str, version: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Retrieves a lease by lease_id, regardless of status."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            if version is not None:
                cursor.execute("SELECT lease_json FROM leases WHERE lease_id = ? AND version = ?", (lease_id, version))
            else:
                cursor.execute("SELECT lease_json FROM leases WHERE lease_id = ? ORDER BY version DESC LIMIT 1", (lease_id,))
            row = cursor.fetchone()
            if row:
                return json.loads(row["lease_json"])
            return None

    def get_active_lease(self, lease_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves the latest active lease by lease_id."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT lease_json FROM leases
                WHERE lease_id = ? AND status = 'ACTIVE'
                ORDER BY version DESC LIMIT 1
            """, (lease_id,))
            row = cursor.fetchone()
            if row:
                return json.loads(row["lease_json"])
            return None

    def update_lease_status(self, lease_id: str, version: int, status: str):
        """Updates lease status (e.g. COMPLETED, EXPIRED, CANCELLED)."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE leases SET status = ? WHERE lease_id = ? AND version = ?
            """, (status, lease_id, version))
            conn.commit()

    def store_robot_command(self, cmd_data: Dict[str, Any]):
        """Stores a generated sub-task command."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO robot_commands
                (cmd_id, lease_id, robot_id, action, params_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'PENDING', ?)
            """, (
                cmd_data["cmd_id"],
                cmd_data["lease_id"],
                cmd_data["robot_id"],
                cmd_data["action"],
                json.dumps(cmd_data.get("params", {})),
                time.time()
            ))
            conn.commit()

    def update_robot_command_status(self, cmd_id: str, status: str):
        """Updates command status (DISPATCHED, COMPLETED, FAILED)."""
        now = time.time()
        with self._get_conn() as conn:
            cursor = conn.cursor()
            if status == "DISPATCHED":
                cursor.execute("UPDATE robot_commands SET status = ?, dispatched_at = ? WHERE cmd_id = ?",
                               (status, now, cmd_id))
            elif status in ("COMPLETED", "FAILED"):
                cursor.execute("UPDATE robot_commands SET status = ?, completed_at = ? WHERE cmd_id = ?",
                               (status, now, cmd_id))
            else:
                cursor.execute("UPDATE robot_commands SET status = ? WHERE cmd_id = ?",
                               (status, cmd_id))
            conn.commit()

    def log_decision(
        self,
        decision_id: str,
        lease_id: str,
        decided_by: str,
        input_context: Dict[str, Any],
        decision_action: str,
        decision_output: Dict[str, Any],
        latency_ms: float
    ):
        """Records decision in decision_log for audit and replay (FR-20, NFR-6)."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO decision_log
                (decision_id, lease_id, decided_by, input_context, decision_action, decision_output, latency_ms, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                decision_id,
                lease_id,
                decided_by,
                json.dumps(input_context),
                decision_action,
                json.dumps(decision_output),
                latency_ms,
                time.time()
            ))
            conn.commit()

    def enqueue_outbox(self, message_id: str, lease_id: str, message_type: str, payload: Dict[str, Any]):
        """Queues an escalation/output for upload (FR-10 store-and-forward)."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO outbox
                (message_id, lease_id, message_type, payload_json, status, created_at)
                VALUES (?, ?, ?, ?, 'PENDING', ?)
            """, (
                message_id,
                lease_id,
                message_type,
                json.dumps(payload),
                time.time()
            ))
            conn.commit()

    def get_pending_outbox(self) -> List[Dict[str, Any]]:
        """Gets all items waiting to be uploaded to cloud."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM outbox WHERE status = 'PENDING' ORDER BY created_at ASC")
            rows = cursor.fetchall()
            return [
                {
                    "message_id": r["message_id"],
                    "lease_id": r["lease_id"],
                    "message_type": r["message_type"],
                    "payload": json.loads(r["payload_json"]),
                    "created_at": r["created_at"]
                }
                for r in rows
            ]

    def mark_outbox_uploaded(self, message_id: str):
        """Marks an outbox message as successfully delivered to the cloud."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE outbox SET status = 'UPLOADED', uploaded_at = ? WHERE message_id = ?
            """, (time.time(), message_id))
            conn.commit()
