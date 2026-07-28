from pathlib import Path

from synapse.tool_output_eval import evaluate_cases, load_cases, summarize_results


def test_fixture_eval_preserves_required_facts_and_saves_space() -> None:
    fixture = Path(__file__).parent / "fixtures" / "tool_output_eval.json"
    results = evaluate_cases(load_cases(fixture))
    summary = summarize_results(results)

    assert len(results) == 3
    assert all(item.passed for item in results)
    assert summary["passed"] == summary["cases"]
    assert summary["required_retention"] == 1.0
    assert summary["savings_ratio"] > 0
