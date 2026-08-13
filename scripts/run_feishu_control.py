#!/usr/bin/env python3
"""Owner-only Feishu long-connection entrypoint; credentials stay in environment variables."""

import json
import os
import threading
import time
from pathlib import Path

import lark_oapi as lark
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from control_plane.adapters.feishu import event_to_dict, normalize_card_action
from control_plane.adapters.feishu_cards import build_callback_status_card
from control_plane.config import load_settings
from control_plane.services import FeishuControlInbox
from control_plane.services import EscalationDeliveryService
from control_plane.adapters.feishu_transport import FeishuMessageTransport
from control_plane.storage import Repository


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit("Missing required environment variable: {}".format(name))
    return value


def main() -> None:
    app_id = required_env("FEISHU_APP_ID")
    app_secret = required_env("FEISHU_APP_SECRET")
    project_config = required_env("CONTROL_PLANE_CONFIG")
    control_config_path = Path(required_env("CONTROL_PLANE_FEISHU_CONFIG"))
    control_config = json.loads(control_config_path.read_text(encoding="utf-8"))
    settings = load_settings(project_config)
    repository = Repository(settings.database)
    repository.migrate()
    for project in settings.projects:
        repository.upsert_project(project)
    owner_open_ids = control_config.get("ownerOpenIds", [])
    if len(owner_open_ids) != 1:
        raise SystemExit("Exactly one Owner open_id is required for escalation delivery")
    escalation_directory = Path(control_config.get("escalationDirectory", ".workbuddy")).expanduser().resolve()
    escalation_delivery = EscalationDeliveryService(
        escalation_directory, FeishuMessageTransport(app_id, app_secret), owner_open_ids[0],
    )
    inbox = FeishuControlInbox(repository, owner_open_ids, escalation_delivery)

    stop = threading.Event()

    def worker() -> None:
        next_escalation_poll = 0.0
        while not stop.wait(0.25):
            inbox.process_pending(limit=20)
            if time.monotonic() >= next_escalation_poll:
                for result in escalation_delivery.poll():
                    print(
                        "Feishu escalation delivery: status={} event={}".format(
                            result.get("status"), result.get("escalation_id") or result.get("file")
                        ),
                        flush=True,
                    )
                next_escalation_poll = time.monotonic() + 60

    threading.Thread(target=worker, name="feishu-control-worker", daemon=True).start()

    def handle_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        stage = "normalize"
        try:
            normalized = normalize_card_action(event_to_dict(data, lark))
            print(
                "Feishu callback received: event_id={} operator={} command={}".format(
                    normalized["event_id"], normalized["operator_id"],
                    normalized["payload"].get("command", "unknown"),
                ),
                flush=True,
            )
            stage = "inbox"
            acknowledgement = inbox.ingest(normalized)
            if acknowledgement["accepted"]:
                stage = "process"
                processed_items = inbox.process_pending(limit=20)
                processed = next(
                    (item for item in processed_items if item["event_id"] == normalized["event_id"]),
                    {"event_id": normalized["event_id"], "status": "processed",
                     "result": {"status": acknowledgement["status"]}},
                )
                succeeded = processed["status"] == "processed"
                content = "已处理" if succeeded else "处理失败，任务保持安全等待"
                toast_type = "success" if succeeded else "error"
                response_card = build_callback_status_card(normalized["payload"], processed)
            else:
                content = "当前账号没有控制权限"
                toast_type = "error"
                response_card = None
        except Exception as error:
            print(
                "Feishu callback rejected at {}: {}".format(
                    stage, type(error).__name__,
                ),
                flush=True,
            )
            content = "请求格式无效，未执行任何操作"
            toast_type = "error"
            response_card = None
        response = {"toast": {"type": toast_type, "content": content}}
        if response_card is not None:
            response["card"] = {"type": "raw", "data": response_card}
        return P2CardActionTriggerResponse(response)

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_card_action_trigger(handle_card_action)
        .build()
    )
    client = lark.ws.Client(
        app_id, app_secret, event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )
    try:
        client.start()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
