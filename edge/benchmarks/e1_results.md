# Nimbus Experiment E1 Benchmark Results

- **Model:** `qwen2.5:1.5b` (4-bit quant, 150-token cap)
- **Host:** Linux x86_64 (NVIDIA Tesla T4 GPU, 16 GB VRAM)
- **Engine Mode:** `ollama` (CUDA Offload: 29/29 layers)
- **Workload:** 15 decisions over 5 hospital tactical scenarios (3 trials)

## Latency Percentiles (NFR-1 Audit)

| Metric | Measured Value | NFR-1 Target | Compliance |
|:---|:---|:---|:---|
| **Min Latency** | `0.000 s` (Reflex rule) | N/A | OK |
| **Median (p50)** | `0.377 s` | N/A | OK |
| **Mean** | `0.318 s` | N/A | OK |
| **p95 Latency** | **`0.495 s`** | **$\le 10.0$ s** | **PASS** |
| **Max Latency** | `0.495 s` | N/A | OK |
| **JSON Syntax Adherence** | **`100.0%`** | **100%** | **OK** |

## Trial Breakdown

| Trial | Scenario ID | Description | Decided By | Action | Latency (s) |
|:---:|:---:|:---|:---:|:---:|:---:|
| 1 | SC-01 | Routine waypoint progression in clear corridor | `SLM` | `DISPATCH_STEP` | 0.35 s |
| 1 | SC-02 | Dynamic obstacle: maintenance cart in corridor | `SLM` | `LOCAL_DETOUR` | 0.38 s |
| 1 | SC-03 | Instruction ambiguity: multiple pediatric beds | `SLM` | `LOCAL_WAIT` | 0.43 s |
| 1 | SC-04 | Corridor blocked, no alternate route inside allowed zones | `SLM` | `LOCAL_DETOUR` | 0.44 s |
| 1 | SC-05 | Lease expiry imminent (<30s remaining) | `RULE` | `SAFE_PARK` | 0.00 s |
| 2 | SC-01 | Routine waypoint progression in clear corridor | `SLM` | `DISPATCH_STEP` | 0.38 s |
| 2 | SC-02 | Dynamic obstacle: maintenance cart in corridor | `SLM` | `LOCAL_DETOUR` | 0.37 s |
| 2 | SC-03 | Instruction ambiguity: multiple pediatric beds | `SLM` | `LOCAL_WAIT` | 0.40 s |
| 2 | SC-04 | Corridor blocked, no alternate route inside allowed zones | `SLM` | `LOCAL_DETOUR` | 0.39 s |
| 2 | SC-05 | Lease expiry imminent (<30s remaining) | `RULE` | `SAFE_PARK` | 0.00 s |
| 3 | SC-01 | Routine waypoint progression in clear corridor | `SLM` | `DISPATCH_STEP` | 0.38 s |
| 3 | SC-02 | Dynamic obstacle: maintenance cart in corridor | `SLM` | `LOCAL_DETOUR` | 0.38 s |
| 3 | SC-03 | Instruction ambiguity: multiple pediatric beds | `SLM` | `LOCAL_WAIT` | 0.49 s |
| 3 | SC-04 | Corridor blocked, no alternate route inside allowed zones | `SLM` | `LOCAL_DETOUR` | 0.40 s |
| 3 | SC-05 | Lease expiry imminent (<30s remaining) | `RULE` | `SAFE_PARK` | 0.00 s |
