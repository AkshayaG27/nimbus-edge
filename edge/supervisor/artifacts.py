"""
Nimbus Edge Camera Artifact & Metrics Packager (FR-9, OUTPUT_READY)
Generates physical delivery-proof image artifacts on disk, computes
cryptographic SHA256 checksums, and compiles mission execution metrics.
"""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Tuple


class ArtifactPackager:
    def __init__(self, artifacts_dir: Optional[str] = None):
        if artifacts_dir:
            self.base_dir = Path(artifacts_dir)
        else:
            # Default to edge/artifacts/ relative to this file
            self.base_dir = Path(__file__).resolve().parent.parent / "artifacts"
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _generate_mock_jpeg_bytes(
        self,
        task_id: str,
        robot_id: str,
        pose: List[float],
        resolution: str = "1280x720"
    ) -> bytes:
        """
        Synthesizes valid binary JPEG data with JFIF application markers
        and embedded ASCII comment metadata for delivery proof.
        """
        # Standard JPEG headers (SOI + APP0 JFIF marker)
        soi = b"\xff\xd8"
        app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00"

        # COM (Comment) marker containing Nimbus mission metadata
        metadata_comment = (
            f"NIMBUS-PROOF;task={task_id};robot={robot_id};"
            f"pose={pose[0]:.2f},{pose[1]:.2f};res={resolution};"
            f"ts={datetime.now(timezone.utc).isoformat()}"
        ).encode("utf-8")
        com_length = len(metadata_comment) + 2
        com_marker = b"\xff\xfe" + com_length.to_bytes(2, byteorder="big") + metadata_comment

        # DQT, SOF0, DHT markers (minimal realistic JPEG framing)
        dqt = b"\xff\xdb\x00C\x00" + (b"\x10" * 64)
        sof0 = b"\xff\xc0\x00\x11\x08\x02\xd0\x05\x00\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
        sos = b"\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00?\x00"
        
        # Mock compressed image scan payload (800 bytes pattern)
        raw_scan = bytes([(i * 37) % 256 for i in range(800)])
        
        # EOI marker
        eoi = b"\xff\xd9"

        return soi + app0 + com_marker + dqt + sof0 + sos + raw_scan + eoi

    def generate_delivery_proof(
        self,
        task_id: str,
        robot_id: str = "R1",
        pose: Optional[List[float]] = None,
        resolution: str = "1280x720"
    ) -> Tuple[str, Path, str]:
        """
        Creates and writes delivery proof photo artifact to disk.
        
        Returns:
            (storage_uri, file_path, sha256_hash)
        """
        current_pose = pose or [0.0, 0.0, 0.0]
        task_dir = self.base_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        proof_path = task_dir / "delivery-proof.jpg"
        jpeg_bytes = self._generate_mock_jpeg_bytes(
            task_id=task_id,
            robot_id=robot_id,
            pose=current_pose,
            resolution=resolution
        )
        proof_path.write_bytes(jpeg_bytes)

        sha256_hash = hashlib.sha256(jpeg_bytes).hexdigest()

        # Save companion metadata sidecar
        sidecar_path = task_dir / "proof_metadata.json"
        meta = {
            "task_id": task_id,
            "robot_id": robot_id,
            "pose": current_pose,
            "resolution": resolution,
            "file_size_bytes": len(jpeg_bytes),
            "sha256": sha256_hash,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        sidecar_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        storage_uri = f"storage://results/{task_id}/delivery-proof.jpg"
        return storage_uri, proof_path, sha256_hash

    def generate_execution_metrics(
        self,
        task_id: str,
        lease_version: int,
        robot_id: str,
        start_time: float,
        end_time: float,
        executed_steps: List[Dict[str, Any]],
        battery_start: float = 95.0,
        battery_end: float = 93.5,
        tactical_events_count: int = 0
    ) -> Dict[str, Any]:
        """
        Computes distance, duration, subtasks count, and battery consumption metrics,
        and saves metrics.json sidecar.
        """
        task_dir = self.base_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        # Calculate estimated distance from consecutive reported poses
        total_distance = 0.0
        last_pose = None
        for step in executed_steps:
            pose = step.get("pose")
            if pose and len(pose) >= 2:
                if last_pose is not None:
                    dx = pose[0] - last_pose[0]
                    dy = pose[1] - last_pose[1]
                    dist = math.sqrt(dx * dx + dy * dy)
                    total_distance += dist
                last_pose = pose

        duration_sec = max(0.01, end_time - start_time)
        battery_consumed = max(0.0, battery_start - battery_end)

        metrics = {
            "task_id": task_id,
            "lease_version": lease_version,
            "robot_id": robot_id,
            "execution_duration_sec": round(duration_sec, 3),
            "distance_travelled_m": round(total_distance, 2),
            "subtasks_completed": len(executed_steps),
            "battery_consumed_pct": round(battery_consumed, 1),
            "tactical_events_count": tactical_events_count,
            "timestamp": time.time()
        }

        metrics_path = task_dir / "metrics.json"
        metrics_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        return metrics

    def package_output(
        self,
        task_id: str,
        lease_version: int,
        robot_id: str,
        pose: List[float],
        start_time: float,
        end_time: float,
        executed_steps: List[Dict[str, Any]],
        battery_start: float = 95.0,
        battery_end: float = 93.5,
        tactical_events_count: int = 0
    ) -> Dict[str, Any]:
        """
        Full bundling helper: writes photo artifact, saves metrics,
        and returns package summary.
        """
        storage_uri, file_path, sha256 = self.generate_delivery_proof(
            task_id=task_id,
            robot_id=robot_id,
            pose=pose
        )
        metrics = self.generate_execution_metrics(
            task_id=task_id,
            lease_version=lease_version,
            robot_id=robot_id,
            start_time=start_time,
            end_time=end_time,
            executed_steps=executed_steps,
            battery_start=battery_start,
            battery_end=battery_end,
            tactical_events_count=tactical_events_count
        )
        return {
            "result_ref": storage_uri,
            "file_path": str(file_path),
            "sha256": sha256,
            "metrics": metrics
        }
