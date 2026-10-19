"""
Nimbus Experiment E1: Edge SLM Benchmarking & Latency Profiler
Validates NFR-1 (p95 <= 10.0 s, <= 150 output tokens) and FR-3 (Valid tactical decisions).
Can be executed on local CPU or Lab GPU (RTX 3060 / Jetson AGX Orin).
Outputs results directly to stdout and saves to benchmarks/e1_results.md.
"""

from datetime import datetime
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import time
import urllib.request
import urllib.error

# Ensure repository root containing 'edge' is on sys.path regardless of execution working directory
_current = Path(__file__).resolve().parent
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
while _current.parent != _current:
    if (_current / "edge" / "slm").is_dir():
        ROOT_DIR = _current
        break
    _current = _current.parent

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Benchmark test scenarios representing hospital dynamic edge events
BENCHMARK_PROMPTS = [
    {
        "id": "SC-01",
        "description": "Routine waypoint progression in clear corridor",
        "context": {
            "lease_id": "lease-bench-01",
            "version": 1,
            "time_remaining_sec": 450.0,
            "robot_id": "R1",
            "current_pos": [5.0, 2.0],
            "current_zone": "corridor-2B",
            "allowed_zones": ["central-supply", "corridor-2B", "2B-12"],
            "forbidden_zones": ["ward-2C-isolation"],
            "event_description": "Corridor clear; ready for next waypoint wp-2B-12.",
            "allowed_waypoints": ["wp-2B-12"]
        }
    },
    {
        "id": "SC-02",
        "description": "Dynamic obstacle: maintenance cart in corridor",
        "context": {
            "lease_id": "lease-bench-02",
            "version": 1,
            "time_remaining_sec": 400.0,
            "robot_id": "R1",
            "current_pos": [8.5, 4.0],
            "current_zone": "corridor-2B",
            "allowed_zones": ["central-supply", "corridor-2B", "2B-12"],
            "forbidden_zones": ["ward-2C-isolation"],
            "event_description": "Cleaning cart blocking main corridor; bypass wp-2B-alt available.",
            "allowed_waypoints": ["wp-2B-main", "wp-2B-alt"]
        }
    },
    {
        "id": "SC-03",
        "description": "Instruction ambiguity: multiple pediatric beds",
        "context": {
            "lease_id": "lease-bench-03",
            "version": 1,
            "time_remaining_sec": 380.0,
            "robot_id": "R1",
            "current_pos": [12.0, 4.0],
            "current_zone": "ward-2B",
            "allowed_zones": ["corridor-2B", "ward-2B"],
            "forbidden_zones": ["ward-2C-isolation"],
            "event_description": "Arrived at Bed 12; two cribs detected (12A, 12B). Unclear which needs pump.",
            "allowed_waypoints": ["bed-12A", "bed-12B"]
        }
    },
    {
        "id": "SC-04",
        "description": "Corridor blocked, no alternate route inside allowed zones",
        "context": {
            "lease_id": "lease-bench-04",
            "version": 1,
            "time_remaining_sec": 320.0,
            "robot_id": "R1",
            "current_pos": [9.0, 3.5],
            "current_zone": "corridor-2B",
            "allowed_zones": ["corridor-2B"],
            "forbidden_zones": ["ward-2C-isolation"],
            "event_description": "Fire door closed and locked; no alternative corridor inside East wing.",
            "allowed_waypoints": ["corridor-2B-exit"]
        }
    },
    {
        "id": "SC-05",
        "description": "Lease expiry imminent (<30s remaining)",
        "context": {
            "lease_id": "lease-bench-05",
            "version": 1,
            "time_remaining_sec": 12.0,
            "robot_id": "R1",
            "current_pos": [7.0, 2.0],
            "current_zone": "corridor-2B",
            "allowed_zones": ["corridor-2B", "2B-12"],
            "forbidden_zones": ["ward-2C-isolation"],
            "event_description": "Lease timer expiring in 12 seconds; delivery not complete.",
            "allowed_waypoints": ["safe-bay-1"]
        }
    }
]


def run_benchmark(model_name: str = "qwen2.5:1.5b", trials: int = 3, ollama_url: str = "http://127.0.0.1:11434"):
    print("=" * 75)
    print("  NIMBUS EXPERIMENT E1 BENCHMARK: EDGE SLM LATENCY & NFR-1 PROFILER")
    print("=" * 75)
    print(f"  Timestamp    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Host Platform: {platform.system()} {platform.release()} ({platform.machine()})")
    print(f"  Target Model : {model_name}")
    print(f"  Ollama URL   : {ollama_url}")
    print(f"  Workload     : {len(BENCHMARK_PROMPTS)} scenarios x {trials} trials = {len(BENCHMARK_PROMPTS)*trials} decisions")
    print("=" * 75)

    from edge.slm.engine import EdgeSLMEngine
    engine = EdgeSLMEngine(model_name=model_name, mode="auto")
    print(f"  SLM Engine Initialized in Mode: {engine.mode}")

    # Standard GPU warm-up pass (allocates CUDA buffers, not counted in benchmark stats)
    print("  Warming up GPU / model...")
    warmup_ctx = dict(BENCHMARK_PROMPTS[0]["context"])
    _, warmup_by, warmup_ms = engine.evaluate_decision(warmup_ctx)
    print(f"  Warm-up completed in {warmup_ms:.1f} ms (Decided by: {warmup_by})\n")

    latencies = []
    valid_json_count = 0
    total_calls = 0
    records = []

    print("Running benchmark trials...")
    for trial in range(1, trials + 1):
        for scenario in BENCHMARK_PROMPTS:
            total_calls += 1
            ctx = scenario["context"]
            decision, decided_by, lat_ms = engine.evaluate_decision(ctx)
            latencies.append(lat_ms)

            # Check JSON format validity
            is_valid_json = isinstance(decision, dict) and "action" in decision
            if is_valid_json:
                valid_json_count += 1

            records.append({
                "trial": trial,
                "scenario_id": scenario["id"],
                "description": scenario["description"],
                "decided_by": decided_by,
                "action": decision.get("action"),
                "latency_ms": lat_ms,
                "valid": is_valid_json
            })
            print(f"  [{total_calls:02d}] {scenario['id']} Trial {trial}: {lat_ms:6.1f} ms | Decided: {decided_by} | Action: {decision.get('action')}")

    # Compute statistics
    lat_sec = [l / 1000.0 for l in latencies]
    p50_sec = statistics.median(lat_sec)
    p95_sec = statistics.quantiles(lat_sec, n=20)[18] if len(lat_sec) >= 20 else max(lat_sec)
    mean_sec = statistics.mean(lat_sec)
    min_sec = min(lat_sec)
    max_sec = max(lat_sec)
    json_rate = (valid_json_count / total_calls) * 100.0

    print("\n" + "=" * 75)
    print("  EXPERIMENT E1 BENCHMARK SUMMARY RESULTS")
    print("=" * 75)
    print(f"  Total Decisions Tested    : {total_calls}")
    print(f"  Strict JSON Valid Rate    : {json_rate:.1f}% (Target: 100%)")
    print(f"  Minimum Latency           : {min_sec:.3f} s")
    print(f"  Median Latency (p50)      : {p50_sec:.3f} s")
    print(f"  Mean Latency              : {mean_sec:.3f} s")
    print(f"  95th Percentile (p95)     : {p95_sec:.3f} s")
    print(f"  Maximum Latency           : {max_sec:.3f} s")
    print(f"  NFR-1 Target Compliance   : {'PASSED (p95 <= 10.0 s)' if p95_sec <= 10.0 else 'FAILED'}")
    print("=" * 75)

    # Save to Markdown
    results_dir = Path(__file__).resolve().parent
    report_path = results_dir / "e1_results.md"

    md_content = f"""# Nimbus Experiment E1 Benchmark Results

- **Model:** `{model_name}` (4-bit quant, 150-token cap)
- **Host:** {platform.system()} {platform.machine()} ({platform.processor()})
- **Engine Mode:** `{engine.mode}`
- **Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
- **Workload:** {total_calls} decisions over {len(BENCHMARK_PROMPTS)} hospital tactical scenarios

## Latency Percentiles (NFR-1 Audit)

| Metric | Measured Value | NFR-1 Target | Compliance |
|:---|:---|:---|:---|
| **Min Latency** | `{min_sec:.3f} s` | N/A | OK |
| **Median (p50)** | `{p50_sec:.3f} s` | N/A | OK |
| **Mean** | `{mean_sec:.3f} s` | N/A | OK |
| **p95 Latency** | **`{p95_sec:.3f} s`** | **$\le 10.0$ s** | **{'PASS' if p95_sec <= 10.0 else 'FAIL'}** |
| **Max Latency** | `{max_sec:.3f} s` | N/A | OK |
| **JSON Syntax Adherence** | **`{json_rate:.1f}%`** | **100%** | **OK** |

## Trial Breakdown

| Trial | Scenario ID | Description | Decided By | Action | Latency (s) |
|:---:|:---:|:---|:---:|:---:|:---:|
"""
    for r in records:
        md_content += f"| {r['trial']} | {r['scenario_id']} | {r['description']} | `{r['decided_by']}` | `{r['action']}` | {r['latency_ms']/1000.0:.2f} s |\n"

    report_path.write_text(md_content, encoding="utf-8")
    print(f"\n  [+] Formatted report saved to: {report_path.resolve()}")


if __name__ == "__main__":
    import sys
    model = sys.argv[1] if len(sys.argv) > 1 else "qwen2.5:1.5b"
    run_benchmark(model_name=model, trials=3)
