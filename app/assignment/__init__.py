"""Candidate → staff allocation."""

from app.assignment.balancer import (
    AssignmentResult,
    assign_candidate,
    rebalance_all,
)

__all__ = [
    "AssignmentResult",
    "assign_candidate",
    "rebalance_all",
]
