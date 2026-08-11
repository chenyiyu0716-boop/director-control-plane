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
from control_plane.config import load_settings
from control_plane.services import FeishuControlInbox
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
    inbox = FeishuControlInbox(repository, control_config.get("ownerOpenIds", []))

    stop = threading.Event()

    def worker() -> None:
        while not stop.wait(0.25):
            inbox.process_pending(limit=20)

    threading.Thread(target=worker, name="feishu-control-worker", daemon=True).start()

    def handle_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        try:
            normalized = normalize_card_action(event_to_dict(data, lark))
            acknowledgement = inbox.ingest(normalized)
            if acknowledgement["accepted"]:
                content = "已接收，正在处理" if not acknowledgement["duplicate"] else "该操作已接收，请勿重复提交"
                toast_type = "info"
            else:
                content = "当前账号没有控制权限"
                toast_type = "error"
        except Exception:
            content = "请求格式无效，未执行任何操作"
            toast_type = "error"
        return P2CardActionTriggerResponse({"toast": {"type": toast_type, "content": content}})

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
