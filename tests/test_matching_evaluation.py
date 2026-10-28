"""The offline metrics that decide whether Jev is trusted over the baseline."""
from __future__ import annotations

import pytest

from app.matching.evaluation import evaluate_order, ndcg_at, precision_at, recall_at, summarise


def test_ranking_metrics_on_a_known_case():
    ranked = ["a", "b", "c", "d"]
    relevant = {"a", "c"}
    assert precision_at(ranked, relevant, 2) == 0.5
    assert recall_at(ranked, relevant, 3) == 1.0
    assert ndcg_at(["a", "c", "b"], relevant, 3) == pytest.approx(1.0)
    assert ndcg_at(["b", "a", "c"], relevant, 3) < 1.0
    assert recall_at(ranked, set(), 3) == 0.0


def test_jev_and_the_baseline_are_scored_against_recruiter_decisions():
    results = [
        {"candidate_id": "a", "category": "strong_match", "score": 90, "baseline": {"score": 10},
         "usage": {"cost_usd": 0.0001}},
        {"candidate_id": "b", "category": "low_match", "score": 20, "baseline": {"score": 95},
         "usage": {"cost_usd": 0.0001}},
        {"candidate_id": "c", "category": "ineligible", "score": 99, "baseline": {"score": 50},
         "usage": {"cost_usd": 0.0001}},
    ]
    reviews = {"a": {"status": "shortlisted"}, "b": {"status": "rejected"}, "c": {"status": "rejected"},
               "d": {"status": "pending"}}
    report = evaluate_order(results, reviews)
    assert report["labelled"] == 3 and report["relevant"] == 1
    assert report["jev"]["ndcg_at_10"] == pytest.approx(1.0)
    assert report["baseline"]["ndcg_at_10"] < 1.0
    # b was rejected and Jev did not mark it ineligible: one of two rejections.
    assert report["mandatory_false_positive_rate"] == 0.5
    assert report["cost_usd"] == pytest.approx(0.0003)


def test_no_decisions_means_no_claim():
    assert evaluate_order([{"candidate_id": "a"}], {}) is None
    assert summarise([None])["orders"] == 0
