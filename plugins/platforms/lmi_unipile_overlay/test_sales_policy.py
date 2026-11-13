from __future__ import annotations

import importlib.util
from pathlib import Path


POLICY_PATH = Path(__file__).with_name("sales_policy.py")


def _load_policy_module():
    spec = importlib.util.spec_from_file_location("lmi_sales_policy_under_test", POLICY_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _normalized(text: str) -> str:
    return " ".join(text.split())


def test_live_policy_requires_answer_before_qualification():
    module = _load_policy_module()

    for channel in ("instagram", "whatsapp", "linkedin"):
        policy = _normalized(module.live_sales_policy(channel))
        normalized_policy = policy.lower()
        assert "answer-first decision order" in normalized_policy
        assert "answer the stated question or request directly before qualifying" in normalized_policy
        assert "do not ask a question merely to keep the conversation moving" in normalized_policy


def test_live_policy_uses_thread_facts_and_blocks_repeated_questions():
    module = _load_policy_module()
    policy = _normalized(module.live_sales_policy("instagram"))

    assert "Extract the facts the lead already supplied" in policy
    assert "Never ask for a fact already present in the exact thread" in policy
    assert "Do not ask a question when a complete direct answer is the useful next step" in policy


def test_composed_policy_places_answer_first_rules_last_for_precedence():
    module = _load_policy_module()
    existing = "Every reply must end with a question."

    composed = module.compose_live_sales_policy(existing, "instagram")

    assert composed.index(existing) < composed.index("ANSWER-FIRST DECISION ORDER")
    assert composed.rstrip().endswith("When in doubt, understand and answer; do not interrogate.")
