"""
Nimbus Edge Store-and-Forward Outbox Worker (FR-10, FR-12, NFR-2)
Background worker thread reading pending escalations/outputs from local SQLite disk queue
and delivering them to the Cloud with exponential backoff retry.
Guarantees zero message loss across network partitions.
"""

import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional
import urllib.error
import urllib.request

from edge.store.local_store import EdgeLocalStore

logger = logging.getLogger("NimbusOutboxWorker")


class OutboxUploadWorker:
    def __init__(
        self,
        store: EdgeLocalStore,
        cloud_url: str = "http://localhost:8000",
        poll_interval_sec: float = 0.5,
        base_backoff_sec: float = 0.5,
        max_backoff_sec: float = 16.0,
        backoff_factor: float = 2.0,
        auto_start: bool = False
    ):
        self.store = store
        self.cloud_url = cloud_url.rstrip("/")
        self.poll_interval = poll_interval_sec
        self.base_backoff = base_backoff_sec
        self.max_backoff = max_backoff_sec
        self.backoff_factor = backoff_factor

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._wake_event = threading.Event()

        # Network simulation state
        self._network_available = True
        self.consecutive_failures = 0
        self.current_backoff_delay = 0.0

        # Stats / telemetry
        self.total_uploaded = 0
        self.total_failures = 0

        if auto_start:
            self.start()

    def set_network_available(self, available: bool):
        """Programmatic network toggle for fault injection / outage tests."""
        self._network_available = available
        if available:
            # Wake worker immediately on network restore
            self.consecutive_failures = 0
            self.current_backoff_delay = 0.0
            self._wake_event.set()

    def pause_network(self):
        """Simulate WAN partition / disconnection."""
        self.set_network_available(False)

    def resume_network(self):
        """Simulate WAN recovery."""
        self.set_network_available(True)

    def is_network_available(self) -> bool:
        return self._network_available

    def start(self):
        """Starts background worker daemon thread."""
        if self._running:
            return
        self._running = True
        self._wake_event.clear()
        self._thread = threading.Thread(target=self._worker_loop, daemon=True, name="OutboxWorkerThread")
        self._thread.start()

    def stop(self, timeout: float = 3.0):
        """Stops background worker thread."""
        if not self._running:
            return
        self._running = False
        self._wake_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def trigger_now(self):
        """Wakes up worker loop immediately."""
        self._wake_event.set()

    def process_pending_outbox_once(self) -> int:
        """
        Processes pending outbox items in FIFO order.
        Returns count of items successfully uploaded in this pass.
        Halts immediately on first network failure to preserve order and back off.
        """
        if not self._network_available:
            return 0

        pending_items = self.store.get_pending_outbox()
        if not pending_items:
            return 0

        uploaded_in_pass = 0

        for item in pending_items:
            if not self._network_available:
                break

            msg_id = item["message_id"]
            payload = item["payload"]
            msg_type = item.get("message_type")

            # Route escalation types to /escalations, check-in to /checkins
            endpoint = "/checkins" if msg_type == "CHECKIN" else "/escalations"
            target_url = f"{self.cloud_url}{endpoint}"

            success = self._send_single_payload(target_url, payload)
            if success:
                self.store.mark_outbox_uploaded(msg_id)
                self.total_uploaded += 1
                uploaded_in_pass += 1
                self.consecutive_failures = 0
                self.current_backoff_delay = 0.0
            else:
                self.consecutive_failures += 1
                self.total_failures += 1
                # Calculate exponential backoff
                self.current_backoff_delay = min(
                    self.max_backoff,
                    self.base_backoff * (self.backoff_factor ** (self.consecutive_failures - 1))
                )
                # Break to preserve FIFO delivery order (FR-10)
                break

        return uploaded_in_pass

    def _send_single_payload(self, url: str, payload: Dict[str, Any]) -> bool:
        """Sends single HTTP POST request to Cloud."""
        if not self._network_available:
            return False

        req_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=req_bytes,
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                # 200, 201, 204 are success
                # Note: Server returns 200 for duplicate seen messages
                if resp.status in (200, 201, 204):
                    return True
                return False
        except urllib.error.HTTPError as he:
            # If server explicitly answered 409 duplicate or already accepted
            try:
                err_body = json.loads(he.read().decode("utf-8"))
                if err_body.get("duplicate") is True:
                    return True
            except Exception:
                pass
            return False
        except (urllib.error.URLError, ConnectionError, OSError, TimeoutError):
            return False

    def _worker_loop(self):
        """Main loop of background worker thread."""
        while self._running:
            try:
                # If currently in backoff delay, sleep for backoff duration
                if self.current_backoff_delay > 0:
                    time.sleep(self.current_backoff_delay)
                    self.current_backoff_delay = 0.0

                self.process_pending_outbox_once()
            except Exception as e:
                logger.error(f"Error in outbox worker loop: {e}")

            # Sleep until next poll or until explicitly notified
            self._wake_event.wait(timeout=self.poll_interval)
            self._wake_event.clear()

    def flush(self, timeout: float = 5.0) -> bool:
        """
        Synchronously flushes all pending outbox entries.
        Returns True if queue is empty, False if items remain after timeout or failure.
        """
        start = time.time()
        while time.time() - start < timeout:
            pending = self.store.get_pending_outbox()
            if not pending:
                return True
            if not self._network_available:
                return False
            uploaded = self.process_pending_outbox_once()
            if uploaded == 0 and self.consecutive_failures > 0:
                return False
            time.sleep(0.05)
        return len(self.store.get_pending_outbox()) == 0
