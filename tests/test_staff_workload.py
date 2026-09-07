"""CRM roster coverage for staff accounts that become managers."""
from __future__ import annotations

from unittest.mock import patch

from app.api.routes import staff_workload
from app.db.users import MANAGER_ROLE, STAFF_ROLE, User


class _Users:
    staff = User(id="staff-1", email="staff@example.com", name="Staff", role=STAFF_ROLE)
    manager = User(id="manager-1", email="rafi@example.com", name="Rafi", role=MANAGER_ROLE)

    def list_employees(self, include_inactive=True):
        assert include_inactive is True
        return [self.staff, self.manager]

    def list_staff(self, include_inactive=False):
        assert include_inactive is False
        return [self.staff]


class _Repository:
    received_roster = []

    def unassigned_count(self):
        return 0

    def orphaned_count(self, roster_ids):
        assert roster_ids == ["staff-1", "manager-1"]
        return 0

    def staff_workload(self, roster):
        type(self).received_roster = [member.id for member in roster]
        return [
            {
                **member.to_public(),
                "assigned": 3 if member.id == "manager-1" else 1,
                "evaluated": 0,
                "unviewed": 0,
                "pending": 3 if member.id == "manager-1" else 1,
                "progress": 0,
            }
            for member in roster
        ]


def test_workload_keeps_manager_visible_with_existing_candidates():
    repository = _Repository()
    with patch("app.api.routes.users", _Users()), patch(
        "app.api.routes.repo", return_value=repository
    ):
        result = staff_workload(_admin={})

    assert _Repository.received_roster == ["staff-1", "manager-1"]
    assert result["roster_ids"] == ["staff-1", "manager-1"]
    rafi = next(row for row in result["items"] if row["name"] == "Rafi")
    assert rafi["role"] == MANAGER_ROLE
    assert rafi["assigned"] == 3
    assert result["totals"]["assigned"] == 4

