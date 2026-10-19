# Nimbus — Edge Tier & Local Autonomy (Person 2)

Welcome to the **Edge Tier** component of project **Nimbus** (Leased Authority for Agentic Ground-Robot Navigation across Edge and Cloud).

This folder contains the complete runtime, supervisor, tactical reasoner (SLM), local durable store, mock robot driver, unit tests, and Experiment E1 benchmark suite for Person 2's scope.

---

## Folder Structure

```
edge/
├── supervisor/
│   └── supervisor.py          # Edge Supervisor loop: boundary enforcement (FR-5), safe park (FR-6), REST sync
├── slm/
│   └── engine.py              # Tactical SLM Engine (Ollama / llama.cpp / mock / rule fallback)
├── store/
│   └── local_store.py         # Local SQLite store & outbox store-and-forward (FR-8, FR-10, NFR-6)
├── mock/
│   └── mock_robot.py          # Robot primitive execution simulator (go_to, wait, photo, beep, stop, report)
├── schemas/
│   ├── lease.schema.json      # Draft 2020-12 wire schema for cloud leases
│   ├── robot_subtask.schema.json
│   ├── escalation.schema.json
│   ├── robot_cmd.schema.json
│   └── validator.py           # Local jsonschema validator
├── benchmarks/
│   ├── e1_benchmark.py        # Experiment E1 benchmark script (latency & JSON validity)
│   └── e1_results.md          # Official benchmark results on NVIDIA Tesla T4 GPU (p95 = 0.495 s)
├── tests/
│   └── test_edge_tier.py      # Unit & boundary tests (100% passing)
├── demo_milestone1_t1.py      # Interactive end-to-end demo for Task T1 routine delivery
├── SPEC-P2.md                 # Person 2 Edge Tier architecture specification
└── PLAN-P2.md                 # Milestone delivery plan & verification matrix
```

---

## Quickstart & Verification

### 1. Run Unit Tests
From the repository root:
```bash
python -m pytest edge/tests/test_edge_tier.py -v
```
All 5 automated tests verify:
- Ingestion and JSON schema validation of valid Draft 2020-12 leases (FR-5)
- Monotonic version updates & duplicate suppression (FR-8)
- Immediate escalation and rejection on forbidden zone entry (FR-5, FR-9)
- Safe parking maneuver on lease expiration (FR-6)
- Store-and-forward outbox logging in SQLite (FR-10, NFR-6)

### 2. Run Milestone 1 Task T1 Demo
Demonstrates routine hospital delivery (Central Supply $\to$ Ward 2B Bed 12) executed under lease authority:
```bash
python edge/demo_milestone1_t1.py
```

### 3. Run Experiment E1 SLM Benchmark
Profiles local SLM decision latency and JSON syntax compliance:
```bash
python edge/benchmarks/e1_benchmark.py qwen2.5:1.5b
```
*(Verified on Tesla T4 GPU: p95 latency = **0.495 s** vs NFR-1 target $\le 10.0$ s, 100% JSON validity).*

---

## Integration Interfaces

- **With Cloud (Person 3 / 4):**
  - Cloud posts leases to `/outbox` or drops into `data/outbox.jsonl`.
  - Edge supervisor periodically polls `GET /outbox`, processes tasks, and forwards completed reports or escalations to `POST /escalations`.
- **With Robot (Person 1):**
  - Edge supervisor dispatches concrete commands using `MockRobot` primitives (`go_to`, `wait`, `photo`, `beep`, `stop`, `report`).
  - When Person 1's ROS 2 bridge / Gazebo stack is active, `MockRobot` is swapped with `Ros2Robot` publishing to `/nimbus/robot_cmd`.
