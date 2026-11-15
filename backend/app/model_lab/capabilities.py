"""Versioned, explicit compatibility probes; reads never call providers."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit


def connection_capabilities(profile: dict) -> dict:
    from .. import settings
    saved = settings.read("model-capabilities:" + str(profile.get("id")), {})
    if saved.get("revision") != settings.model_revision(profile):
        saved = {}
    items = {name: {"state": "unknown", "label": "未验证", "message": ""} for name in ("text", "tools", "json", "web_search")}
    items.update(saved.get("items", {}))
    if settings.model_status(profile)["ready"]:
        items["text"] = {"state": "supported", "label": "已验证", "message": "已收到文本回答；不代表工具或搜索能力已验证"}
    return items


def record_capability(profile_id: str | None, revision: str, name: str, state: str, message: str, protocol: str | None = None) -> bool:
    from .. import settings, db
    from . import repository
    if not profile_id:
        return False
    with settings._LOCK:
        current = repository.get_profile(profile_id, public=False)
        if not current or settings.model_revision(current) != revision:
            return False
        saved = settings.read("model-capabilities:" + profile_id, {})
        if saved.get("revision") != revision:
            saved = {"revision": revision, "items": {}}
        saved["items"][name] = {"state": state, "label": {"supported": "已验证", "unsupported": "不支持", "unknown": "未确认"}[state], "message": message, "validated_at": db.now_iso(), **({"protocol": protocol} if protocol else {})}
        settings.write("model-capabilities:" + profile_id, saved)
    return True


async def probe_capability(profile: dict, name: str) -> dict:
    from .. import llm, settings
    protocol = "ark" if (urlsplit(profile.get("base_url", "")).hostname or "").endswith(".volces.com") else "responses"
    try:
        with llm.model_profile_context(profile):
            client = llm.get_client()
            common = {"model": profile["model_id"], "max_tokens": min(2048, int(profile.get("max_output_tokens") or 2048)), **llm._request_options()}
            if name == "tools":
                tools = [{"type": "function", "function": {"name": "connection_echo", "description": "Return an OK acknowledgement", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}]
                messages = [{"role": "user", "content": "Call connection_echo exactly once, then acknowledge its result with OK."}]
                first = await asyncio.wait_for(client.chat.completions.create(**common, messages=messages, tools=tools), 25)
                reply = first.choices[0].message
                calls = reply.tool_calls or []
                if len(calls) != 1 or calls[0].function.name != "connection_echo" or json.loads(calls[0].function.arguments or "{}") != {}:
                    raise ValueError("未收到有效工具调用")
                assistant = {"role": "assistant", "content": reply.content, "tool_calls": [call.model_dump(exclude_none=True) for call in calls]}
                if getattr(reply, "reasoning_content", None):
                    assistant["reasoning_content"] = reply.reasoning_content
                messages += [assistant, {"role": "tool", "tool_call_id": calls[0].id, "content": '{"ok":true}'}]
                final = await asyncio.wait_for(client.chat.completions.create(**common, messages=messages, tools=tools), 25)
                if not (final.choices[0].message.content or "").strip() or final.choices[0].finish_reason == "length" or getattr(final.choices[0].message, "tool_calls", None):
                    raise ValueError("工具结果续答未完成")
            elif name == "json":
                result = await asyncio.wait_for(client.chat.completions.create(**common, messages=[{"role": "user", "content": 'Return only the json object {"ok":true}.'}], response_format={"type": "json_object"}), 25)
                if json.loads(result.choices[0].message.content or "") != {"ok": True}:
                    raise ValueError("未返回有效 JSON 对象")
            else:
                tool = {"type": "web_search", **({"max_keyword": 1} if protocol == "ark" else {})}
                result = await asyncio.wait_for(client.responses.create(model=profile["model_id"], input="Search for the official Python documentation URL and return it briefly.", tools=[tool], max_output_tokens=min(2048, int(profile.get("max_output_tokens") or 2048))), 25)
                if not llm._response_output_text(result):
                    raise ValueError("未收到搜索正文")
                if not any(getattr(item, "type", None) == "web_search_call" for item in getattr(result, "output", [])):
                    raise ValueError("未确认服务实际执行了原生搜索")
        state, message = "supported", "能力验证成功"
    except Exception as exc:
        code = llm._http_status_code(exc)
        state = "unsupported" if code in {400, 404, 405, 422, 501} else "unknown"
        message = "接口不支持此能力或请求格式" if state == "unsupported" else "验证未完成：" + llm.friendly_runtime_error(exc)[:250]
    accepted = record_capability(profile["id"], settings.model_revision(profile), name, state, message, protocol if name == "web_search" else None)
    return {"ok": accepted and state == "supported", "state": state, "message": message if accepted else "配置已修改，请重新验证"}
