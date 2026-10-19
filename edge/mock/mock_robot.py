"""
Nimbus Mock Robot (Person 1 Emulation for Testing)
Emulates PiCar-X navigation stack and sub-task command primitives:
go_to, wait, stop, photo, beep, report.
"""

import time
from typing import Any, Dict, Optional


class MockRobot:
    def __init__(self, robot_id: str = "robot-R1", initial_pos: Optional[list] = None):
        self.robot_id = robot_id
        self.pose = initial_pos or [0.0, 0.0, 0.0]  # [x, y, theta]
        self.current_zone = "hallway-central"
        self.status = "IDLE"  # IDLE, RUNNING, BLOCKED, COMPLETED
        self.battery_pct = 95.0
        self.current_cmd_id = None
        self.history = []

    def execute_command(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes a received RobotCommand primitive.
        Returns execution status report.
        """
        cmd_id = cmd["cmd_id"]
        action = cmd["action"]
        params = cmd.get("params", {})
        self.current_cmd_id = cmd_id
        self.status = "RUNNING"

        record = {
            "cmd_id": cmd_id,
            "action": action,
            "params": params,
            "start_time": time.time(),
        }

        if action == "go_to":
            target = params.get("target", self.pose[:2])
            # Simulate navigation movement
            self.pose[0] = target[0]
            self.pose[1] = target[1]
            if len(target) > 2:
                self.pose[2] = target[2]
            target_name = params.get("target_name", "target")
            self.status = "COMPLETED"
            result_msg = f"Arrived at {target_name} ({target})"

        elif action == "wait":
            duration = params.get("duration_sec", 1.0)
            self.status = "COMPLETED"
            result_msg = f"Waited {duration}s"

        elif action == "photo":
            resolution = params.get("photo_resolution", "1280x720")
            self.status = "COMPLETED"
            result_msg = f"Photo captured at {self.pose} with res {resolution}"

        elif action == "beep":
            freq = params.get("beep_frequency_hz", 440)
            self.status = "COMPLETED"
            result_msg = f"Beeped at {freq}Hz"

        elif action == "stop":
            self.status = "IDLE"
            result_msg = "Emergency / reflex stop applied"

        elif action == "report":
            self.status = "IDLE"
            result_msg = f"Pose: {self.pose}, Battery: {self.battery_pct}%"

        else:
            self.status = "FAILED"
            result_msg = f"Unknown action: {action}"

        record["end_time"] = time.time()
        record["status"] = self.status
        record["result_msg"] = result_msg
        self.history.append(record)

        return {
            "robot_id": self.robot_id,
            "cmd_id": cmd_id,
            "status": self.status,
            "pose": self.pose,
            "message": result_msg,
            "timestamp": time.time()
        }

    def get_status(self) -> Dict[str, Any]:
        """Returns current robot telemetry."""
        return {
            "robot_id": self.robot_id,
            "current_cmd_id": self.current_cmd_id,
            "status": self.status,
            "pose": self.pose,
            "current_zone": self.current_zone,
            "battery_pct": self.battery_pct,
            "timestamp": time.time()
        }
