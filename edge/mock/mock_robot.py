"""
Nimbus Mock Robot (Person 1 Emulation for Testing)
Emulates PiCar-X navigation stack and sub-task command primitives:
go_to, wait, stop, photo, beep, report.
Supports coordinate tracking, zone resolution, reflex emergency stopping,
transit corridor traversal, and dynamic obstacle / fault injection for tactical SLM autonomy (FR-1, FR-3, FR-4, FR-6).
"""

import time
from typing import Any, Dict, List, Optional

from edge.supervisor.geofence import DEFAULT_ZONE_REGISTRY


class MockRobot:
    def __init__(self, robot_id: str = "robot-R1", initial_pos: Optional[List[float]] = None):
        self.robot_id = robot_id
        self.pose = initial_pos or [0.0, 0.0, 0.0]  # [x, y, theta]
        self.current_zone = "central-supply"
        self.status = "IDLE"  # IDLE, RUNNING, BLOCKED, COMPLETED, STOPPED, FAILED
        self.battery_pct = 95.0
        self.has_fault = False
        self.fault_msg = None
        self.current_cmd_id = None
        self.history: List[Dict[str, Any]] = []

        # Obstacle simulation registry: {zone_or_corridor: {type, description, clears_after_waits, wait_count}}
        self.injected_obstacles: Dict[str, Dict[str, Any]] = {}

    def inject_obstacle(
        self,
        zone: str,
        obstacle_type: str = "transient",
        description: str = "Maintenance trolley blocking path",
        clears_after_waits: int = 1
    ):
        """Injects a simulated dynamic or persistent obstacle into a zone or corridor."""
        self.injected_obstacles[zone] = {
            "type": obstacle_type,
            "description": description,
            "clears_after_waits": clears_after_waits,
            "wait_count": 0
        }

    def clear_obstacles(self):
        """Clears all simulated obstacles."""
        self.injected_obstacles.clear()

    def set_fault(self, fault_msg: str = "Motor drive stall detected"):
        """Simulates a hardware or mechanical fault."""
        self.has_fault = True
        self.fault_msg = fault_msg

    def clear_fault(self):
        self.has_fault = False
        self.fault_msg = None

    def set_battery(self, pct: float):
        self.battery_pct = pct

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

        # Check for hardware fault or critically depleted battery
        if self.has_fault or self.battery_pct <= 10.0:
            self.status = "FAILED"
            msg = self.fault_msg or f"Battery critically depleted ({self.battery_pct}%)"
            record["end_time"] = time.time()
            record["status"] = self.status
            record["result_msg"] = msg
            self.history.append(record)
            return {
                "robot_id": self.robot_id,
                "cmd_id": cmd_id,
                "status": "FAILED",
                "pose": list(self.pose),
                "zone": self.current_zone,
                "message": msg,
                "battery_pct": self.battery_pct,
                "fault": True,
                "timestamp": time.time()
            }

        if action == "go_to":
            to_zone = params.get("to")
            target = params.get("target")

            # Check if this movement encounters an injected obstacle directly or via corridor traversal
            blocked_zone = None
            if to_zone and to_zone in self.injected_obstacles:
                blocked_zone = to_zone
            elif self.current_zone in self.injected_obstacles:
                blocked_zone = self.current_zone
            elif (
                (self.current_zone == "central-supply" and to_zone in ("2B-12", "2B-01", "ward-2B")) or
                (self.current_zone in ("2B-12", "2B-01", "ward-2B") and to_zone == "central-supply")
            ):
                # Standard East-West transit between Central Supply and Ward 2B routes via corridor-2B
                if "corridor-2B" in self.injected_obstacles:
                    blocked_zone = "corridor-2B"

            if blocked_zone:
                obs_info = self.injected_obstacles[blocked_zone]
                # If transient and waited enough, obstacle has cleared
                if obs_info["type"] == "transient" and obs_info["wait_count"] >= obs_info["clears_after_waits"]:
                    del self.injected_obstacles[blocked_zone]
                else:
                    self.status = "BLOCKED"
                    result_msg = f"Navigation blocked in {blocked_zone}: {obs_info['description']}"
                    record["end_time"] = time.time()
                    record["status"] = self.status
                    record["result_msg"] = result_msg
                    self.history.append(record)
                    return {
                        "robot_id": self.robot_id,
                        "cmd_id": cmd_id,
                        "status": "BLOCKED",
                        "pose": list(self.pose),
                        "zone": self.current_zone,
                        "blocked_zone": blocked_zone,
                        "obstacle_type": obs_info["type"],
                        "obstacle_description": obs_info["description"],
                        "message": result_msg,
                        "timestamp": time.time()
                    }

            # Coordinate updates
            if target and isinstance(target, (list, tuple)) and len(target) >= 2:
                self.pose[0] = float(target[0])
                self.pose[1] = float(target[1])
                if len(target) > 2:
                    self.pose[2] = float(target[2])
            elif to_zone and to_zone in DEFAULT_ZONE_REGISTRY:
                center = DEFAULT_ZONE_REGISTRY[to_zone]["center"]
                self.pose[0] = float(center[0])
                self.pose[1] = float(center[1])

            if to_zone:
                self.current_zone = to_zone

            target_name = params.get("target_name", to_zone or f"pos({self.pose[0]},{self.pose[1]})")
            self.status = "COMPLETED"
            result_msg = f"Arrived at {target_name} ({self.pose})"

        elif action == "wait":
            duration = params.get("duration_sec", params.get("seconds", 1.0))
            self.status = "COMPLETED"
            result_msg = f"Waited {duration}s"

            # Increment wait counts on all active obstacles (including transit obstacles)
            for obs in self.injected_obstacles.values():
                obs["wait_count"] += 1

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
            reason = params.get("reason", "Emergency / reflex stop applied")
            result_msg = f"Stop executed: {reason}"

        elif action == "report":
            self.status = "IDLE"
            result_msg = f"Pose: {self.pose}, Zone: {self.current_zone}, Battery: {self.battery_pct}%"

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
            "pose": list(self.pose),
            "zone": self.current_zone,
            "message": result_msg,
            "timestamp": time.time()
        }

    def get_status(self) -> Dict[str, Any]:
        """Returns current robot telemetry."""
        return {
            "robot_id": self.robot_id,
            "current_cmd_id": self.current_cmd_id,
            "status": self.status,
            "pose": list(self.pose),
            "current_zone": self.current_zone,
            "battery_pct": self.battery_pct,
            "has_fault": self.has_fault,
            "timestamp": time.time()
        }
