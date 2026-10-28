"""The offline metrics that say whether the ranking agrees with recruiters."""
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


def test_the_ranking_is_scored_against_recruiter_decisions():
    ranked = [("a", "strong_match", 90.0), ("b", "low_match", 20.0), ("c", "ineligible", 99.0)]
    reviews = {"a": {"status": "shortlisted"}, "b": {"status": "rejected"}, "c": {"status": "rejected"},
               "d": {"status": "pending"}}
    report = evaluate_order(ranked, reviews)
    assert report["labelled"] == 3 and report["relevant"] == 1
    assert report["ndcg_at_10"] == pytest.approx(1.0)
    # b was rejected and was not marked ineligible: one of two rejections.
    assert report["mandatory_false_positive_rate"] == 0.5


def test_a_ranking_that_buries_the_pick_scores_lower():
    reviews = {"a": {"status": "shortlisted"}, "b": {"status": "rejected"}}
    good = evaluate_order([("a", "strong_match", 90.0), ("b", "low_match", 10.0)], reviews)
    bad = evaluate_order([("a", "low_match", 10.0), ("b", "strong_match", 90.0)], reviews)
    assert good["ndcg_at_10"] > bad["ndcg_at_10"]


def test_no_decisions_means_no_claim():
    assert evaluate_order([("a", "strong_match", 90.0)], {}) is None
    assert summarise([None])["orders"] == 0
