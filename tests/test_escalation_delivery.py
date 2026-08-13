import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from control_plane.services import EscalationDeliveryError, EscalationDeliveryService


class FakeTransport:
    def __init__(self):
        self.sent = []

    def send_card(self, owner_open_id, card, idempotency_key):
        self.sent.append((owner_open_id, card, idempotency_key))
        return "om_{}".format(len(self.sent))


class EscalationDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 8, 13, 4, 0, tzinfo=timezone.utc)
        self.transport = FakeTransport()
        self.service = EscalationDeliveryService(
            self.root, self.transport, "ou_owner", now=lambda: self.now,
        )

    def tearDown(self):
        self.temp.cleanup()

    def write_event(self, name="escalation-TASK-001-20260813T040000Z.json", **updates):
        event = {
            "schema_version": "1.0", "created_at": "2026-08-13T04:00:00Z",
            "executor_id": "workbuddy-hy3", "task_id": "TASK-001", "lease_id": None,
            "reason_code": "HIGH_RISK_DIRECTION", "summary": "Owner decision required",
            "decision_needed": "APPROVE", "status": "PENDING",
        }
        event.update(updates)
        path = self.root / name
        path.write_text(json.dumps(event), encoding="utf-8")
        return path

    def test_initial_delivery_is_idempotent_until_reminder_due(self):
        path = self.write_event()
        self.assertEqual(self.service.poll()[0]["status"], "sent")
        self.assertEqual(self.service.poll(), [])
        self.assertEqual(len(self.transport.sent), 1)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["reminder_count"], 1)
        self.assertEqual(len(saved["escalation_id"]), 64)
        self.assertEqual(saved["last_delivery_key"], self.transport.sent[0][2])
        self.assertEqual(len(saved["last_delivery_key"]), 32)
        self.assertNotIn("nonce", saved)

        self.now += timedelta(hours=1)
        self.service.poll()
        self.assertEqual(len(self.transport.sent), 2)
        self.assertNotEqual(self.transport.sent[0][2], self.transport.sent[1][2])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["reminder_count"], 2)

    def test_reminders_stop_at_fifteen(self):
        self.write_event(reminder_count=15, last_reminded_at="2026-08-12T00:00:00+00:00")
        self.assertEqual(self.service.poll(), [])
        self.assertEqual(self.transport.sent, [])

    def test_approve_deny_and_later_are_atomic_and_safe(self):
        path = self.write_event()
        self.service.poll()
        event = json.loads(path.read_text(encoding="utf-8"))
        result = self.service.apply_decision(
            event["escalation_id"], "later", "ou_owner", "later", "after meeting",
        )
        self.assertEqual(result["status"], "PENDING")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "PENDING")

        result = self.service.apply_decision(
            event["escalation_id"], "approve", "ou_owner", "approved", "fake tests only",
        )
        self.assertEqual(result["status"], "APPROVED")
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["decision"]["parameters"], "fake tests only")
        self.assertNotEqual(saved["status"], "APPLIED")
        self.assertTrue(self.service.apply_decision(
            event["escalation_id"], "approve", "ou_owner", "duplicate",
        )["duplicate"])

    def test_malformed_event_fails_without_delivery_or_mutation(self):
        path = self.write_event(reason_code="UNKNOWN")
        before = path.read_bytes()
        result = self.service.poll()[0]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.transport.sent, [])

    def test_transport_failure_is_persisted_without_counting_a_reminder(self):
        class FailingTransport:
            def send_card(self, owner_open_id, card, idempotency_key):
                raise RuntimeError("synthetic send failure")

        path = self.write_event()
        service = EscalationDeliveryService(
            self.root, FailingTransport(), "ou_owner", now=lambda: self.now,
        )
        result = service.poll()[0]
        self.assertEqual(result["status"], "failed")
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["delivery_status"], "FAILED")
        self.assertIn("synthetic send failure", saved["last_delivery_error"])
        self.assertEqual(saved.get("reminder_count", 0), 0)

    def test_identity_changes_cannot_target_an_event(self):
        self.write_event()
        with self.assertRaises(EscalationDeliveryError):
            self.service.apply_decision("0" * 64, "approve", "ou_owner")


if __name__ == "__main__":
    unittest.main()
