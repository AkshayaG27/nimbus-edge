# Nimbus — Edge Tier & Local Autonomy Specification (`SPEC.md`)
**Document Version:** 1.0  
**Owner:** Person 2 (Edge Tier & Local Autonomy)  
**System:** Nimbus — LLM-Orchestrated Robot Fleets with Leased Authority across Cloud and Edge  
**Target Course:** DS252: Introduction to Cloud Computing, IISc (Aug–Nov 2026)  
**Target Hardware:** Lab Workstation (NVIDIA RTX 3060 12GB / Jetson AGX Orin 32GB / Local Dev CPU)  

---

## 1. Role & Architectural Boundaries of Person 2

The Nimbus system is structured in three hierarchical tiers:
1. **Cloud Tier (Person 3 & 4):** Centralized global view, heavy LLM on Amazon Bedrock, global route planning, multi-wing scheduling, DynamoDB state, S3 assets, and operator/requester web interfaces.
2. **Edge Tier (Person 2 - You):** On-premises cluster supervisor (one per hospital wing), local Small Language Model (SLM, 1–3B params), local state store, lease enforcement, fault handling, and store-and-forward persistence during WAN disconnections.
3. **Robot Tier (Person 1):** Physical/simulated mobile ground robots (PiCar-X in Gazebo), ROS 2 / classical navigation stack (A*, local costmaps, DWA), and autonomous model-free safety stops (ultrasonic/LiDAR reflex).

```
 +-------------------------------------------------------------------------+
 |                              CLOUD TIER                                 |
 |  Requester UI | Operator Console | Bedrock Orchestrator | Lease Mgr    |
 +-------------------------------------------------------------------------+
                                     │  ▲
             Lease (v_k)             │  │  Escalations (CLARIFY,
             Zones, Expiry, Deadlines│  │  UNEXPECTED_EVENT, OUTPUT_READY)
             Speed Limits            ▼  │  Check-in Heartbeats
 +─────────────────────────────────────────────────────────────────────────+
 │                     EDGE TIER (PERSON 2 SCOPE)                          │
 │                                                                         │
 │  ┌───────────────────────────────────────────────────────────────────┐  │
 │  │                         Cloud Bridge / Comm                       │  │
 │  │      (MQTT/WebSocket / IoT Core / Local Mock Client)              │  │
 │  └───────────────────▲───────────────────────────────┬───────────────┘  │
 │                      │                               │                  │
 │                      │ Store-and-Forward             ▼ Incoming Lease   │
 │  ┌───────────────────┴───────────────────────────────┴───────────────┐  │
 │  │                       Local Lease & State Store                   │  │
 │  │          (SQLite / Append-only Disk Log, Replay Buffer)           │  │
 │  └───────────────────▲───────────────────────────────┬───────────────┘  │
 │                      │                               │                  │
 │                      │ State & Alerts                ▼ Validated Lease  │
 │  ┌───────────────────┴───────────────────────────────┴───────────────┐  │
 │  │                        Edge Supervisor Loop                       │  │
 │  │    - Lease boundary enforcement (Zones, Expiration, Version)      │  │
 │  │    - Safe Parking Engine (Doorway/Lift avoidance on expiry)       │  │
 │  │    - Monotonic versioning & deduplication filter (FR-8)           │  │
 │  │    - Edge Node Failover & Takeover Arbiter (FR-7)                 │  │
 │  └─────────────▲───────────────────────────────────────┬─────────────┘  │
 │                │                                       │                │
 │                │ Eval / Prompt                         ▼ Sub-tasks      │
 │  ┌─────────────┴─────────────┐           ┌─────────────┴─────────────┐  │
 │  │   Edge SLM Inference      │           │      Robot Dispatcher     │  │
 │  │   (llama.cpp 4-bit 1-3B)  │           │   (Sub-task Dispatch &    │  │
 │  │   + Rule-based Fallback   │           │    Execution Monitor)     │  │
 │  └───────────────────────────┘           └─────────────┬─────────────┘  │
 +────────────────────────────────────────────────────────┼────────────────+
                                                          │  ▲
                                  Robot Command Primitives│  │  Robot Status
                                  (go_to, wait, photo,...)│  │  (Position, Battery,
                                                          ▼  │   Obstacle Alerts)
 +───────────────────────────────────────────────────────────┴─────────────+
 |                              ROBOT TIER                                 |
 |  Simulated PiCar-X (Gazebo) | ROS 2 Nav2 Classical Planner | Reflex Stop |
 +-------------------------------------------------------------------------+
```

### Person 2 Core Responsibilities:
1. **Edge Supervisor Loop:** Continuously supervise the assigned wing cluster, intake signed leases from Cloud, validate lease authenticity and version freshness, break high-level tasks into robot primitive actions, and track execution.
2. **Lease Boundary Enforcement (FR-5, FR-6):** Enforce spatial bounds (allowed and forbidden zones), speed limits, and lease duration. If a lease expires, execute a graceful safe parking procedure (never stop in doors, corridors, or lifts).
3. **Local Autonomy & SLM Decisions (FR-3, FR-4, E1, E2):** Deploy and pin a 1–3B parameter quantized SLM (Qwen2.5-1.5B or Llama-3.2-3B via llama.cpp) to handle local tactical deviations (e.g. detours, dynamic waits, robot re-assignment). Implement a deterministic rule-based fallback if SLM generation is malformed or times out.
4. **Fault Tolerance & Store-and-Forward (FR-8, FR-10, FR-12, NFR-2):** Keep an append-only local persistence log (SQLite / disk buffer) holding leases, telemetry, and output artifacts. During WAN outages (up to 10 min), continue autonomous execution inside valid leases. Reconnect and flush summaries without dropping data.
5. **Deduplication & Multi-Node Failover (FR-7, FR-8):** Enforce strict message sequence IDs and lease versioning. Prevent duplicate action execution. If one edge workstation dies, allow a standby peer node to resume within ~30s.

---

## 2. Interface Contracts, Wire Schemas & HTTP REST Transport

The wire contracts for the whole Nimbus team are anchored in `contracts/` (JSON Schema Draft 2020-12) from the team repository (`vaibhav3000/nimbus`).
For **Milestone 1**, Cloud-Edge transport is served via **HTTP REST** on `server.py` (`http://localhost:8000`) before transitioning to AWS IoT Core / MQTT in later milestones.

### 2.1 The Lease Wire Schema (`contracts/lease.schema.json`)
The Cloud Lease defines bounded authority granted to an edge cluster:
```json
{
  "lease_id": "lease-task-000002-v1",
  "version": 1,
  "priority": "urgent",
  "deadline_ts": "2026-10-01T20:11:22Z",
  "expiry_ts": "2026-10-01T20:14:22Z",
  "allowed_zones": ["2B-12", "corridor-2B"],
  "forbidden_zones": ["ward-2C-isolation"],
  "speed_limit": 0.8,
  "assigned_robots": ["R1"],
  "task_sequence": [
    {"cmd": "go_to", "robot": "R1", "params": {"to": "central-supply"}},
    {"cmd": "wait", "robot": "R1", "params": {"seconds": 10}},
    {"cmd": "go_to", "robot": "R1", "params": {"to": "2B-12"}},
    {"cmd": "photo", "robot": "R1", "params": {}},
    {"cmd": "beep", "robot": "R1", "params": {}}
  ],
  "cluster_id": "east-wing",
  "when_to_escalate": ["door_closed", "deadline_at_risk", "robot_failed"]
}
```
**Invariants:**
- `deadline_ts <= expiry_ts` (ISO 8601 UTC).
- Monotonic versioning: only the highest version is ever honoured (FR-8).
- Confinement: all sub-task destinations must fall strictly within `allowed_zones` and never touch `forbidden_zones` (FR-5).

---

### 2.2 Robot Sub-task Grammar (`contracts/robot_subtask.schema.json`)
Fixed primitives executed by the robot navigation layer (Person 1):
```json
{
  "cmd": "go_to",
  "robot": "R1",
  "params": { "to": "2B-12", "max_speed": 0.8 }
}
```
Supported commands: `go_to`, `wait`, `stop`, `photo`, `beep`, `report`.

---

### 2.3 Escalation Wire Schema (`contracts/escalation.schema.json`)
The Edge contacts the Cloud via `POST /escalations`:
```json
{
  "type": "OUTPUT_READY",
  "message_id": "msg-edge-0012",
  "lease_version": 1,
  "task_id": "task-000002",
  "payload": {
    "result_ref": "delivery-confirmed-photo-bed12"
  }
}
```
- **`CLARIFY`**: payload requires `{"question": "...", "possible_readings": [...]}`.
- **`UNEXPECTED_EVENT`**: payload requires `{"what_happened": "...", "where": "...", "robots_saw": [...]}`.
- **`OUTPUT_READY`**: payload requires `{"result_ref": "..."}`. On receipt, `server.py` transitions task to `"status": "completed"`.

---

### 2.4 Milestone 1 HTTP Transport Endpoints (Cloud Stand-in `server.py`)
- **`GET /outbox`**: Edge polls or fetches new leases published by Cloud.
- **`POST /escalations`**: Edge pushes structured escalations (`CLARIFY`, `UNEXPECTED_EVENT`, `OUTPUT_READY`).
- **`POST /checkins`**: Periodic edge health check-in heartbeat.
- **`GET /tasks/{id}/timeline`**: Full decision-log replay for validation.


### 2.3 Robot Primitive Command Grammar (`robot_cmd.schema.json`)
Contract between Person 2 (Edge Supervisor) and Person 1 (Robot Navigation Stack):

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "RobotCommand",
  "type": "object",
  "required": ["cmd_id", "lease_id", "robot_id", "timestamp", "action", "params"],
  "properties": {
    "cmd_id": { "type": "string" },
    "lease_id": { "type": "string" },
    "robot_id": { "type": "string" },
    "timestamp": { "type": "number" },
    "action": {
      "type": "string",
      "enum": ["go_to", "wait", "stop", "photo", "beep", "report"]
    },
    "params": {
      "type": "object",
      "properties": {
        "target": {
          "type": "array",
          "items": { "type": "number" },
          "minItems": 2,
          "maxItems": 3,
          "description": "[x, y] or [x, y, theta]"
        },
        "target_name": { "type": "string" },
        "max_speed": { "type": "number", "minimum": 0.0, "maximum": 2.0 },
        "duration_sec": { "type": "number" },
        "beep_frequency_hz": { "type": "integer" },
        "photo_resolution": { "type": "string", "enum": ["640x480", "1280x720"] }
      }
    }
  }
}
```

#### Robot Status Feedback (`robot_status.schema.json`):
```json
{
  "robot_id": "robot-R1",
  "current_cmd_id": "cmd-0012",
  "status": "RUNNING", // "IDLE" | "RUNNING" | "BLOCKED" | "SAFETY_STOP" | "COMPLETED" | "FAILED"
  "pose": { "x": 10.5, "y": 4.2, "theta": 1.57 },
  "current_zone": "corridor-2B-east",
  "battery_pct": 88.0,
  "obstacle_distance_m": 1.8,
  "timestamp": 1792056012.33
}
```

---

### 2.4 Cloud-Edge Heartbeat & Reconnection Protocol
To satisfy **FR-8, FR-12, NFR-2**:
- **Periodic Check-in (Every 5 seconds):** Edge sends short JSON payload containing `{ cluster_id, highest_lease_version, active_leases: [{ lease_id, version, robots: [...] }], queue_depth, timestamp }`.
- **Reconnection Catchup (FR-12):** Upon recovering from WAN partition, Edge sends a **Reconnection Summary**:
  ```json
  {
    "cluster_id": "cluster-east-wing",
    "reconnect_timestamp": 1792056600.0,
    "disconnected_duration_seconds": 320.5,
    "completed_tasks": ["task-urgent-pump-01"],
    "active_leases": [
      {
        "lease_id": "lease-a1b2c3d4",
        "current_version": 2,
        "status": "EXPIRED_PARKED",
        "robot_positions": { "robot-R1": [10.0, 2.0] }
      }
    ],
    "pending_uploads_count": 1
  }
  ```

---

## 3. Edge Supervisor Architecture & State Machine

The Edge Supervisor is an event-driven daemon running on the wing cluster workstation.

### 3.1 Finite State Machine (FSM)
```
             ┌─────────────────────────┐
             │       STATE: INIT       │
             └────────────┬────────────┘
                          │ Boot & DB init
                          ▼
             ┌─────────────────────────┐
       ┌────►│   STATE: IDLE_WAITING   │◄──────────────┐
       │     └────────────┬────────────┘               │
       │                  │ Lease received & validated │
       │                  ▼                            │
       │     ┌─────────────────────────┐               │
       │     │   STATE: LEASE_ACTIVE   │               │
       │     └────────────┬────────────┘               │
       │                  │ Decompose & dispatch       │
       │                  ▼                            │
       │     ┌─────────────────────────┐               │
       │     │   STATE: EXEC_SUBTASK   │               │
       │     └──────┬────────────┬─────┘               │
       │            │            │                     │
       │  Subtask OK│            │ Obstacle/Event      │
       │            │            ▼                     │
       │            │     ┌──────────────┐             │
       │            │     │ LOCAL_RESOLVE│             │
       │            │     └──────┬───────┘             │
       │            │            │                     │
       │            │      Solved│  Unsolvable         │
       │            │       by   │  in lease           │
       │            │       SLM  │                     │
       │            │            ▼                     │
       │            │     ┌──────────────┐             │
       │            │     │  ESCALATING  │             │
       │            │     │ (CLARIFY/UE) │             │
       │            │     └──────┬───────┘             │
       │            │            │                     │
       │            │            ▼                     │
       │            │     ┌──────────────┐             │
       │            │     │ WAIT_CLOUD_  │             │
       │            │     │  RE-PLAN     │             │
       │            │     └──────────────┘             │
       │            │                                  │
       │ All steps  │ Lease expiry timer               │
       │ done       │ fires before finish              │
       │            ▼                                  │
       │     ┌─────────────────────────┐               │
       │     │  LEASE_EXPIRED_PARKING  │               │
       │     │ (Navigate to safe spot) │               │
       │     └────────────┬────────────┘               │
       │                  │ Safe parked                │
       │                  ▼                            │
       │     ┌─────────────────────────┐               │
       │     │    STATE: SAFE_PARKED   │               │
       │     └─────────────────────────┘               │
       │                                               │
       └───────────────────────────────────────────────┘
```

### 3.2 State Transition Invariants
1. **Invariant 1 (Zone Confinement - FR-5):** Every target coordinate $[x, y]$ passed to `robot_cmd` must belong to the lease `allowed_zones` and must NEVER intersect `forbidden_zones`.
2. **Invariant 2 (Version Monotonicity - FR-8):** If an incoming lease payload has `version <= current_version` for the same `lease_id`, it is dropped immediately and logged.
3. **Invariant 3 (Expiry Guard - FR-6):** An internal watchdog timer checks `now() > valid_until`. The second expiry occurs, the robot stops current forward movement and transitions to `LEASE_EXPIRED_PARKING`, issuing a command to head to the nearest designated `safe_parking_spots`.
4. **Invariant 4 (Doorway & Lift Prohibition - FR-6):** Robots must NEVER be left standing inside doorways, stairwells, or elevator vestibules. Safe parking targets are verified static hospital bay recesses.
5. **Invariant 5 (Store-and-Forward - FR-10):** All execution events, decisions, and completed output summaries are written to local SQLite disk BEFORE sending over network. If WAN send fails, they remain flagged `pending_upload = 1`.

---

## 4. Edge SLM Inference Engine Specification

### 4.1 Target Models & Execution Environment (E1 & NFR-1)
- **Model Choices:**
  - Candidate A: **Qwen/Qwen2.5-1.5B-Instruct** (GGUF 4-bit quant `Q4_K_M`, ~1.1 GB disk footprint).
  - Candidate B: **meta-llama/Llama-3.2-3B-Instruct** (GGUF 4-bit quant `Q4_K_M`, ~2.0 GB disk footprint).
- **Inference Server:** `llama.cpp` server or Python bindings (`llama-cpp-python`) with GPU offloading (`n_gpu_layers: -1` on RTX 3060 / Orin; CPU fallback for dev).
- **Constraints:**
  - Max generation tokens: **150 tokens** (enforced by `max_tokens=150`).
  - Response time target: **p95 <= 10.0 seconds** on target hardware (NFR-1).
  - JSON output mode: Guided generation (grammar-based or strict JSON system prompt).

### 4.2 Edge SLM Decision Prompt Template
```
<|im_start|>system
You are the Nimbus Edge Cluster Tactical Supervisor for the hospital East Wing.
Your job is to make bounded tactical decisions for mobile delivery robots.
You can ONLY output valid JSON adhering to the specified schema.
Allowed actions:
1. "DISPATCH_STEP" (proceed with next sub-task)
2. "LOCAL_DETOUR" (detour via adjacent clear waypoint within allowed zones)
3. "LOCAL_WAIT" (wait up to 30 seconds for dynamic obstacle to pass)
4. "SWAP_ROBOT" (reassign sub-task to idle robot in same cluster)
5. "ESCALATE_CLARIFY" (ask cloud for user clarification)
6. "ESCALATE_UNEXPECTED" (report unsolvable event / route closure / lease breach)

RULES:
- Never exceed allowed zones: {allowed_zones}
- Never enter forbidden zones: {forbidden_zones}
- If lease expires in < 30 seconds, choose "SAFE_PARK"
- Keep explanation under 20 words.
<|im_end|>
<|im_start|>user
Current Lease: {lease_id} (Version {version})
Time Remaining: {time_remaining_sec}s
Assigned Robot: {robot_id} at {current_pos} (Zone: {current_zone})
Current Event: {event_description}
Allowed Next Waypoints: {allowed_waypoints}

Respond in JSON format:
{
  "action": "<action_name>",
  "target_waypoint": "<waypoint_id_or_null>",
  "reason": "<short_explanation>"
}
<|im_end|>
<|im_start|>assistant
```

### 4.3 Deterministic Rule-Based Fallback
If the SLM fails to generate within 8 seconds, outputs unparsable JSON, or violates a boundary constraint:
- Deterministic fallback logic immediately overrides:
  1. If obstacle distance $< 0.5m \to$ `LOCAL_WAIT` for 10s.
  2. If wait count $> 2 \to$ Check alternative waypoints inside `allowed_zones`.
  3. If no alternative waypoint $\to$ Issue `ESCALATE_UNEXPECTED`.
  4. If lease time $< 30s \to$ Issue `SAFE_PARK`.

---

## 5. Local Storage & Store-and-Forward Engine (`FR-10`)

The local persistence engine is powered by **SQLite** (`edge_local_store.db`), ensuring ACID guarantees, instant atomic transactions, and zero external database dependencies.

### Tables:
1. `leases`: Stores current and historical leases (`lease_id`, `version`, `cluster_id`, `lease_json`, `status`, `received_at`, `expires_at`).
2. `robot_commands`: Stores generated sub-task commands (`cmd_id`, `lease_id`, `robot_id`, `action`, `params_json`, `status`, `created_at`, `completed_at`).
3. `decision_log`: Stores all decisions for NFR-6 / FR-20 replay (`decision_id`, `lease_id`, `decided_by` [SLM|RULE|CLOUD], `inputs_json`, `output_json`, `latency_ms`, `timestamp`).
4. `outbox_escalations`: Queue for escalations and outputs (`msg_id`, `type`, `payload_json`, `status` [PENDING|UPLOADED], `created_at`, `uploaded_at`).

---

## 6. Acceptance Criteria & Validation Mapping

| Requirement | Acceptance Criterion | Person 2 Edge Tier Test Validation |
|-------------|----------------------|-----------------------------------|
| **FR-3** | Edge supervision: Splits lease into sub-tasks, assigns robots, monitors ($\ge 95\%$ allowed actions). | `test_edge_supervision_decomposition`: Verify 50 mock leases decompose into valid primitives with $\ge 95\%$ compliance. |
| **FR-4** | Handle locally or ask: Solve local events; otherwise send `CLARIFY` / `UNEXPECTED_EVENT` ($\ge 80\%$ on 40 labelled scenarios). | `test_edge_local_vs_escalate`: Run the 40 benchmark scenarios through SLM + fallback classifier, asserting $\ge 80\%$ accuracy. |
| **FR-5** | Stay within lease: No actions outside allowed zones, after expiry, or from outdated version. | `test_lease_boundary_enforcement`: Injected out-of-zone waypoints, expired timers, and stale lease versions are strictly rejected. |
| **FR-6** | Keep working through outages: Continue current lease and hold messages; after expiry, park in safe spots. | `test_outage_continuity_and_safe_parking`: Simulate 5-min network drop; confirm execution continues, and on simulated expiry, robot parks in designated spot. |
| **FR-7** | Edge node takeover: Standby node takes over within 30s; no duplicate steps. | `test_failover_takeover`: Primary process SIGKILL; standby reads SQLite and picks up exactly from last completed sub-task. |
| **FR-8** | No duplicate actions: Repeated, re-ordered, and outdated messages dropped. | `test_message_deduplication`: Inject duplicate command IDs and lower lease versions; verify zero duplicate motor commands. |
| **FR-9** | Three structured escalation types (`CLARIFY`, `UNEXPECTED_EVENT`, `OUTPUT_READY`). | `test_escalation_schema_compliance`: Validate outbox payloads against JSON Schema. |
| **FR-10** | Results never lost: Kept on disk until safely uploaded (survives 10-min outage). | `test_store_and_forward_persistence`: Cut WAN for 10 min, generate 5 outputs, reconnect WAN, verify all 5 arrive without corruption. |
| **FR-12** | Reconnection: Reconnect within 1 min; no robot under two leases. | `test_reconnection_summary`: Validate summary payload generation and lease reconciliation. |
| **NFR-1** | Responsiveness: Edge decision p95 $\le 10$ s on pinned hardware. | Experiment E1 benchmarking script runs 50 trials and asserts p95 latency. |
| **NFR-6** | Traceability: Every decision logged and byte-reproducible. | Replay log verification tool diffs run outputs against recorded logs. |
