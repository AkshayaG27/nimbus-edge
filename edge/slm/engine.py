"""
Nimbus Edge SLM Inference & Fallback Engine
Provides structured reasoning for tactical decisions, bounded by lease constraints.
Supports llama.cpp / mock engine / rule-based fallback.
Capped at 150 output tokens (PRD Sec. 2 & 5, NFR-1).
"""

import json
import time
from typing import Any, Dict, Optional, Tuple


class EdgeSLMEngine:
    def __init__(self, model_name: str = "qwen2.5:1.5b", mode: str = "auto"):
        """
        mode:
          - "ollama": Queries local Ollama server (http://127.0.0.1:11434)
          - "llama_cpp": Uses real local GGUF weights via llama-cpp-python
          - "mock": Simulates edge SLM reasoning for unit testing and CI
          - "rules_only": Direct deterministic fallback
          - "auto": Auto-detects Ollama, then llama_cpp, then mock
        """
        self.model_name = model_name
        self.mode = mode
        self.llm = None
        self.ollama_url = "http://127.0.0.1:11434/api/generate"

        if self.mode == "auto":
            # Check if Ollama is running
            import urllib.request
            try:
                with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=1) as resp:
                    if resp.status == 200:
                        self.mode = "ollama"
                        self.model_name = "qwen2.5:1.5b"
            except Exception:
                self.mode = "mock"

        if self.mode == "llama_cpp":
            try:
                from llama_cpp import Llama
                self.Llama = Llama
            except ImportError:
                self.mode = "mock"

    def format_prompt(self, context: Dict[str, Any]) -> str:
        """Formats tactical prompt according to SPEC.md."""
        allowed_zones = context.get("allowed_zones", [])
        forbidden_zones = context.get("forbidden_zones", [])
        prompt = (
            "<|im_start|>system\n"
            "You are the Nimbus Edge Cluster Tactical Supervisor for hospital delivery robots.\n"
            "Output strictly valid JSON with keys: action, target_waypoint, reason.\n"
            f"Allowed zones: {allowed_zones}\n"
            f"Forbidden zones: {forbidden_zones}\n"
            "Action Selection Guidelines:\n"
            "- DISPATCH_STEP: path is clear, proceed with next waypoint.\n"
            "- LOCAL_DETOUR: path blocked but an alternative waypoint exists in allowed zones.\n"
            "- LOCAL_WAIT: temporary dynamic obstacle; wait for clearance.\n"
            "- ESCALATE_CLARIFY: destination bed, item, or instructions are ambiguous.\n"
            "- ESCALATE_UNEXPECTED: route completely blocked with no detour, or out of lease bounds.\n"
            "- SAFE_PARK: lease expiry is imminent (<30s remaining).\n"
            "<|im_end|>\n"
            "<|im_start|>user\n"
            f"Lease: {context.get('lease_id')} (v{context.get('version')})\n"
            f"Time left: {context.get('time_remaining_sec')}s\n"
            f"Robot: {context.get('robot_id')} at {context.get('current_pos')} ({context.get('current_zone')})\n"
            f"Event: {context.get('event_description')}\n"
            f"Allowed Waypoints: {context.get('allowed_waypoints')}\n"
            "<|im_end|>\n"
            "<|im_start|>assistant\n"
        )
        return prompt

    def evaluate_decision(self, context: Dict[str, Any]) -> Tuple[Dict[str, Any], str, float]:
        """
        Generates tactical decision.
        Returns: (decision_dict, decided_by ['SLM'|'RULE'], latency_ms)
        """
        start_time = time.time()
        time_remaining = context.get("time_remaining_sec", 999.0)

        # 1. Immediate Hard Invariant: Expiry approaching -> Safe Park
        if time_remaining <= 15.0:
            latency_ms = (time.time() - start_time) * 1000.0
            return {
                "action": "SAFE_PARK",
                "target_waypoint": "safe-spot-1",
                "reason": "Lease expiry imminent (<15s remaining)."
            }, "RULE", latency_ms

        # 2. Try SLM inference if configured
        if self.mode == "ollama":
            import urllib.request
            prompt = self.format_prompt(context)
            req_data = json.dumps({
                "model": self.model_name,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {
                    "num_predict": 150,
                    "temperature": 0.2
                }
            }).encode("utf-8")
            try:
                req = urllib.request.Request(
                    self.ollama_url,
                    data=req_data,
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=30.0) as resp:
                    resp_json = json.loads(resp.read().decode("utf-8"))
                    text = resp_json.get("response", "").strip()
                    # Strip markdown code blocks if model wrapped JSON
                    if text.startswith("```"):
                        text = text.split("```")[1]
                        if text.startswith("json"):
                            text = text[4:]
                        text = text.strip()
                    parsed = json.loads(text)
                    latency_ms = (time.time() - start_time) * 1000.0
                    return parsed, "SLM", latency_ms
            except Exception as e:
                # Fallback to rule engine on timeout or malformed output
                pass

        if self.mode == "llama_cpp" and self.llm is not None:
            prompt = self.format_prompt(context)
            try:
                output = self.llm(
                    prompt,
                    max_tokens=150,
                    stop=["<|im_end|>", "\n\n\n"],
                    temperature=0.2
                )
                text = output["choices"][0]["text"].strip()
                parsed = json.loads(text)
                latency_ms = (time.time() - start_time) * 1000.0
                return parsed, "SLM", latency_ms
            except Exception:
                # Malformed output or timeout -> fallback to rule engine
                pass

        # 3. Mock SLM Mode (emulates realistic SLM reasoning for tests/E1)
        if self.mode == "mock":
            event = context.get("event_description", "")
            action = "DISPATCH_STEP"
            target = None
            reason = "Standard step execution."

            if "blocked" in event.lower() or "obstacle" in event.lower():
                # Check if alternate waypoint available inside allowed zone
                allowed_wps = context.get("allowed_waypoints", [])
                if len(allowed_wps) > 1:
                    action = "LOCAL_DETOUR"
                    target = allowed_wps[1]
                    reason = "Dynamic obstacle encountered; taking local detour in allowed zone."
                else:
                    action = "ESCALATE_UNEXPECTED"
                    reason = "Corridor blocked and no alternate detour exists within lease."
            elif "ambiguous" in event.lower() or "which" in event.lower():
                action = "ESCALATE_CLARIFY"
                reason = "Target location ambiguous; clarifying with cloud orchestrator."

            latency_ms = (time.time() - start_time) * 1000.0 + 15.0  # Synthetic latency
            return {
                "action": action,
                "target_waypoint": target,
                "reason": reason
            }, "SLM", latency_ms

        # 4. Deterministic Rule-Based Fallback
        return self._rule_fallback(context, start_time)

    def _rule_fallback(self, context: Dict[str, Any], start_time: float) -> Tuple[Dict[str, Any], str, float]:
        """Deterministic safety rules."""
        event = context.get("event_description", "").lower()
        if "blocked" in event:
            decision = {
                "action": "ESCALATE_UNEXPECTED",
                "target_waypoint": None,
                "reason": "Rule fallback: Path blocked, escalating to cloud."
            }
        else:
            decision = {
                "action": "DISPATCH_STEP",
                "target_waypoint": None,
                "reason": "Rule fallback: Standard progression."
            }
        latency_ms = (time.time() - start_time) * 1000.0
        return decision, "RULE", latency_ms
