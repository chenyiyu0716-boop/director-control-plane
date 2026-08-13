import hashlib
import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from ..adapters.feishu_cards import build_escalation_card


ESCALATION_FILE = re.compile(r"^escalation-(?:[A-Za-z0-9_-]+)-\d{8}T\d{6}Z\.json$")
TERMINAL_STATUSES = {"APPROVED", "DENIED", "APPLIED"}
ALLOWED_REASON_CODES = {
    "HIGH_RISK_DIRECTION", "HIGH_RISK_PERMISSION", "HIGH_RISK_RELEASE",
    "HIGH_RISK_PAYMENT", "UNRECOVERABLE_EXCEPTION",
}


class EscalationDeliveryError(Exception):
    pass


class EscalationDeliveryService:
    """File-backed Owner escalation delivery with idempotent reminders and decisions."""

    def __init__(self, directory: Path, transport: Any, owner_open_id: str,
                 reminder_interval: timedelta = timedelta(hours=1), max_reminders: int = 15,
                 now: Optional[Callable[[], datetime]] = None):
        self.directory = Path(directory).resolve()
        self.transport = transport
        self.owner_open_id = owner_open_id.strip()
        self.reminder_interval = reminder_interval
        self.max_reminders = max_reminders
        self.now = now or (lambda: datetime.now(timezone.utc))
        if not self.owner_open_id:
            raise EscalationDeliveryError("one owner open_id is required")
        if max_reminders < 1 or max_reminders > 15:
            raise EscalationDeliveryError("max_reminders must be between 1 and 15")

    def poll(self) -> List[Dict[str, Any]]:
        results = []
        if not self.directory.is_dir():
            return results
        for path in sorted(self.directory.glob("escalation-*.json")):
            try:
                event = self._load(path)
                if event["status"] != "PENDING":
                    continue
                count = event.get("reminder_count", 0)
                if count >= self.max_reminders or not self._reminder_due(event):
                    continue
                escalation_id = self._identity(path, event)
                nonce = secrets.token_urlsafe(24)
                expires_at = (self.now() + timedelta(hours=24)).isoformat()
                card = build_escalation_card(event, escalation_id, nonce, expires_at)
                delivery_key = "{}-{}".format(escalation_id, count + 1)
                message_id = self.transport.send_card(self.owner_open_id, card, delivery_key)
                timestamp = self.now().astimezone(timezone.utc).isoformat()
                event.update({
                    "escalation_id": escalation_id,
                    "delivery_status": "SENT",
                    "message_id": str(message_id),
                    "delivered_at": event.get("delivered_at") or timestamp,
                    "last_reminded_at": timestamp,
                    "reminder_count": count + 1,
                    "last_delivery_key": delivery_key,
                })
                event.pop("last_delivery_error", None)
                self._write(path, event)
                results.append({"escalation_id": escalation_id, "status": "sent",
                                "reminder_count": count + 1})
            except Exception as error:
                results.append({"file": path.name, "status": "failed",
                                "error_type": type(error).__name__, "error": str(error)})
        return results

    def apply_decision(self, escalation_id: str, action: str, operator_id: str,
                       reason: str = "", parameters: str = "") -> Dict[str, Any]:
        if action not in {"approve", "deny", "later"}:
            raise EscalationDeliveryError("unsupported escalation decision")
        path, event = self._find(escalation_id)
        if event["status"] in TERMINAL_STATUSES:
            return {"escalation_id": escalation_id, "status": event["status"], "duplicate": True}
        if event["status"] != "PENDING":
            raise EscalationDeliveryError("escalation is not pending")
        timestamp = self.now().astimezone(timezone.utc).isoformat()
        decision = {
            "action": action, "operator_id": operator_id, "reason": reason.strip(),
            "parameters": parameters.strip(), "decided_at": timestamp,
        }
        if action == "later":
            event["last_owner_response"] = decision
            event["last_reminded_at"] = timestamp
        else:
            event["status"] = "APPROVED" if action == "approve" else "DENIED"
            event["decision"] = decision
            event["delivery_status"] = "DECIDED"
        self._write(path, event)
        return {"escalation_id": escalation_id, "status": event["status"], "duplicate": False}

    def _find(self, escalation_id: str):
        matches = []
        for path in sorted(self.directory.glob("escalation-*.json")):
            event = self._load(path)
            if self._identity(path, event) == escalation_id:
                matches.append((path, event))
        if len(matches) != 1:
            raise EscalationDeliveryError("escalation identity is missing or ambiguous")
        return matches[0]

    def _load(self, path: Path) -> Dict[str, Any]:
        if path.parent.resolve() != self.directory or not ESCALATION_FILE.fullmatch(path.name):
            raise EscalationDeliveryError("invalid escalation filename")
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise EscalationDeliveryError("invalid escalation JSON") from error
        required = {"schema_version", "created_at", "executor_id", "task_id", "lease_id",
                    "reason_code", "summary", "decision_needed", "status"}
        missing = sorted(required - set(event))
        if missing:
            raise EscalationDeliveryError("missing escalation fields: {}".format(", ".join(missing)))
        if event["schema_version"] != "1.0" or event["reason_code"] not in ALLOWED_REASON_CODES:
            raise EscalationDeliveryError("unsupported escalation schema or reason")
        if event["status"] not in {"PENDING", "APPROVED", "DENIED", "APPLIED"}:
            raise EscalationDeliveryError("invalid escalation status")
        if not isinstance(event["summary"], str) or not event["summary"].strip():
            raise EscalationDeliveryError("escalation summary is required")
        count = event.get("reminder_count", 0)
        if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= 15:
            raise EscalationDeliveryError("invalid reminder_count")
        return event

    def _identity(self, path: Path, event: Dict[str, Any]) -> str:
        stable = {key: event.get(key) for key in (
            "schema_version", "created_at", "executor_id", "task_id", "lease_id",
            "reason_code", "summary", "decision_needed",
        )}
        material = path.name + "\n" + json.dumps(stable, ensure_ascii=False, sort_keys=True,
                                                  separators=(",", ":"))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _reminder_due(self, event: Dict[str, Any]) -> bool:
        value = event.get("last_reminded_at")
        if not value:
            return True
        try:
            previous = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError) as error:
            raise EscalationDeliveryError("invalid last_reminded_at") from error
        if previous.tzinfo is None:
            raise EscalationDeliveryError("last_reminded_at must include timezone")
        return self.now().astimezone(timezone.utc) - previous.astimezone(timezone.utc) >= self.reminder_interval

    @staticmethod
    def _write(path: Path, event: Dict[str, Any]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".escalation-", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(event, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
