# Nimbus — Task 2.1 Test Verification Report
**Topic:** Lease Boundary & Safe Parking Enforcement (FR-5, FR-6, NFR-6)  
**Role:** Person 2 — Edge Tier & Local Autonomy  
**Repository:** `Nimbus-Edge`  
**Execution Date:** October 22, 2026  
**Environment:** Python 3.10.11, pytest-9.1.1, Windows (x86_64)  
**Result:** **14 / 14 PASSED (100% Green)**  

---

## 1. Executive Summary

| Category | Requirements | Tests Run | Passed | Failed | Status |
| :--- | :--- | :---: | :---: | :---: | :---: |
| **Spatial Geofencing** | FR-5 (Zone Confinement, Invariant 1) | 4 | 4 | 0 | **PASS** |
| **Safe Parking Engine** | FR-6 (Doorway/Lift Prohibition, Invariant 4) | 3 | 3 | 0 | **PASS** |
| **Expiry Watchdog & Interrupt** | FR-6 (Expiry Guard, Invariant 3) | 2 | 2 | 0 | **PASS** |
| **Milestone 1 Baseline Regression** | FR-3, FR-8, FR-9, FR-10, NFR-6 | 5 | 5 | 0 | **PASS** |
| **Total** | | **14** | **14** | **0** | **100% PASS** |

---

## 2. Detailed Test Results Breakdown

### A. Spatial Geofencing & Boundary Enforcement (FR-5)

| Test Case | Target Tested | Verification Objective | Result |
| :--- | :--- | :--- | :---: |
| `test_geofence_validator_allowed_and_forbidden_zones` | `SpatialGeofenceValidator` | Verifies that destinations outside `allowed_zones` or inside `forbidden_zones` are flagged immediately. | **PASSED** |
| `test_geofence_validator_metric_coordinates` | Coordinate Geofencing | Confirms `[x, y]` coordinates map to polygon bounds, rejecting coordinates in forbidden zones or outside hospital building. | **PASSED** |
| `test_supervisor_blocks_destination_outside_allowed_zones` | `EdgeSupervisor.process_lease` | Asserts that unleased destination (`pediatric-icu`) is rejected with `FAILED_BOUNDARY`, logged as `RULE` decision, and queued as `UNEXPECTED_EVENT`. | **PASSED** |
| `test_supervisor_blocks_forbidden_intermediate_waypoints` | Multi-waypoint navigation | Injects trajectory with intermediate step crossing `ward-2C-isolation`; asserts early halt before dispatch. | **PASSED** |

### B. Safe Parking Engine & Hazardous Transit Zone Avoidance (FR-6)

| Test Case | Target Tested | Verification Objective | Result |
| :--- | :--- | :--- | :---: |
| `test_safe_parking_prohibits_doorways_elevators_and_corridors` | `SafeParkingEngine.is_safe_for_parking` | Asserts that doorways (`doorway-fire-exit-1`), elevators (`elevator-vestibule-east`), and corridors (`corridor-2B`) are **never** permitted for parking (Invariant 4). | **PASSED** |
| `test_nearest_safe_parking_spot_selection` | Proximity Resolver | Validates Euclidean distance sorting to pick the closest verified hospital bay recess (`safe-parking-bay-1`, `safe-parking-bay-2`, `safe-parking-bay-east`). | **PASSED** |
| `test_nearest_safe_parking_spot_excludes_forbidden_zones` | Conflict Filter | When closest safe bay falls inside lease `forbidden_zones`, engine automatically selects the next closest valid safe bay. | **PASSED** |

### C. Expiry Watchdog & In-Flight Interrupt (FR-6, NFR-6)

| Test Case | Target Tested | Verification Objective | Result |
| :--- | :--- | :--- | :---: |
| `test_in_flight_expiry_interrupt` | `EdgeSupervisor.trigger_expiry_interrupt` | Cancels active in-flight commands (`CANCELLED`), halts robot via `stop`, dispatches `go_to` safe spot, writes `SAFE_PARK` decision to SQLite, updates status to `EXPIRED_PARKED`, and enqueues `UNEXPECTED_EVENT`. | **PASSED** |
| `test_multi_robot_safe_parking_on_expiry` | Multi-Robot Fleet (`R1`, `R2`) | Confirms all assigned robots in the lease are independently and safely parked in respective nearby non-transit recesses upon expiry. | **PASSED** |

### D. Baseline Edge Tier Regression Suite (Milestone 1)

| Test Case | Target Tested | Verification Objective | Result |
| :--- | :--- | :--- | :---: |
| `test_lease_schema_validation` | `validator.py` | JSON Schema Draft 2020-12 compliance and cross-field check (`deadline_ts <= expiry_ts`). | **PASSED** |
| `test_task_t1_execution_end_to_end` | `EdgeSupervisor` | End-to-end execution of routine hospital delivery (Task T1) producing `OUTPUT_READY`. | **PASSED** |
| `test_monotonic_versioning_and_deduplication` | `local_store.py` (FR-8) | Monotonic version check; drops stale/duplicate lease versions. | **PASSED** |
| `test_forbidden_zone_boundary_enforcement` | Boundary Check | Legacy forbidden zone rejection test. | **PASSED** |
| `test_expiry_watchdog_safe_parking` | Watchdog | Legacy expiry test validating transition to `safe-parking-bay-1`. | **PASSED** |

---

## 3. Invariant Verification & Compliance Matrix

| Invariant / PRD Requirement | Specification Rule | Verification Proof |
| :--- | :--- | :--- |
| **Invariant 1 (Zone Confinement - FR-5)** | All target coordinates $[x, y]$ and zone names must belong to `allowed_zones` and never intersect `forbidden_zones`. | Verified in `test_supervisor_blocks_destination_outside_allowed_zones` and `test_supervisor_blocks_forbidden_intermediate_waypoints`. |
| **Invariant 3 (Expiry Guard - FR-6)** | The instant `now() >= expiry_ts` occurs, active commands halt and the robot moves to safe parking. | Verified in `test_in_flight_expiry_interrupt`: active command marked `CANCELLED`, emergency stop issued, safe park executed. |
| **Invariant 4 (Doorway/Lift Prohibition - FR-6)** | Robots must **never** be left standing inside doorways, stairwells, or elevator vestibules. | Verified in `test_safe_parking_prohibits_doorways_elevators_and_corridors`: prohibited zones rejected with 100% accuracy. |
| **Invariant 5 (Store-and-Forward - FR-10, NFR-6)** | All execution events, decisions, and escalations must be written to local SQLite disk before transmission. | Verified across all tests: tables `leases`, `robot_commands`, `decision_log`, and `outbox` populated and verified. |

---

## 4. Raw Pytest Execution Log

```text
============================= test session starts =============================
platform win32 -- Python 3.10.11, pytest-9.1.1, pluggy-1.6.0
rootdir: C:\Users\aksha\Introduction to CLoud Computing\Nimbus-Edge
plugins: anyio-4.15.1
collected 14 items

edge/tests/test_boundary_and_parking.py::test_geofence_validator_allowed_and_forbidden_zones PASSED [  7%]
edge/tests/test_boundary_and_parking.py::test_geofence_validator_metric_coordinates PASSED [ 14%]
edge/tests/test_boundary_and_parking.py::test_supervisor_blocks_destination_outside_allowed_zones PASSED [ 21%]
edge/tests/test_boundary_and_parking.py::test_supervisor_blocks_forbidden_intermediate_waypoints PASSED [ 28%]
edge/tests/test_boundary_and_parking.py::test_safe_parking_prohibits_doorways_elevators_and_corridors PASSED [ 35%]
edge/tests/test_boundary_and_parking.py::test_nearest_safe_parking_spot_selection PASSED [ 42%]
edge/tests/test_boundary_and_parking.py::test_nearest_safe_parking_spot_excludes_forbidden_zones PASSED [ 50%]
edge/tests/test_boundary_and_parking.py::test_in_flight_expiry_interrupt PASSED [ 57%]
edge/tests/test_boundary_and_parking.py::test_multi_robot_safe_parking_on_expiry PASSED [ 64%]
edge/tests/test_edge_tier.py::test_lease_schema_validation PASSED        [ 71%]
edge/tests/test_edge_tier.py::test_task_t1_execution_end_to_end PASSED   [ 78%]
edge/tests/test_edge_tier.py::test_monotonic_versioning_and_deduplication PASSED [ 85%]
edge/tests/test_edge_tier.py::test_forbidden_zone_boundary_enforcement PASSED [ 92%]
edge/tests/test_edge_tier.py::test_expiry_watchdog_safe_parking PASSED   [100%]

====================== 14 passed, 10 warnings in 20.56s =======================
```
