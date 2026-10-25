"""
Nimbus Edge Supervisor Runner (Person 2)
Runs a continuous Edge Supervisor daemon for a specific cluster (e.g. east-wing or west-wing).
Polls cloud outbox for leases, executes sub-tasks locally, and sends OUTPUT_READY escalations.

Usage:
    python edge/run_supervisor.py --cluster east-wing
    python edge/run_supervisor.py --cluster west-wing
"""

import argparse
from pathlib import Path
import sys
import time

# Ensure repository root is on sys.path
_current = Path(__file__).resolve().parent
while _current.parent != _current:
    if (_current / "edge" / "supervisor").is_dir():
        if str(_current) not in sys.path:
            sys.path.insert(0, str(_current))
        break
    _current = _current.parent

from edge.supervisor.supervisor import EdgeSupervisor


def main():
    parser = argparse.ArgumentParser(description="Run an edge supervisor cluster daemon.")
    parser.add_argument("--cluster", default="east-wing", help="Cluster ID (e.g., east-wing, west-wing)")
    parser.add_argument("--cloud-url", default="http://127.0.0.1:8000", help="Cloud API endpoint URL")
    parser.add_argument("--db", default=None, help="Path to local SQLite DB (defaults to edge_<cluster>.db)")
    parser.add_argument("--interval", type=float, default=2.0, help="Outbox polling interval in seconds")
    parser.add_argument("--once", action="store_true", help="Poll once and exit")
    args = parser.parse_args()

    db_path = args.db or f"edge_{args.cluster.replace('-', '_')}.db"

    print("=" * 65)
    print(f"  NIMBUS EDGE SUPERVISOR DAEMON: [{args.cluster.upper()}]")
    print("=" * 65)
    print(f"  Cluster ID      : {args.cluster}")
    print(f"  Cloud Endpoint  : {args.cloud_url}")
    print(f"  Local SQLite DB : {db_path}")
    print(f"  Polling Interval: {args.interval}s")
    print("  Status          : Active & Listening for Leases...\n")

    supervisor = EdgeSupervisor(
        cluster_id=args.cluster,
        cloud_url=args.cloud_url,
        db_path=db_path,
        slm_mode="auto"
    )

    processed_leases = set()

    try:
        while True:
            # 1. Flush any pending escalations (store-and-forward FR-10)
            flushed = supervisor.flush_pending_escalations()
            if flushed > 0:
                print(f"[{args.cluster}] Uploaded {flushed} pending escalation(s) to cloud.")

            # 2. Fetch new leases from cloud outbox
            leases = supervisor.fetch_leases_from_cloud()
            for lease in leases:
                lid = lease.get("lease_id")
                ver = lease.get("version", 1)
                key = f"{lid}-v{ver}"
                if key in processed_leases:
                    continue

                target_cluster = lease.get("cluster_id")
                if target_cluster != args.cluster:
                    # Ignore leases targeted to other clusters
                    continue

                print(f"[{args.cluster}] Ingesting lease: {lid} (v{ver})")
                result = supervisor.process_lease(lease)
                status = result.get("status")
                print(f"[{args.cluster}] Mission Result: {status}")
                if status == "COMPLETED":
                    print(f"[{args.cluster}] Proof Ref   : {result.get('result_ref')}")
                elif status == "REJECTED":
                    print(f"[{args.cluster}] Reason      : {result.get('reason')}")
                processed_leases.add(key)

            if args.once:
                break
            time.sleep(args.interval)

    except KeyboardInterrupt:
        print(f"\n[{args.cluster}] Supervisor shutting down cleanly.")


if __name__ == "__main__":
    main()
