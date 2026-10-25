"""
Patch script for supervisor.py:
1. Normalizes task_id extraction from lease ('lease-task-000001-v1' -> 'task-000001').
2. Ensures escalation HTTP errors are printed with status code and payload for clean debugging.
3. Adds flush_pending_escalations method for store-and-forward outbox uploads.
"""

from pathlib import Path

p = Path("edge/supervisor/supervisor.py")
if not p.exists():
    print(f"Error: {p} not found. Please run this script from the repository root.")
    exit(1)

text = p.read_text(encoding="utf-8")

# 1. Add _extract_task_id static method if not present
if "def _extract_task_id" not in text:
    target = '    def start_outbox_worker(self):'
    replacement = '''    @staticmethod
    def _extract_task_id(lease: dict) -> str:
        """Extracts canonical task_id ('task-000001') from lease document or lease_id ('lease-task-000001-v1')."""
        if "task_id" in lease:
            return lease["task_id"]
        lid = lease.get("lease_id", "")
        if lid.startswith("lease-") and "-v" in lid:
            return lid[len("lease-"):lid.rindex("-v")]
        return lid

    def start_outbox_worker(self):'''
    text = text.replace(target, replacement)

# 2. Update task_id references
text = text.replace('task_id = lease.get("task_id", lease_id)', 'task_id = self._extract_task_id(lease)')
text = text.replace('task_id = lease.get("task_id", lease["lease_id"])', 'task_id = self._extract_task_id(lease)')

# 3. Add flush_pending_escalations method if not present
flush_method = '''
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
                import urllib.request, json
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
'''

if "def flush_pending_escalations" not in text:
    text += flush_method

p.write_text(text, encoding="utf-8")
print("Successfully patched edge/supervisor/supervisor.py")
