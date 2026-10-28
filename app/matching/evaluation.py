"""Offline evaluation: does the matcher rank the people recruiters pick near the top?

The labels are the recruiters' own decisions on matched candidates —
`shortlisted` is relevant, `rejected` is not, everything else is unlabelled
and left out — and the ranking is scored against them:

    precision@10    shortlisted among the top ten labelled candidates
    recall@20       share of all shortlisted candidates found in the top twenty
    ndcg@10         ranking quality with binary relevance
    mandatory FPR   candidates the matcher did not mark ineligible whom a
                    recruiter rejected — an upper bound on eligibility false
                    positives, since a rejection can have other reasons

This is what weights and thresholds (`scoring.DEFAULT_WEIGHTS`, or an order's
`matchingWeights`) should be tuned against. One bias to keep in mind: recruiters
mostly decide on candidates the screen already showed them near the top, so the
labels lean toward whatever ranking produced them.

Run against a live database with:

    python -m app.matching.evaluation JO-1 JO-2 ...
"""
from __future__ import annotations

import math
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.matching import scoring


def precision_at(ranked: Sequence[str], relevant: set, k: int) -> float:
    top = list(ranked)[:k]
    return sum(1 for item in top if item in relevant) / k if k else 0.0


def recall_at(ranked: Sequence[str], relevant: set, k: int) -> float:
    if not relevant:
        return 0.0
    return sum(1 for item in list(ranked)[:k] if item in relevant) / len(relevant)


def ndcg_at(ranked: Sequence[str], relevant: set, k: int) -> float:
    gains = [1.0 if item in relevant else 0.0 for item in list(ranked)[:k]]
    dcg = sum(gain / math.log2(index + 2) for index, gain in enumerate(gains))
    ideal = sum(1.0 / math.log2(index + 2) for index in range(min(k, len(relevant))))
    return dcg / ideal if ideal else 0.0


def evaluate_order(ranked: List[Tuple[str, str, float]], reviews: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """`ranked` is (candidate_id, category, score), best first, for one order."""
    labelled = {cid: review.get("status") for cid, review in reviews.items()
                if review.get("status") in ("shortlisted", "rejected")}
    scored = [item for item in ranked if item[0] in labelled]
    if not scored:
        return None
    relevant = {cid for cid, status in labelled.items() if status == "shortlisted"}
    rejected = {cid for cid, status in labelled.items() if status == "rejected"}
    order = [cid for cid, _category, _score in sorted(scored, key=lambda item: scoring.rank_key(item[1], item[2]))]
    passed_gate = {cid for cid, category, _score in scored if category != scoring.INELIGIBLE}
    return {
        "labelled": len(scored),
        "relevant": len(relevant),
        "precision_at_10": round(precision_at(order, relevant, 10), 4),
        "recall_at_20": round(recall_at(order, relevant, 20), 4),
        "ndcg_at_10": round(ndcg_at(order, relevant, 10), 4),
        "mandatory_false_positive_rate": round(len(rejected & passed_gate) / len(rejected), 4) if rejected else None,
    }


def summarise(per_order: Iterable[Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    rows = [row for row in per_order if row]
    if not rows:
        return {"orders": 0, "note": "No recruiter decisions to evaluate against yet."}

    def mean(key: str) -> float:
        values = [float(row[key]) for row in rows if isinstance(row.get(key), (int, float))]
        return round(sum(values) / len(values), 4) if values else 0.0

    return {
        "orders": len(rows),
        "labelled": sum(row["labelled"] for row in rows),
        **{key: mean(key) for key in ("precision_at_10", "recall_at_20", "ndcg_at_10",
                                      "mandatory_false_positive_rate")},
    }


def run(order_ids: Sequence[str]) -> Dict[str, Any]:
    from app.db.mongo import get_db
    from app.matching import matcher, store

    per_order: Dict[str, Any] = {}
    for order_id in order_ids:
        order = get_db()["job_orders"].find_one({"id": order_id}, {"_id": 0})
        if not order:
            per_order[order_id] = None
            continue
        ranking = matcher.rank(order)
        ranked = [(m.candidate_id, m.category, m.score) for m in ranking.matches]
        per_order[order_id] = evaluate_order(ranked, store.reviews_for_order(order_id))
    return {"per_order": per_order, "summary": summarise(per_order.values())}


if __name__ == "__main__":  # pragma: no cover — operator entry point
    import json

    print(json.dumps(run(sys.argv[1:]), indent=2, default=str))
