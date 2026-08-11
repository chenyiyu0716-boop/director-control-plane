#!/usr/bin/env python3
"""Send one owner-only requirement card for Feishu channel verification."""

import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from control_plane.adapters.feishu_cards import build_callback_test_card


API_BASE = "https://open.feishu.cn/open-apis"


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit("Missing required environment variable: {}".format(name))
    return value


def request_json(url: str, payload: dict, token: str = "") -> dict:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = "Bearer {}".format(token)
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise SystemExit("Feishu request failed (HTTP {}): {}".format(error.code, body))
    if result.get("code") != 0:
        raise SystemExit("Feishu request failed: code={} msg={}".format(
            result.get("code"), result.get("msg", "unknown error")
        ))
    return result


def main() -> None:
    app_id = required_env("FEISHU_APP_ID")
    app_secret = required_env("FEISHU_APP_SECRET")
    owner_open_id = required_env("FEISHU_OWNER_OPEN_ID")
    project_id = os.environ.get("FEISHU_TEST_PROJECT_ID", "panel")

    token_response = request_json(
        API_BASE + "/auth/v3/app_access_token/internal",
        {"app_id": app_id, "app_secret": app_secret},
    )
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    card = build_callback_test_card(project_id, secrets.token_urlsafe(24), expires_at)
    query = urllib.parse.urlencode({"receive_id_type": "open_id"})
    message_response = request_json(
        API_BASE + "/im/v1/messages?" + query,
        {
            "receive_id": owner_open_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        },
        token_response["app_access_token"],
    )
    message_id = ((message_response.get("data") or {}).get("message_id") or "unknown")
    print("Test card sent successfully: {}".format(message_id))


if __name__ == "__main__":
    main()
