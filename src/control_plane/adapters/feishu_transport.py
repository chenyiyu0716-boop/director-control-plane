import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict


class FeishuTransportError(Exception):
    pass


class FeishuMessageTransport:
    API_BASE = "https://open.feishu.cn/open-apis"

    def __init__(self, app_id: str, app_secret: str, timeout: int = 15):
        self.app_id = app_id
        self.app_secret = app_secret
        self.timeout = timeout

    def send_card(self, owner_open_id: str, card: Dict[str, Any], idempotency_key: str) -> str:
        token = self._request(
            self.API_BASE + "/auth/v3/app_access_token/internal",
            {"app_id": self.app_id, "app_secret": self.app_secret},
        )["app_access_token"]
        query = urllib.parse.urlencode({"receive_id_type": "open_id"})
        result = self._request(
            self.API_BASE + "/im/v1/messages?" + query,
            {"receive_id": owner_open_id, "msg_type": "interactive",
             "content": json.dumps(card, ensure_ascii=False), "uuid": idempotency_key}, token,
        )
        message_id = (result.get("data") or {}).get("message_id")
        if not message_id:
            raise FeishuTransportError("Feishu response is missing message_id")
        return str(message_id)

    def _request(self, url: str, payload: Dict[str, Any], token: str = "") -> Dict[str, Any]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = "Bearer {}".format(token)
        request = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, ValueError) as error:
            raise FeishuTransportError("Feishu request failed") from error
        if result.get("code") != 0:
            raise FeishuTransportError("Feishu request rejected: code={}".format(result.get("code")))
        return result
