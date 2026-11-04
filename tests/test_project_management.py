"""Guarded project maintenance: create, rename or re-budget, delete or archive.

The tests drive the real approval kernel: prepare stages an approval, the
``*_from_approval`` entry point claims it, and only then does anything reach the
fake provider -- the same path ``execute_approved_action`` takes.

Every identifier and name below is synthesized.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from moneybird_mcp import safety
from moneybird_mcp.config import MoneybirdError, MoneybirdHTTPError
from moneybird_mcp.credentials import set_active_administration_id
from moneybird_mcp.tools import projects
from moneybird_mcp.tools.approvals import APPROVAL_EXECUTORS
from moneybird_mcp.write_contracts import WRITE_SPECS

ADMINISTRATION = "project-maintenance-admin"


class _ProjectClient:
    """Provider double for the project endpoints."""

    def __init__(self) -> None:
        self.administration_id = ADMINISTRATION
        self.projects: dict[str, dict] = {
            "71001": {"id": "71001", "name": "Renovatie", "state": "active", "budget": 40},
            "71002": {"id": "71002", "name": "Oud project", "state": "archived", "budget": None},
            "71003": {"id": "71003", "name": "Onderhoud", "state": "active", "budget": None},
        }
        self.list_states: list[str] = []
        self.create_calls: list[dict] = []
        self.update_calls: list[tuple[str, dict]] = []
        self.delete_calls: list[str] = []
        self.next_id = 71900
        # Provider behaviours a test can switch on.
        self.archive_on_delete = False
        self.rename_on_archive = False
        self.ignore_budget = False

    def list_projects(self, *, limit=25, page=1, state=""):
        self.list_states.append(state)
        items = list(self.projects.values())
        start = (page - 1) * limit
        return [dict(item) for item in items[start : start + limit]]

    def get_project(self, project_id):
        if project_id not in self.projects:
            raise MoneybirdHTTPError("not found", status_code=404)
        return dict(self.projects[project_id])

    def create_project(self, project):
        self.create_calls.append(dict(project))
        record = {
            "id": str(self.next_id),
            "name": project["name"],
            "state": "active",
            "budget": None if self.ignore_budget else project.get("budget"),
        }
        self.next_id += 1
        self.projects[record["id"]] = record
        return dict(record)

    def update_project(self, project_id, project):
        self.update_calls.append((project_id, dict(project)))
        record = self.projects[project_id]
        record["name"] = project["name"]
        if "budget" in project and not self.ignore_budget:
            record["budget"] = project["budget"]
        return dict(record)

    def delete_project(self, project_id):
        self.delete_calls.append(project_id)
        if self.archive_on_delete:
            self.projects[project_id]["state"] = "archived"
            if self.rename_on_archive:
                self.projects[project_id]["name"] = "Something else"
        else:
            del self.projects[project_id]


class _IsolatedWriteKernel(unittest.TestCase):
    """Give each test its own approval store, audit log and active administration."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="moneybird_projects_")
        self._env = mock.patch.dict(
            os.environ,
            {
                "MONEYBIRD_MCP_DATA_DIR": self._temp_dir.name,
                "MONEYBIRD_CAPABILITY_MODE": "write_enabled",
            },
        )
        self._env.start()
        set_active_administration_id(ADMINISTRATION)
        safety.clear_pending_approvals()
        self.client = _ProjectClient()
        self._patch = mock.patch.object(
            projects.ctx, "get_client", return_value=self.client
        )
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        safety.clear_pending_approvals()
        set_active_administration_id(None)
        self._env.stop()
        self._temp_dir.cleanup()


class RegistrationTests(unittest.TestCase):
    def test_each_action_has_an_executor_and_a_write_contract(self) -> None:
        for action in ("create_project", "update_project", "delete_project"):
            with self.subTest(action=action):
                self.assertIn(action, APPROVAL_EXECUTORS)
                self.assertIn(action, WRITE_SPECS)


class CreateProjectTests(_IsolatedWriteKernel):
    def test_prepare_stages_without_writing_and_checks_every_state(self) -> None:
        prepared = projects.prepare_create_project(name=" Nieuwbouw ", budget_hours=120)

        self.assertEqual(self.client.create_calls, [])
        self.assertEqual(self.client.list_states, ["all"])
        self.assertEqual(
            prepared["preview"]["project"], {"name": "Nieuwbouw", "budget": 120}
        )

    def test_executes_once_and_verifies_by_reading_the_project(self) -> None:
        prepared = projects.prepare_create_project(name="Nieuwbouw", budget_hours=120)
        result = projects.create_project_from_approval(prepared["approval_id"])

        self.assertEqual(self.client.create_calls, [{"name": "Nieuwbouw", "budget": 120}])
        self.assertEqual(result["status"], "created")
        self.assertTrue(result["verification"]["fully_verified"])
        self.assertEqual(
            result["project"],
            {"id": "71900", "name": "Nieuwbouw", "state": "active", "budget": 120},
        )

    def test_without_a_budget_no_budget_is_sent(self) -> None:
        prepared = projects.prepare_create_project(name="Nieuwbouw")
        result = projects.create_project_from_approval(prepared["approval_id"])

        self.assertEqual(self.client.create_calls, [{"name": "Nieuwbouw"}])
        self.assertIsNone(result["project"]["budget"])
        self.assertTrue(result["verification"]["fully_verified"])

    def test_a_name_in_use_is_refused_ignoring_case_and_state(self) -> None:
        for name in ("renovatie", "OUD PROJECT "):
            with self.subTest(name=name):
                with self.assertRaisesRegex(MoneybirdError, "already exists"):
                    projects.prepare_create_project(name=name)
        self.assertEqual(self.client.create_calls, [])

    def test_an_empty_name_is_refused(self) -> None:
        with self.assertRaisesRegex(MoneybirdError, "name is required"):
            projects.prepare_create_project(name="  ")

    def test_a_name_taken_after_preview_sends_nothing(self) -> None:
        prepared = projects.prepare_create_project(name="Nieuwbouw")
        self.client.projects["71500"] = {
            "id": "71500", "name": "nieuwbouw", "state": "active", "budget": None
        }
        result = projects.create_project_from_approval(prepared["approval_id"])

        self.assertEqual(result["status"], "precondition_failed")
        self.assertEqual(self.client.create_calls, [])
        self.assertEqual(result["conflicting_projects"][0]["id"], "71500")

    def test_a_budget_that_did_not_land_fails_verification(self) -> None:
        self.client.ignore_budget = True
        prepared = projects.prepare_create_project(name="Nieuwbouw", budget_hours=8)
        result = projects.create_project_from_approval(prepared["approval_id"])

        self.assertEqual(result["status"], "completed_with_verification_errors")
        self.assertEqual(
            result["verification"]["field_mismatches"],
            {"budget": {"expected": 8, "actual": None}},
        )

    def test_the_approval_is_single_use(self) -> None:
        prepared = projects.prepare_create_project(name="Nieuwbouw")
        projects.create_project_from_approval(prepared["approval_id"])
        with self.assertRaises(MoneybirdError):
            projects.create_project_from_approval(prepared["approval_id"])
        self.assertEqual(len(self.client.create_calls), 1)

    def test_read_only_mode_refuses_and_leaves_the_approval_usable(self) -> None:
        prepared = projects.prepare_create_project(name="Nieuwbouw")
        with mock.patch.dict(os.environ, {"MONEYBIRD_CAPABILITY_MODE": "read_only"}):
            with self.assertRaisesRegex(MoneybirdError, "writes are disabled"):
                projects.create_project_from_approval(prepared["approval_id"])
        self.assertEqual(self.client.create_calls, [])

        result = projects.create_project_from_approval(prepared["approval_id"])
        self.assertEqual(result["status"], "created")


class UpdateProjectTests(_IsolatedWriteKernel):
    def test_a_rename_by_name_sends_only_the_name(self) -> None:
        prepared = projects.prepare_update_project(
            project_name="Renovatie", name="Renovatie dak"
        )
        self.assertEqual(prepared["preview"]["before"]["name"], "Renovatie")
        self.assertEqual(prepared["preview"]["after"]["name"], "Renovatie dak")
        self.assertEqual(self.client.update_calls, [])

        result = projects.update_project_from_approval(prepared["approval_id"])

        self.assertEqual(self.client.update_calls, [("71001", {"name": "Renovatie dak"})])
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["project"]["budget"], 40)
        self.assertTrue(result["verification"]["fully_verified"])

    def test_a_budget_change_always_resends_the_name(self) -> None:
        prepared = projects.prepare_update_project(project_id="71001", budget_hours=60)
        result = projects.update_project_from_approval(prepared["approval_id"])

        self.assertEqual(
            self.client.update_calls, [("71001", {"name": "Renovatie", "budget": 60})]
        )
        self.assertEqual(result["project"]["budget"], 60)
        self.assertTrue(result["verification"]["fully_verified"])

    def test_clear_budget_sends_none(self) -> None:
        prepared = projects.prepare_update_project(project_id="71001", clear_budget=True)
        result = projects.update_project_from_approval(prepared["approval_id"])

        self.assertEqual(
            self.client.update_calls, [("71001", {"name": "Renovatie", "budget": None})]
        )
        self.assertIsNone(result["project"]["budget"])
        self.assertTrue(result["verification"]["fully_verified"])

    def test_a_case_change_of_its_own_name_is_allowed(self) -> None:
        prepared = projects.prepare_update_project(project_id="71001", name="RENOVATIE")
        self.assertEqual(prepared["payload"]["project"], {"name": "RENOVATIE"})

    def test_refusals(self) -> None:
        cases = [
            ({"project_id": "71001", "budget_hours": 5, "clear_budget": True}, "not both"),
            ({"project_id": "71001", "budget_hours": 40}, "Nothing to change"),
            ({"project_id": "71003", "clear_budget": True}, "Nothing to change"),
            ({"project_id": "71001", "name": "Renovatie"}, "Nothing to change"),
            ({"project_id": "71001", "name": "onderhoud"}, "already exists"),
            ({"project_id": "71001", "name": "Oud Project"}, "already exists"),
            ({"name": "X"}, "exactly one of project_id or project_name"),
            ({"project_id": "71001", "project_name": "Renovatie", "name": "X"}, "exactly one"),
            ({"project_name": "Bestaat niet", "name": "X"}, "got 0"),
        ]
        for kwargs, message in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(MoneybirdError, message):
                    projects.prepare_update_project(**kwargs)
        self.assertEqual(self.client.update_calls, [])

    def test_a_stale_preview_aborts_before_any_write(self) -> None:
        prepared = projects.prepare_update_project(project_id="71001", budget_hours=60)
        self.client.projects["71001"]["budget"] = 50
        result = projects.update_project_from_approval(prepared["approval_id"])

        self.assertEqual(result["status"], "precondition_failed")
        self.assertEqual(self.client.update_calls, [])

    def test_a_budget_that_did_not_land_fails_verification(self) -> None:
        self.client.ignore_budget = True
        prepared = projects.prepare_update_project(project_id="71001", budget_hours=60)
        result = projects.update_project_from_approval(prepared["approval_id"])

        self.assertEqual(result["status"], "verification_failed")
        self.assertEqual(
            result["verification"]["field_mismatches"],
            {"budget": {"expected": 60, "actual": 40}},
        )


class DeleteProjectTests(_IsolatedWriteKernel):
    def test_preview_explains_delete_or_archive(self) -> None:
        prepared = projects.prepare_delete_project(project_name="Renovatie")

        self.assertEqual(self.client.delete_calls, [])
        self.assertEqual(prepared["preview"]["project"]["id"], "71001")
        self.assertIn("archives", prepared["preview"]["outcome"])

    def test_an_unlinked_project_is_deleted_and_proven_gone(self) -> None:
        prepared = projects.prepare_delete_project(project_id="71001")
        result = projects.delete_project_from_approval(prepared["approval_id"])

        self.assertEqual(self.client.delete_calls, ["71001"])
        self.assertEqual(result["status"], "deleted")
        self.assertIsNone(result["project"])
        self.assertTrue(result["verification"]["fully_verified"])

    def test_a_linked_project_is_archived_and_verified(self) -> None:
        self.client.archive_on_delete = True
        prepared = projects.prepare_delete_project(project_id="71001")
        result = projects.delete_project_from_approval(prepared["approval_id"])

        self.assertEqual(result["status"], "archived")
        self.assertEqual(result["project"]["state"], "archived")
        self.assertTrue(result["verification"]["fully_verified"])

    def test_an_archive_that_changed_anything_else_fails_verification(self) -> None:
        self.client.archive_on_delete = True
        self.client.rename_on_archive = True
        prepared = projects.prepare_delete_project(project_id="71001")
        result = projects.delete_project_from_approval(prepared["approval_id"])

        self.assertEqual(result["status"], "verification_failed")
        self.assertFalse(result["verification"]["fully_verified"])

    def test_an_archived_project_is_refused(self) -> None:
        with self.assertRaisesRegex(MoneybirdError, "is archived"):
            projects.prepare_delete_project(project_id="71002")

    def test_an_unknown_project_id_is_reported(self) -> None:
        with self.assertRaises(MoneybirdHTTPError):
            projects.prepare_delete_project(project_id="79999")

    def test_a_stale_preview_aborts_before_any_write(self) -> None:
        prepared = projects.prepare_delete_project(project_id="71001")
        self.client.projects["71001"]["name"] = "Renamed elsewhere"
        result = projects.delete_project_from_approval(prepared["approval_id"])

        self.assertEqual(result["status"], "precondition_failed")
        self.assertEqual(self.client.delete_calls, [])


class BudgetNormalisationTests(unittest.TestCase):
    def test_whole_budgets_become_integers_and_others_stay_visible(self) -> None:
        occurrence = projects._project_occurrence
        self.assertEqual(occurrence({"budget": "12.0"})["budget"], 12)
        self.assertEqual(occurrence({"budget": 12})["budget"], 12)
        self.assertEqual(occurrence({"budget": "12.5"})["budget"], "12.5")
        self.assertIsNone(occurrence({"budget": None})["budget"])


if __name__ == "__main__":
    unittest.main()
