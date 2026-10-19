# Nimbus — Project Plan & Person 2 Tracking (`PLAN.md`)
**Role:** Person 2 — Edge Tier & Local Autonomy Lead  
**Team Members:** Person 1 (Robot & Safety), Person 2 (Edge Autonomy - You), Person 3 (Cloud & Leases), Person 4 (Infra & Comms)  
**Timeline:** October 1 – November 28, 2026  
**Current Date:** October 14, 2026 (Phase 1 / Pre-Milestone 1)  

---

## 1. Overall Team Roadmap & Milestone Schedule

| Milestone / Phase | Dates | Person 2 Primary Deliverables | Cross-Team Integration & Deliverables | Milestone Gate (Proof of Completion) |
|---|---|---|---|---|
| **Phase 1: Foundations & Milestone 1** | **Oct 1 – Oct 18** *(Current)* | • Edge schemas locked down<br>• Edge SLM Benchmark & Pinned Model (E1, NFR-1)<br>• Edge Supervisor loop foundation<br>• Local store (SQLite) initialized<br>• Mock Robot & Mock Cloud harnesses | • P1: Gazebo hospital + 2 robots + basic nav<br>• P3: Bedrock prompt parsing + DynamoDB lease schema<br>• P4: Terraform skeleton + IoT Core setup | **Milestone 1:** Task T1 (Routine delivery) executes end-to-end: Cloud Request $\to$ Bedrock creates Lease $\to$ Edge decomposes $\to$ Robot navigates $\to$ Output reported. |
| **Phase 2: Edge Autonomy & Milestone 2** | **Oct 19 – Nov 1** | • Local detours within lease bounds (FR-4, FR-5)<br>• Safe parking engine on lease expiry (FR-6)<br>• Store-and-forward queue & upload retry (FR-10)<br>• Escalation engine (`CLARIFY`, `UNEXPECTED_EVENT`)<br>• Offline decision quality experiment (E2) | • P1: Moving obstacle avoidance + reflex stop (FR-1, FR-2)<br>• P3: Bedrock escalation handling & re-planning (FR-16)<br>• P4: Requester UI (FR-21) + Operator console map (FR-22) | **Milestone 2 (MVP Complete):** Scenarios SC-1 to SC-3 and Tasks T1–T5 run end-to-end through Requester Web App. |
| **Phase 3: Baselines, Faults & Milestone 3** | **Nov 2 – Nov 6** | • Edge failover/takeover between nodes (FR-7)<br>• Monotonic versioning & deduplication audit (FR-8, E6)<br>• Network degradation testing (tc/netem, E4, E5)<br>• Baseline C2 (Cloud-only relay) integration | • P1: Finalize multi-robot Gazebo fleet (3–10 robots)<br>• P3: No-LLM classical fallback engine (C3, FR-18)<br>• P4: WAN trace replay & CloudWatch cost calculation | **Milestone 3:** C1 vs C2 vs C3 comparative benchmark results plotted. GitHub repo & report submitted (Nov 6). |
| **Post-M3: Red-Teaming, Viva & Demo** | **Nov 7 – Nov 28** | • Multi-robot scaling experiment E7 (up to 10 robots, NFR-3)<br>• Red-team testing against competitor team systems<br>• Viva preparation & Final Industry Demo (Nov 28) | • Entire team: bug fixes, demo scripting, stretch hardware PiCar-X (E8) if approved. | **Final Demo:** Live evaluation before the IISc industrial examination panel. |

---

## 2. Person 2 Detailed Work Breakdown Structure (WBS)

### Phase 1: Foundations & Milestone 1 (Target: Oct 18) — *IN PROGRESS*
- [x] **Task 1.1: Architecture & Interface Contract Alignment with Team Repo**
  - Locked down JSON Schemas directly matching `contracts/` in `vaibhav3000/nimbus`: `lease.schema.json`, `escalation.schema.json`, `robot_subtask.schema.json`, `decision_log_entry.schema.json`.
  - Agreed with Person 4 on **HTTP REST Transport** for Milestone 1 (`server.py` at `http://localhost:8000`: `GET /outbox`, `POST /escalations`, `POST /checkins`).
- [x] **Task 1.2: Experiment E1 — Edge SLM Benchmarking & Local Run (NFR-1)**
  - Configured Ollama with `qwen2.5:1.5b` on local CPU.
  - Verified decision latency: **5.05 seconds** (comfortably under the $10.0$ s ceiling for NFR-1).
  - Verified 100% compliant JSON output adhering to tactical schema.
- [x] **Task 1.3: Local Persistence Engine (`edge/store`) (FR-8, FR-10)**
  - SQLite database backing store (`leases`, `robot_commands`, `decision_log`, `outbox`).
  - Monotonic sequence / lease version enforcement (drop stale/duplicate messages).
- [ ] **Task 1.4: Edge Supervisor Core Loop with HTTP Client (`edge/supervisor`) (FR-3, FR-5)**
  - Implement HTTP outbox poller (`GET /outbox`) and escalation pusher (`POST /escalations`).
  - Implement task sequence processor: iterate through `lease.task_sequence`, checking zones and dispatching primitives.
  - Watchdog expiration timer monitoring `expiry_ts`.
- [x] **Task 1.5: Mock Environment for Standalone Integration Testing**
  - `MockRobot` simulating PiCar-X navigation responses (`go_to`, `wait`, `photo`, `beep`).
- [ ] **Task 1.6: Milestone 1 End-to-End Live Validation (Task T1)**
  - Run `server.py` locally.
  - Submit clinical request for Task T1 (routine delivery to 2B-12) via API / Web App.
  - Edge Supervisor fetches lease from `GET /outbox`, decomposes and executes with Mock Robot, and pushes `POST /escalations` with `OUTPUT_READY`.
  - Confirm task transitions to `completed` in `server.py`!

---

### Phase 2: Edge Autonomy & Robustness (Target: Nov 1)
- [ ] **Task 2.1: Lease Boundary & Safe Parking Enforcement (FR-5, FR-6)**
  - Spatial geofence validator: check all waypoints against `allowed_zones` and `forbidden_zones`.
  - Expiry interrupt: when `valid_until` expires, immediately cancel current errand and dispatch command to nearest `safe_parking_spots`.
  - Guarantee robot never stalls in doorways, corridors, or elevator entrances.
- [ ] **Task 2.2: Local Tactical Autonomy & Edge SLM Invocation (FR-3, FR-4)**
  - Connect Edge Supervisor to pinned local SLM via `llama.cpp` inference API.
  - Implement tactical decision actions: `LOCAL_WAIT` (transient obstacle), `LOCAL_DETOUR` (alternate route within allowed zones), and `SWAP_ROBOT`.
  - Implement deterministic rule-based fallback if SLM generation times out ($>8$ s) or produces invalid output.
- [ ] **Task 2.3: Escalation Pipeline (`CLARIFY` & `UNEXPECTED_EVENT`) (FR-9)**
  - Trigger `CLARIFY` when destination or item instructions are ambiguous.
  - Trigger `UNEXPECTED_EVENT` when corridor is blocked and no detour exists within leased zones.
  - Package camera artifacts and metrics for `OUTPUT_READY`.
- [ ] **Task 2.4: Store-and-Forward WAN Outage Resilience (FR-10, FR-12, NFR-2)**
  - Implement outbox upload worker with exponential backoff and persistent disk queue.
  - Verify that during 10-minute network outages, zero task outputs or decision logs are lost.
  - Generate and transmit Reconnection Summary upon WAN recovery.
- [ ] **Task 2.5: Experiment E2 (Offline Decision Quality Benchmark)**
  - Curate 40 hand-labelled hospital scenario prompts.
  - Compare accuracy: Edge SLM vs Cloud LLM vs Deterministic Rules (target $\ge 80\%$ correct tactical actions).

---

### Phase 3: Fault Injection, Baselines & Benchmarking (Target: Nov 6)
- [ ] **Task 3.1: Edge Node Failover & High Availability (FR-7, E5)**
  - Implement shared/replicated SQLite state heartbeat between primary and standby edge nodes.
  - Simulate primary node crash mid-task; verify standby takes over within $\approx 30$ seconds with zero duplicate sub-tasks executed.
- [ ] **Task 3.2: Message Deduplication & Idempotency Testing (FR-8, E6)**
  - Injected out-of-order, duplicate, and stale lease version messages into the edge supervisor.
  - Verify that duplicate sub-tasks are ignored and zero duplicate physical actions occur.
- [ ] **Task 3.3: Network Degradation Replay (E4, E5 with Person 4)**
  - Integrate with `tc/netem` WAN degradation harness replaying real IISc corridor/lift RF traces.
  - Benchmark Task T2, T4, T5 under packet loss and disconnection.
- [ ] **Task 3.4: Baseline C2 (Cloud-Only Relay) Mode Support**
  - Add configuration flag `SYSTEM_MODE = "C1" | "C2" | "C3"`.
  - Under C2 mode, Edge Supervisor bypasses local SLM and forwards every non-reflex event to Cloud LLM.

---

### Phase 4: Red-Teaming, Scaling & Final Viva (Target: Nov 28)
- [ ] **Task 4.1: Multi-Robot Fleet Scaling (NFR-3, E7)**
  - Scale Edge Supervisor to concurrently manage 3, 6, and 10+ simulated robots across multiple wings.
  - Assert p95 edge decision latency stays within $1.5\times$ baseline.
- [ ] **Task 4.2: Red-Team Defense & Cross-Team Testing**
  - Inject adversarial leases (conflicting zones, impossible deadlines, corrupt tokens) and ensure edge supervisor rejects them cleanly.
- [ ] **Task 4.3: Final Project Submission & Demonstration**
  - Code freeze, comprehensive test suite pass, documentation, and live demo script rehearsal.

---

## 3. Immediate Action Plan for Phase 1 (Next 48 Hours)

To ensure Person 2 leads Milestone 1 without blocking on teammates:

1. **Step 1 (Schema & Models):** Create Python validation models and JSON Schema files in `edge/schemas/`.
2. **Step 2 (Local Store):** Implement `edge/store/local_store.py` providing SQLite CRUD for leases, commands, decision logs, and the outbox.
3. **Step 3 (Edge Supervisor Engine):** Implement `edge/supervisor/supervisor.py` managing state transitions, lease timers, sub-task dispatch, and safe parking.
4. **Step 4 (Benchmarking Harness for E1):** Implement `benchmarks/e1_benchmark.py` allowing instant evaluation of model latency, token rate, and JSON compliance.
5. **Step 5 (Mock Harness & Integration Test):** Implement `edge/mock/mock_robot.py` and `tests/test_milestone1_flow.py` to prove Task T1 runs end-to-end today!
