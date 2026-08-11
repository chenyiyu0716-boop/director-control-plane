from typing import Any, Dict


def _text(content: str) -> Dict[str, str]:
    return {"tag": "plain_text", "content": content}


def _button(label: str, button_type: str, value: Dict[str, Any]) -> Dict[str, Any]:
    return {"tag": "button", "text": _text(label), "type": button_type, "value": value}


def _submit_button(label: str, button_type: str, value: Dict[str, Any]) -> Dict[str, Any]:
    return dict(_button(label, button_type, value), form_action_type="submit")


def build_decision_card(task: Dict[str, Any], nonce: str, expires_at: str) -> Dict[str, Any]:
    base = {
        "command": "task_decision", "task_id": task["id"], "task_version": task["version"],
        "nonce": nonce, "expires_at": expires_at,
    }
    return {
        "schema": "2.0",
        "header": {"title": _text("需要负责人决策 · {}".format(task["id"]))},
        "body": {"elements": [
            {"tag": "markdown", "content": "**{}**\n{}\n当前状态：{} · 优先级：{} · 风险：{}".format(
                task["title"], task["objective"], task["state"], task["priority"], task["risk_level"]
            )},
            {"tag": "form", "name": "owner_decision", "elements": [
                {"tag": "input", "name": "reason", "label": _text("决定说明"),
                 "placeholder": _text("填写批准、拒绝或要求修改的原因")},
                _submit_button("批准", "primary", dict(base, action="approve")),
                _submit_button("要求修改", "default", dict(base, action="request_changes")),
                _submit_button("拒绝", "danger", dict(base, action="reject")),
            ]},
        ]},
    }


def build_requirement_card(project_id: str, nonce: str, expires_at: str) -> Dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {"title": _text("调整项目开发方向")},
        "body": {"elements": [
            {"tag": "markdown", "content": "目标项目：**{}**\n提交后先生成影响预览，不会直接执行。".format(project_id)},
            {"tag": "form", "name": "requirement_intake", "elements": [
                {"tag": "select_static", "name": "kind", "label": _text("调整类型"),
                 "placeholder": _text("请选择"), "options": [
                     {"text": _text(label), "value": value} for value, label in [
                         ("new_requirement", "新增需求"), ("direction_change", "调整方向"),
                         ("priority_change", "调整优先级"), ("pause", "暂停推进"),
                         ("resume", "恢复推进"), ("replan", "重新规划"),
                     ]
                 ]},
                {"tag": "input", "name": "objective", "label": _text("目标与原因"),
                 "placeholder": _text("说明希望改变什么、为什么")},
                {"tag": "select_static", "name": "requested_priority", "label": _text("期望优先级（可选）"),
                 "placeholder": _text("P0–P3"), "options": [
                     {"text": _text(value), "value": value} for value in ("P0", "P1", "P2", "P3")
                 ]},
                _submit_button("生成变更预览", "primary", {
                    "command": "requirement_intake", "project_id": project_id,
                    "nonce": nonce, "expires_at": expires_at,
                }),
            ]},
        ]},
    }


def build_intake_confirmation_card(intake: Dict[str, Any], nonce: str, expires_at: str) -> Dict[str, Any]:
    preview = intake["preview"]
    affected = preview.get("affected_active_tasks", [])
    task_lines = ["- {} · {} · {}".format(item["task_id"], item["state"], item["title"]) for item in affected]
    base = {
        "command": "confirm_intake", "intake_id": intake["id"], "intake_version": intake["version"],
        "nonce": nonce, "expires_at": expires_at,
    }
    return {
        "schema": "2.0",
        "header": {"title": _text("确认开发方向变更")},
        "body": {"elements": [
            {"tag": "markdown", "content": "**目标**\n{}\n\n**受影响任务**\n{}\n\n**推荐方案**\n{}".format(
                intake["objective"], "\n".join(task_lines) if task_lines else "- 无活跃任务",
                preview["recommended_plan"]["summary"],
            )},
            _button("确认交给 Planner", "primary", dict(base, confirm=True)),
            _button("取消", "default", dict(base, confirm=False)),
        ]},
    }
