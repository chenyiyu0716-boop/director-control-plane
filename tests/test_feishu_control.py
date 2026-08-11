import tempfile
import time
import unittest
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from control_plane.adapters.feishu import normalize_card_action
from control_plane.adapters.feishu_cards import (
    build_callback_test_card, build_decision_card, build_intake_confirmation_card,
    build_requirement_card,
)
from control_plane.config import ProjectConfig
from control_plane.domain.models import AgentType, TaskState
from control_plane.services import FeishuControlError, FeishuControlInbox, TaskRegistry, task_from_dict
from control_plane.storage import Repository


class FeishuControlTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = Repository(self.root / "control-plane.sqlite3")
        self.repository.migrate()
        self.repository.upsert_project(ProjectConfig(
            id="panel", name="Panel", kind="control_plane", owner="Owner", root=self.root,
            ledger=self.root / "TASK_QUEUE.md", status=self.root / "CURRENT_STATE.md",
            knowledge_roots=[], enabled_agents=list(AgentType),
        ))
        self.registry = TaskRegistry(self.repository)
        self.inbox = FeishuControlInbox(self.repository, ["ou_owner"])

    def tearDown(self):
        self.temp.cleanup()

    def register_task(self, task_id="TASK-017", needs_decision=True):
        task = self.registry.register(task_from_dict({
            "id": task_id, "projectId": "panel", "title": "Controlled task",
            "objective": "Wait for owner", "scope": ["Control"], "acceptance": ["Owner confirms"],
            "priority": "P1", "riskLevel": "high", "allowedExecutors": ["codex"],
            "workspaceRoots": [str(self.root)], "dependencies": [],
        }), "test")
        if needs_decision:
            task = self.registry.transition(task_id, TaskState.NEEDS_DECISION, 1, "policy", "owner required")
        return task

    def event(self, event_id, command, payload=None, operator="ou_owner", expires=None, nonce=None):
        body = {"command": command}
        body.update(payload or {})
        return {
            "event_id": event_id,
            "event_type": "card.action.trigger",
            "operator_id": operator,
            "nonce": nonce or "nonce-{}".format(event_id),
            "expires_at": (expires or (datetime.now(timezone.utc) + timedelta(minutes=5))).isoformat(),
            "message_ref": "om_{}".format(event_id),
            "payload": body,
        }

    def test_ack_is_fast_idempotent_and_does_not_apply_inline(self):
        task = self.register_task()
        event = self.event("evt-approve", "task_decision", {
            "task_id": task["id"], "task_version": task["version"],
            "action": "approve", "reason": "范围与风险已确认",
        })
        started = time.monotonic()
        ack = self.inbox.ingest(event)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3)
        self.assertEqual(ack, {"accepted": True, "duplicate": False, "status": "pending"})
        self.assertEqual(self.repository.get_task(task["id"])["state"], "NEEDS_DECISION")
        self.assertTrue(self.inbox.ingest(event)["duplicate"])
        processed = self.inbox.process_pending()
        self.assertEqual(processed[0]["status"], "processed")
        self.assertEqual(self.repository.get_task(task["id"])["state"], "READY")
        self.assertEqual(len(self.repository.list_owner_decisions(task["id"])), 1)

    def test_only_allowlisted_owner_can_enqueue(self):
        event = self.event("evt-intruder", "requirement_intake", {
            "project_id": "panel", "kind": "new_requirement", "objective": "Do something",
        }, operator="ou_intruder")
        self.assertEqual(self.inbox.ingest(event)["status"], "unauthorized")
        self.assertEqual(self.repository.list_pending_feishu_events(), [])

    def test_locked_inbox_fails_before_callback_deadline(self):
        event = self.event("evt-locked", "requirement_intake", {
            "project_id": "panel", "kind": "new_requirement", "objective": "wait safely",
        })
        lock = sqlite3.connect(str(self.repository.database))
        lock.execute("BEGIN EXCLUSIVE")
        try:
            started = time.monotonic()
            with self.assertRaises(sqlite3.OperationalError):
                self.inbox.ingest(event)
            self.assertLess(time.monotonic() - started, 3)
        finally:
            lock.rollback()
            lock.close()
        self.assertEqual(self.repository.list_pending_feishu_events(), [])

    def test_expired_event_is_rejected_without_side_effect(self):
        task = self.register_task()
        event = self.event("evt-expired", "task_decision", {
            "task_id": task["id"], "task_version": task["version"],
            "action": "approve", "reason": "too late",
        }, expires=datetime.now(timezone.utc) - timedelta(seconds=1))
        self.inbox.ingest(event)
        result = self.inbox.process_pending()
        self.assertEqual(result[0]["status"], "rejected")
        self.assertIn("expired", result[0]["result"]["error"])
        self.assertEqual(self.repository.get_task(task["id"])["state"], "NEEDS_DECISION")
        self.assertEqual(self.repository.list_owner_decisions(task["id"]), [])

    def test_worker_recovers_if_business_write_finished_before_inbox_status(self):
        task = self.register_task()
        event = self.event("evt-recovery", "task_decision", {
            "task_id": task["id"], "task_version": task["version"],
            "action": "approve", "reason": "recover safely",
        })
        self.inbox.ingest(event)
        self.repository.apply_owner_decision(
            event["event_id"], task["id"], task["version"], "approve", "ou_owner", "recover safely",
        )
        processed = self.inbox.process_pending()
        self.assertEqual(processed[0]["status"], "processed")
        self.assertEqual(len(self.repository.list_owner_decisions(task["id"])), 1)
        self.assertEqual(self.repository.get_task(task["id"])["state"], "READY")

    def test_requirement_needs_preview_and_confirmation_never_creates_ready(self):
        active = self.register_task("TASK-ACTIVE", needs_decision=False)
        self.registry.transition(active["id"], TaskState.READY, 1, "policy", "safe")
        self.registry.transition(active["id"], TaskState.CLAIMED, 2, "codex", "claimed")
        self.registry.transition(active["id"], TaskState.RUNNING, 3, "codex", "running")
        intake_event = self.event("evt-intake", "requirement_intake", {
            "project_id": "panel", "kind": "direction_change", "objective": "先稳定再扩展",
            "requested_priority": "P0",
        })
        self.inbox.ingest(intake_event)
        result = self.inbox.process_pending()[0]["result"]
        intake = result["intake"]
        self.assertEqual(intake["status"], "PREVIEW_PENDING")
        self.assertEqual(intake["preview"]["automatic_task_changes"], [])
        self.assertEqual(intake["preview"]["affected_active_tasks"][0]["state"], "RUNNING")
        self.assertEqual(intake["preview"]["relationships"]["pause_candidates"], ["TASK-ACTIVE"])
        self.assertEqual(intake["preview"]["relationships"]["replacement_candidates"], ["TASK-ACTIVE"])
        self.assertIn("DRAFT", intake["preview"]["recommended_plan"]["summary"])
        self.assertEqual(self.repository.get_task(active["id"])["state"], "RUNNING")

        confirm_event = self.event("evt-confirm", "confirm_intake", {
            "intake_id": intake["id"], "intake_version": 1, "confirm": True,
        })
        self.inbox.ingest(confirm_event)
        confirmed = self.inbox.process_pending()[0]["result"]
        self.assertEqual(confirmed["intake"]["status"], "CONFIRMED")
        self.assertEqual(confirmed["next_action"], "planner_review")
        self.assertEqual(self.repository.get_task(active["id"])["state"], "RUNNING")
        self.assertEqual(self.repository.list_tasks("panel", "READY"), [])

    def test_unknown_fields_are_not_persisted(self):
        event = self.event("evt-secret", "requirement_intake", {
            "project_id": "panel", "kind": "new_requirement", "objective": "safe",
            "cookie": "must-not-store",
        })
        with self.assertRaises(FeishuControlError):
            self.inbox.ingest(event)
        self.assertEqual(self.repository.list_pending_feishu_events(), [])

    def test_sdk_payload_normalization_keeps_only_card_values(self):
        normalized = normalize_card_action({
            "header": {"event_id": "evt-card"},
            "event": {
                "operator": {"open_id": "ou_owner"},
                "context": {"open_message_id": "om_card"},
                "action": {
                    "value": {
                        "command": "requirement_intake", "project_id": "panel",
                        "kind": "new_requirement", "nonce": "nonce-card",
                        "expires_at": "2026-08-12T00:00:00+08:00",
                    },
                    "form_value": {"objective": "新的方向"},
                },
            },
            "raw_chat": "must-not-follow",
        })
        self.assertEqual(normalized["operator_id"], "ou_owner")
        self.assertEqual(normalized["payload"]["objective"], "新的方向")
        self.assertNotIn("raw_chat", normalized["payload"])

    def test_cards_bind_owner_actions_to_nonce_expiry_and_versions(self):
        task = self.register_task()
        decision = build_decision_card(task, "nonce-decision", "2026-08-12T00:00:00+08:00")
        decision_actions = decision["body"]["elements"][-1]["elements"][1:]
        decision_values = [item["value"] for item in decision_actions]
        self.assertEqual({value["action"] for value in decision_values}, {
            "approve", "reject", "request_changes",
        })
        self.assertTrue(all(value["task_version"] == task["version"] for value in decision_values))
        self.assertTrue(all(item["action_type"] == "form_submit" for item in decision_actions))

        requirement = build_requirement_card("panel", "nonce-intake", "2026-08-12T00:00:00+08:00")
        requirement_elements = requirement["body"]["elements"][-1]["elements"]
        submit = requirement_elements[-1]
        selects = [item for item in requirement_elements if item["tag"] == "select_static"]
        self.assertEqual(submit["value"]["command"], "requirement_intake")
        self.assertEqual(submit["action_type"], "form_submit")
        self.assertTrue(all("label" not in item for item in selects))
        self.assertTrue(all(option["text"]["tag"] == "plain_text" for item in selects for option in item["options"]))

        transport = build_callback_test_card(
            "director-agent", "nonce-transport", "2026-08-12T00:00:00+08:00",
        )
        transport_button = transport["body"]["elements"][-1]
        transport_value = transport_button["behaviors"][0]["value"]
        self.assertEqual(transport_button["behaviors"][0]["type"], "callback")
        self.assertEqual(transport_value["command"], "requirement_intake")
        self.assertEqual(transport_value["project_id"], "director-agent")

        self.inbox.ingest(self.event("evt-card-intake", "requirement_intake", {
            "project_id": "panel", "kind": "pause", "objective": "暂停当前推进",
        }))
        intake = self.inbox.process_pending()[0]["result"]["intake"]
        confirmation = build_intake_confirmation_card(
            intake, "nonce-confirm", "2026-08-12T00:00:00+08:00",
        )
        confirm_actions = confirmation["body"]["elements"][-2:]
        confirm_values = [item["behaviors"][0]["value"] for item in confirm_actions]
        self.assertEqual([value["confirm"] for value in confirm_values], [True, False])
        self.assertTrue(all(value["intake_version"] == 1 for value in confirm_values))


if __name__ == "__main__":
    unittest.main()
