# Nimbus Experiment E1 Benchmark Results

- **Model:** `qwen2.5:1.5b` (4-bit quant, 150-token cap)
- **Host:** Linux x86_64 (x86_64)
- **Engine Mode:** `ollama`
- **Date:** 2026-10-19 14:42:19
- **Workload:** 15 decisions over 5 hospital tactical scenarios

## Latency Percentiles (NFR-1 Audit)

| Metric | Measured Value | NFR-1 Target | Compliance |
|:---|:---|:---|:---|
| **Min Latency** | `0.000 s` | N/A | OK |
| **Median (p50)** | `0.606 s` | N/A | OK |
| **Mean** | `5.967 s` | N/A | OK |
| **p95 Latency** | **`30.021 s`** | **$\le 10.0$ s** | **FAIL** |
| **Max Latency** | `30.021 s` | N/A | OK |
| **JSON Syntax Adherence** | **`100.0%`** | **100%** | **OK** |

## Trial Breakdown

| Trial | Scenario ID | Description | Decided By | Action | Latency (s) |
|:---:|:---:|:---|:---:|:---:|:---:|
| 1 | SC-01 | Routine waypoint progression in clear corridor | `RULE` | `DISPATCH_STEP` | 30.02 s |
| 1 | SC-02 | Dynamic obstacle: maintenance cart in corridor | `RULE` | `DISPATCH_STEP` | 30.01 s |
| 1 | SC-03 | Instruction ambiguity: multiple pediatric beds | `SLM` | `SWAP_ROBOT` | 23.82 s |
| 1 | SC-04 | Corridor blocked, no alternate route inside allowed zones | `SLM` | `SWAP_ROBOT` | 0.61 s |
| 1 | SC-05 | Lease expiry imminent (<30s remaining) | `RULE` | `SAFE_PARK` | 0.00 s |
| 2 | SC-01 | Routine waypoint progression in clear corridor | `SLM` | `SWAP_ROBOT` | 0.50 s |
| 2 | SC-02 | Dynamic obstacle: maintenance cart in corridor | `SLM` | `SWAP_ROBOT` | 0.41 s |
| 2 | SC-03 | Instruction ambiguity: multiple pediatric beds | `SLM` | `SWAP_ROBOT` | 0.73 s |
| 2 | SC-04 | Corridor blocked, no alternate route inside allowed zones | `SLM` | `SWAP_ROBOT` | 0.92 s |
| 2 | SC-05 | Lease expiry imminent (<30s remaining) | `RULE` | `SAFE_PARK` | 0.00 s |
| 3 | SC-01 | Routine waypoint progression in clear corridor | `SLM` | `SWAP_ROBOT` | 0.45 s |
| 3 | SC-02 | Dynamic obstacle: maintenance cart in corridor | `SLM` | `SWAP_ROBOT` | 0.85 s |
| 3 | SC-03 | Instruction ambiguity: multiple pediatric beds | `SLM` | `SWAP_ROBOT` | 0.61 s |
| 3 | SC-04 | Corridor blocked, no alternate route inside allowed zones | `SLM` | `SWAP_ROBOT` | 0.59 s |
| 3 | SC-05 | Lease expiry imminent (<30s remaining) | `RULE` | `SAFE_PARK` | 0.00 s |
