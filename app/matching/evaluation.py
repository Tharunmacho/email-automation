"""Offline evaluation: does Jev rank better than the keyword baseline?

Nothing in this feature should be trusted to drive a decision until this says
so. The labels are the recruiters' own decisions on matched candidates —
`shortlisted` is relevant, `rejected` is not, everything else is unlabelled
and left out — and both rankings are scored against them:

    jev        stored results, sorted category-then-score as the screen sorts
    baseline   the same candidates, sorted by the stored keyword baseline

Metrics, per order and averaged across orders with at least one label:

    precision@10    shortlisted among the top ten
    recall@20       share of all shortlisted candidates found in the top twenty
    ndcg@10         ranking quality with binary relevance
    mandatory FPR   candidates Jev did not mark ineligible whom a recruiter
                    rejected — an upper bound on eligibility false positives,
                    since a rejection can have other reasons

There is a bias worth stating: recruiters mostly decide on candidates the
screen already showed them near the top, so the labels lean toward whatever
ranking produced them. A fair comparison wants decisions made before Jev was
switched on, or a sample reviewed blind. Until then, read a Jev "win" here as
"not worse", not as proof of better.

Run against a live database with:

    python -m app.matching.evaluation JO-1 JO-2 ...
"""
from __future__ import annotations

import math
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence

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


def evaluate_order(results: List[Dict[str, Any]], reviews: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Both rankings for one order against its recruiter labels."""
    labelled = {cid: review.get("status") for cid, review in reviews.items()
                if review.get("status") in ("shortlisted", "rejected")}
    scored = [r for r in results if r and r.get("candidate_id") in labelled]
    if not scored:
        return None
    relevant = {cid for cid, status in labelled.items() if status == "shortlisted"}

    jev_ranked = [r["candidate_id"] for r in sorted(
        scored, key=lambda r: scoring.rank_key(r.get("category"), r.get("score") or 0))]
    base_ranked = [r["candidate_id"] for r in sorted(
        scored, key=lambda r: -float((r.get("baseline") or {}).get("score") or 0))]

    rejected = {cid for cid, status in labelled.items() if status == "rejected"}
    passed_gate = {r["candidate_id"] for r in scored if r.get("category") != scoring.INELIGIBLE}

    def metrics(ranked: List[str]) -> Dict[str, float]:
        return {
            "precision_at_10": round(precision_at(ranked, relevant, 10), 4),
            "recall_at_20": round(recall_at(ranked, relevant, 20), 4),
            "ndcg_at_10": round(ndcg_at(ranked, relevant, 10), 4),
        }

    cost = sum(float((r.get("usage") or {}).get("cost_usd") or 0) for r in results if r)
    return {
        "labelled": len(scored),
        "relevant": len(relevant),
        "jev": metrics(jev_ranked),
        "baseline": metrics(base_ranked),
        "mandatory_false_positive_rate": round(
            len(rejected & passed_gate) / len(rejected), 4) if rejected else None,
        "cost_usd": round(cost, 6),
    }


def summarise(per_order: Iterable[Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    rows = [row for row in per_order if row]
    if not rows:
        return {"orders": 0, "note": "No recruiter decisions to evaluate against yet."}

    def mean(path: Sequence[str]) -> float:
        values = []
        for row in rows:
            node: Any = row
            for key in path:
                node = (node or {}).get(key)
            if isinstance(node, (int, float)):
                values.append(float(node))
        return round(sum(values) / len(values), 4) if values else 0.0

    out = {"orders": len(rows), "labelled": sum(row["labelled"] for row in rows)}
    for system in ("jev", "baseline"):
        out[system] = {metric: mean((system, metric))
                       for metric in ("precision_at_10", "recall_at_20", "ndcg_at_10")}
    out["mandatory_false_positive_rate"] = mean(("mandatory_false_positive_rate",))
    out["total_cost_usd"] = round(sum(row["cost_usd"] for row in rows), 6)
    return out


def run(order_ids: Sequence[str]) -> Dict[str, Any]:
    from app.matching import store

    per_order = {}
    for order_id in order_ids:
        entries = store.results_for_order(order_id)
        per_order[order_id] = evaluate_order(
            [entry["result"] for entry in entries], store.reviews_for_order(order_id))
    return {"per_order": per_order, "summary": summarise(per_order.values())}


if __name__ == "__main__":  # pragma: no cover — operator entry point
    import json

    print(json.dumps(run(sys.argv[1:]), indent=2, default=str))
