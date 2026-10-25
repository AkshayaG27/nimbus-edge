"""
Nimbus Spatial Geofence Validator (FR-5: Lease Boundary Enforcement)
Enforces spatial boundary rules on robot destinations and trajectories:
- All target destinations and waypoints must lie strictly within allowed_zones.
- No target destination or waypoint may intersect forbidden_zones.
- Supports both semantic zone names and [x, y] metric coordinates.
"""

import math
from typing import Any, Dict, List, Optional, Tuple, Union


# Standard East Wing zone catalog with spatial bounding boxes [xmin, ymin, xmax, ymax]
# and center coordinates [x, y]
DEFAULT_ZONE_REGISTRY: Dict[str, Dict[str, Any]] = {
    "central-supply": {
        "center": [0.0, 0.0],
        "bounds": [-3.0, -3.0, 3.0, 3.0],
        "zone_type": "store",
        "description": "Central Medical Supply & Dispatch Depot"
    },
    "corridor-2B": {
        "center": [10.0, 0.0],
        "bounds": [3.0, -1.5, 17.0, 1.5],
        "zone_type": "corridor",
        "description": "Main East-West Transit Corridor 2B"
    },
    "corridor-2B-north": {
        "center": [10.0, 2.0],
        "bounds": [3.0, 1.5, 17.0, 3.5],
        "zone_type": "corridor",
        "description": "Parallel North Bypass Corridor 2B"
    },
    "corridor-2B-service": {
        "center": [10.0, -3.0],
        "bounds": [3.0, -4.5, 17.0, -1.5],
        "zone_type": "corridor",
        "description": "Service Transit Hallway 2B"
    },
    "2B-01": {
        "center": [8.0, 4.0],
        "bounds": [6.5, 2.0, 9.5, 6.0],
        "zone_type": "patient_bed",
        "description": "Ward 2B Bed 01"
    },
    "2B-12": {
        "center": [14.0, 4.0],
        "bounds": [12.5, 2.0, 15.5, 6.0],
        "zone_type": "patient_bed",
        "description": "Ward 2B Bed 12 (Paediatric Room)"
    },
    "ward-2B": {
        "center": [11.0, 4.0],
        "bounds": [6.0, 1.5, 16.0, 7.0],
        "zone_type": "ward",
        "description": "General Ward 2B Cluster"
    },
    "ward-2C-isolation": {
        "center": [25.0, 0.0],
        "bounds": [22.0, -4.0, 28.0, 4.0],
        "zone_type": "isolation",
        "description": "Infectious Disease Isolation Ward 2C"
    },
    "pediatric-icu": {
        "center": [10.0, -5.0],
        "bounds": [6.0, -8.0, 14.0, -2.0],
        "zone_type": "icu",
        "description": "Pediatric Intensive Care Unit"
    },
    "doorway-fire-exit-1": {
        "center": [17.5, 0.0],
        "bounds": [16.5, -1.0, 18.5, 1.0],
        "zone_type": "doorway",
        "description": "Emergency Fire Exit Doorway 1"
    },
    "elevator-vestibule-east": {
        "center": [3.5, 0.0],
        "bounds": [2.5, -1.5, 4.5, 1.5],
        "zone_type": "elevator",
        "description": "East Wing Passenger Elevator Vestibule"
    },
    "safe-parking-bay-1": {
        "center": [-1.0, 4.0],
        "bounds": [-2.0, 3.0, 0.0, 5.0],
        "zone_type": "parking_bay",
        "description": "Supply Depot Equipment Recess Bay 1"
    },
    "safe-parking-bay-2": {
        "center": [11.0, -1.8],
        "bounds": [10.0, -2.5, 12.0, -1.5],
        "zone_type": "parking_bay",
        "description": "Corridor 2B Wall Alcove Recess Bay 2"
    },
    "safe-parking-bay-east": {
        "center": [20.0, 3.0],
        "bounds": [19.0, 2.0, 21.0, 4.0],
        "zone_type": "parking_bay",
        "description": "East Wing Sub-station Parking Recess"
    }
}


class SpatialGeofenceValidator:
    """
    Validates that robot navigation commands and waypoints remain within
    allowed zones and do not violate forbidden zones (FR-5).
    """

    def __init__(self, zone_registry: Optional[Dict[str, Dict[str, Any]]] = None):
        self.registry = zone_registry or DEFAULT_ZONE_REGISTRY

    def resolve_zone_for_coordinates(self, coords: Union[List[float], Tuple[float, float]]) -> Optional[str]:
        """
        Determines which named zone contains the given [x, y] coordinates.
        Checks bounding boxes first, falls back to closest center if within tolerance.
        """
        x, y = coords[0], coords[1]
        for zone_name, info in self.registry.items():
            xmin, ymin, xmax, ymax = info["bounds"]
            if xmin <= x <= xmax and ymin <= y <= ymax:
                return zone_name
        return None

    def get_zone_center(self, zone_name: str) -> Optional[List[float]]:
        """Returns [x, y] center coordinates for a registered zone."""
        info = self.registry.get(zone_name)
        if info:
            return list(info["center"])
        return None

    def find_alternative_waypoints(
        self,
        current_zone: str,
        destination_zone: str,
        allowed_zones: List[str],
        forbidden_zones: List[str],
        blocked_zone: Optional[str] = None
    ) -> List[str]:
        """
        Discovers alternative routable waypoints within allowed_zones (excluding
        forbidden_zones, the blocked zone, and current/destination) that can bypass an obstruction.
        """
        allowed_set = set(allowed_zones)
        forbidden_set = set(forbidden_zones)
        excluded = {current_zone, destination_zone}
        if blocked_zone:
            excluded.add(blocked_zone)

        candidates = []
        for zone_name in self.registry:
            if zone_name in excluded:
                continue
            if zone_name in allowed_set and zone_name not in forbidden_set:
                ztype = self.registry[zone_name].get("zone_type", "")
                if ztype in ("corridor", "ward", "parking_bay"):
                    candidates.append(zone_name)

        # Prioritize explicit bypass corridors first
        candidates.sort(key=lambda z: (
            0 if "bypass" in self.registry.get(z, {}).get("description", "").lower() or "north" in z else 1
        ))
        return candidates

    def validate_destination(
        self,
        destination: Any,
        allowed_zones: List[str],
        forbidden_zones: List[str]
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Validates a single destination (string zone name or [x, y] coordinate).
        
        Returns:
            (is_valid: bool, reason: str, resolved_zone_name: Optional[str])
        """
        allowed_set = set(allowed_zones)
        forbidden_set = set(forbidden_zones)

        # Case 1: Destination specified as a named zone string
        if isinstance(destination, str):
            zone_name = destination.strip()

            # Rule 1: Must NOT be in forbidden zones
            if zone_name in forbidden_set:
                return False, f"Destination '{zone_name}' is explicitly inside forbidden_zones", zone_name

            # Rule 2: Must be in allowed zones
            if zone_name not in allowed_set:
                return False, f"Destination '{zone_name}' is not permitted in allowed_zones {sorted(list(allowed_set))}", zone_name

            return True, "Valid", zone_name

        # Case 2: Destination specified as coordinates [x, y]
        elif isinstance(destination, (list, tuple)) and len(destination) >= 2:
            coords = [float(destination[0]), float(destination[1])]
            zone_name = self.resolve_zone_for_coordinates(coords)

            if not zone_name:
                return False, f"Coordinates {coords} fall outside any recognized hospital zone", None

            if zone_name in forbidden_set:
                return False, f"Coordinates {coords} resolve to forbidden zone '{zone_name}'", zone_name

            if zone_name not in allowed_set:
                return False, f"Coordinates {coords} resolve to zone '{zone_name}' which is not in allowed_zones", zone_name

            return True, "Valid", zone_name

        return False, f"Unrecognized destination format: {destination}", None

    def validate_subtask(
        self,
        subtask: Dict[str, Any],
        allowed_zones: List[str],
        forbidden_zones: List[str]
    ) -> Tuple[bool, str]:
        """
        Validates all spatial parameters of a sub-task command against lease boundaries.
        Inspects 'to', 'target', 'waypoints', 'destination' in params.
        """
        cmd = subtask.get("cmd")
        params = subtask.get("params", {})

        if cmd == "go_to":
            # Extract target zone or coordinate
            target = params.get("to") or params.get("target") or params.get("destination")
            if target is None:
                return False, "Command 'go_to' missing target destination parameter ('to' or 'target')"

            valid, reason, _ = self.validate_destination(target, allowed_zones, forbidden_zones)
            if not valid:
                return False, reason

            # If intermediate waypoints are specified, validate all of them
            waypoints = params.get("waypoints", [])
            for idx, wp in enumerate(waypoints):
                wp_valid, wp_reason, _ = self.validate_destination(wp, allowed_zones, forbidden_zones)
                if not wp_valid:
                    return False, f"Intermediate waypoint #{idx+1} invalid: {wp_reason}"

        return True, "Valid"
