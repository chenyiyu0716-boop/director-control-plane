import json
from typing import Any, Dict


class FeishuAdapterError(Exception):
    pass


def normalize_card_action(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce an SDK card.action.trigger object to the safe inbox contract."""
    header = raw.get("header") or {}
    event = raw.get("event") or {}
    action = event.get("action") or {}
    operator = event.get("operator") or {}
    value = action.get("value") or {}
    form_value = action.get("form_value") or {}
    if not isinstance(value, dict) or not isinstance(form_value, dict):
        raise FeishuAdapterError("card action values must be objects")
    payload = dict(value)
    payload.update(form_value)
    event_id = header.get("event_id") or raw.get("event_id")
    operator_id = operator.get("open_id") or raw.get("open_id")
    if not event_id or not operator_id:
        raise FeishuAdapterError("card action is missing event_id or operator open_id")
    return {
        "event_id": str(event_id),
        "event_type": "card.action.trigger",
        "operator_id": str(operator_id),
        "nonce": str(payload.pop("nonce", "")),
        "expires_at": str(payload.pop("expires_at", "")),
        "message_ref": event.get("context", {}).get("open_message_id"),
        "payload": payload,
    }


def event_to_dict(event: Any, lark_module: Any) -> Dict[str, Any]:
    serialized = lark_module.JSON.marshal(event)
    return json.loads(serialized) if isinstance(serialized, str) else serialized
