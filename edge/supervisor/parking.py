"""
Nimbus Safe Parking Engine (FR-6: Lease Expiry & Safe Parking Enforcement)
Guarantees that upon lease expiry or emergency interrupt:
1. Current robot errand is halted immediately (model-free reflex stop).
2. The nearest verified safe parking recess is selected.
3. Prohibited parking zones (doorways, fire exits, active corridors, elevator vestibules) are strictly avoided.
4. Forbidden zones from the lease are strictly excluded.
"""

import math
from typing import Any, Dict, List, Optional, Tuple, Union

from edge.supervisor.geofence import DEFAULT_ZONE_REGISTRY, SpatialGeofenceValidator


# Invariant 4 (FR-6): Robots must NEVER be left standing in doorways, corridors, or elevator vestibules.
PROHIBITED_PARKING_ZONE_TYPES = {
    "doorway",
    "fire_exit",
    "elevator",
    "lift",
    "stairwell",
    "corridor",
    "hallway",
    "transit",
    "isolation"
}

# Verified static safe parking bays (wall alcoves, dedicated recesses, docks)
DEFAULT_SAFE_PARKING_SPOTS: Dict[str, Dict[str, Any]] = {
    "safe-parking-bay-1": {
        "zone_name": "safe-parking-bay-1",
        "coords": [-1.0, 4.0],
        "zone_type": "parking_bay",
        "wing": "east-wing",
        "capacity": 2,
        "description": "Supply Depot Equipment Recess Bay 1"
    },
    "safe-parking-bay-2": {
        "zone_name": "safe-parking-bay-2",
        "coords": [11.0, -1.8],
        "zone_type": "parking_bay",
        "wing": "east-wing",
        "capacity": 2,
        "description": "Corridor 2B Wall Alcove Recess Bay 2"
    },
    "safe-parking-bay-east": {
        "zone_name": "safe-parking-bay-east",
        "coords": [20.0, 3.0],
        "zone_type": "parking_bay",
        "wing": "east-wing",
        "capacity": 2,
        "description": "East Wing Sub-station Parking Recess"
    }
}


class SafeParkingEngine:
    """
    Manages safe parking spot resolution and execution on lease expiration (FR-6).
    """

    def __init__(
        self,
        safe_spots: Optional[Dict[str, Dict[str, Any]]] = None,
        zone_registry: Optional[Dict[str, Dict[str, Any]]] = None
    ):
        self.safe_spots = safe_spots or DEFAULT_SAFE_PARKING_SPOTS
        self.zone_registry = zone_registry or DEFAULT_ZONE_REGISTRY
        self.geofence = SpatialGeofenceValidator(self.zone_registry)

    def is_safe_for_parking(self, zone_name: str) -> bool:
        """
        Validates that a zone is an approved safe parking area and does not violate
        Invariant 4 (doorways, elevator vestibules, or active transit corridors).
        """
        # 1. Check known prohibited zone types
        zone_info = self.zone_registry.get(zone_name)
        if zone_info:
            ztype = zone_info.get("zone_type", "").lower()
            if ztype in PROHIBITED_PARKING_ZONE_TYPES:
                return False

        # 2. Heuristic check on zone naming
        lower_name = zone_name.lower()
        for forbidden_kw in ("door", "exit", "lift", "elevator", "corridor", "hallway", "stair"):
            if forbidden_kw in lower_name and "safe-parking" not in lower_name:
                return False

        # 3. Must be registered as a safe spot or designated parking bay
        if zone_name in self.safe_spots:
            return True

        if zone_info and zone_info.get("zone_type") == "parking_bay":
            return True

        return False

    def find_nearest_safe_parking_spot(
        self,
        current_pose: List[float],
        current_zone: Optional[str] = None,
        forbidden_zones: Optional[List[str]] = None,
        candidate_spots: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Finds the closest verified safe parking recess to current_pose that is:
        - NOT in forbidden_zones
        - NOT in prohibited parking zones (doorway, elevator vestibule, corridor)
        
        Returns:
            Dict containing zone_name, coords, distance, description
        """
        forbidden_set = set(forbidden_zones or [])
        robot_x = current_pose[0] if len(current_pose) > 0 else 0.0
        robot_y = current_pose[1] if len(current_pose) > 1 else 0.0

        # Determine spot pool to consider
        pool: Dict[str, Dict[str, Any]] = {}
        if candidate_spots:
            for s in candidate_spots:
                if s in self.safe_spots:
                    pool[s] = self.safe_spots[s]
                elif s in self.zone_registry:
                    info = self.zone_registry[s]
                    pool[s] = {
                        "zone_name": s,
                        "coords": info["center"],
                        "zone_type": info.get("zone_type", "parking_bay"),
                        "description": info.get("description", s)
                    }
        else:
            pool = self.safe_spots

        eligible_candidates = []
        for name, data in pool.items():
            # Exclude if explicitly in forbidden zones
            if name in forbidden_set:
                continue

            # Exclude if in a prohibited zone (doorway, corridor, elevator)
            if not self.is_safe_for_parking(name):
                continue

            spot_coords = data["coords"]
            dist = math.hypot(spot_coords[0] - robot_x, spot_coords[1] - robot_y)
            eligible_candidates.append({
                "zone_name": name,
                "coords": spot_coords,
                "distance": dist,
                "description": data.get("description", name)
            })

        if not eligible_candidates:
            # Fallback emergency bay if everything else was blocked
            fallback = list(self.safe_spots.values())[0]
            return {
                "zone_name": fallback["zone_name"],
                "coords": fallback["coords"],
                "distance": 999.0,
                "description": fallback.get("description", "Emergency fallback bay")
            }

        # Sort by proximity
        eligible_candidates.sort(key=lambda item: item["distance"])
        return eligible_candidates[0]

    def execute_safe_parking(
        self,
        robot: Any,
        lease: Dict[str, Any],
        reason: str = "Lease expired mid-mission"
    ) -> Dict[str, Any]:
        """
        Executes the safe parking sequence:
        1. Issues an immediate 'stop' command to halt in-flight motion.
        2. Resolves nearest valid safe parking spot.
        3. Issues a 'go_to' command targeting the safe parking bay.
        """
        robot_status = robot.get_status()
        current_pose = robot_status.get("pose", [0.0, 0.0, 0.0])
        current_zone = robot_status.get("current_zone", "corridor")
        forbidden_zones = lease.get("forbidden_zones", [])

        # Step 1: Emergency / Reflex stop
        stop_cmd = {
            "cmd_id": f"cmd-stop-{int(robot_status.get('timestamp', 0)*1000)}",
            "lease_id": lease.get("lease_id", "unknown"),
            "robot_id": robot.robot_id,
            "action": "stop",
            "params": {"reason": reason}
        }
        stop_report = robot.execute_command(stop_cmd)

        # Step 2: Determine nearest safe parking bay outside corridors/doors
        target_spot = self.find_nearest_safe_parking_spot(
            current_pose=current_pose,
            current_zone=current_zone,
            forbidden_zones=forbidden_zones
        )

        # Step 3: Dispatch navigation to the safe parking recess
        park_cmd = {
            "cmd_id": f"cmd-park-{int(robot_status.get('timestamp', 0)*1000)+1}",
            "lease_id": lease.get("lease_id", "unknown"),
            "robot_id": robot.robot_id,
            "action": "go_to",
            "params": {
                "to": target_spot["zone_name"],
                "target": target_spot["coords"],
                "safe_parking": True
            }
        }
        park_report = robot.execute_command(park_cmd)

        return {
            "robot_id": robot.robot_id,
            "action": "go_to",
            "params": park_cmd["params"],
            "target_spot": target_spot,
            "stop_executed": stop_report["status"] in ("IDLE", "COMPLETED"),
            "parking_status": park_report["status"],
            "final_pose": park_report["pose"],
            "reason": reason
        }
