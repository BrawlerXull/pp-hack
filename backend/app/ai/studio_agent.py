"""AG Studio's AI assistant on Gemini.

AG Studio's Studio Agent Framework calls an LLM adapter once per turn with an `AgLlmRequest` (conversation
items, instructions, tool JSON schemas, tool choice, response format). The frontend adapter posts that request
to POST /studio/agent/turn, and this module turns it into one Gemini `generate_content` call and the reply
back into AG Studio output items (`AgLlmResponse`). The Gemini key never reaches the browser.

The agents only read: their tools query the dashboard's own data, build widgets and read Checkout Guard's
records. Nothing here can change the guard, move money or reach PayPal.
"""

import asyncio
import json
import time
import uuid
from collections import OrderedDict
from typing import Any

import structlog
from google.genai import types

from app.ai import gemini
from app.config import get_settings

log = structlog.get_logger()

MAX_OUTPUT_TOKENS = 8192
TURN_TIMEOUT_S = 60.0  # dashboard-building turns are longer than a verdict

# Gemini 3 models sign their function calls and expect the signature back with the call in the next turn.
# AG Studio keeps the conversation, not our signatures, so we remember them per call id (one API process).
# A call we no longer remember gets the documented placeholder that skips validation.
_SIGNATURES: OrderedDict[str, bytes] = OrderedDict()
_SIGNATURE_CAP = 2000
SKIP_SIGNATURE = b"skip_thought_signature_validator"


class TurnError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _remember(call_id: str, signature: bytes) -> None:
    _SIGNATURES[call_id] = signature
    _SIGNATURES.move_to_end(call_id)
    while len(_SIGNATURES) > _SIGNATURE_CAP:
        _SIGNATURES.popitem(last=False)


def _text_of(item: dict[str, Any]) -> str:
    parts = []
    for c in item.get("content") or []:
        if c.get("type") == "text" and c.get("text"):
            parts.append(c["text"])
        elif c.get("type") == "refusal" and c.get("refusal"):
            parts.append(c["refusal"])
    return "\n".join(parts)


def clean_schema(schema: Any) -> Any:
    """AG Studio emits JSON Schema 2020-12 from its Shape library; drop what Gemini rejects: `$schema`, and
    the `{"not": {}}` sentinel Shape uses for "may be undefined" inside anyOf (`required` has optionality)."""
    if isinstance(schema, list):
        return [clean_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: clean_schema(v) for k, v in schema.items() if k != "$schema"}
    if isinstance(out.get("anyOf"), list):
        branches = [b for b in out["anyOf"] if b != {"not": {}}]
        if len(branches) == 1 and isinstance(branches[0], dict):
            rest = {k: v for k, v in out.items() if k != "anyOf"}
            return {**branches[0], **rest}
        out["anyOf"] = branches
    return out


def break_ref_loops(schema: Any) -> Any:
    """Gemini rejects a `$ref` cycle unless it passes through an optional property, and some models
    (flash-lite) apply that strictly. AG Studio's query schema has two: an expression nests expressions,
    and a filter group nests filters. Each reference that closes a cycle becomes an open schema (any
    value); AG Studio validates the tool's arguments itself, so nothing reaches the dashboard unchecked."""
    if not isinstance(schema, dict):
        return schema
    defs_key = "$defs" if "$defs" in schema else "definitions" if "definitions" in schema else None
    defs = schema.get(defs_key) if defs_key else None
    if not isinstance(defs, dict):
        return schema
    prefix = f"#/{defs_key}/"

    def refs(node: Any) -> set[str]:
        if isinstance(node, dict):
            found = {node["$ref"][len(prefix) :]} if str(node.get("$ref", "")).startswith(prefix) else set()
            return found.union(*(refs(v) for k, v in node.items() if k != "$ref"))
        if isinstance(node, list):
            return set().union(*(refs(v) for v in node))
        return set()

    # Depth-first over the definitions; an edge into a definition still on the stack closes a cycle.
    back: dict[str, set[str]] = {}
    state: dict[str, int] = {}  # 1 on the stack, 2 done

    def visit(name: str) -> None:
        state[name] = 1
        for target in sorted(refs(defs[name])):
            if target not in defs:
                continue
            if state.get(target) == 1:
                back.setdefault(name, set()).add(target)
            elif target not in state:
                visit(target)
        state[name] = 2

    for name in defs:
        if name not in state:
            visit(name)
    if not back:
        return schema

    def cut(node: Any, targets: set[str]) -> Any:
        if isinstance(node, dict):
            ref = str(node.get("$ref", ""))
            if ref.startswith(prefix) and ref[len(prefix) :] in targets:
                return {"description": f"A nested {ref[len(prefix) :]} (same shape)"}
            return {k: cut(v, targets) for k, v in node.items()}
        if isinstance(node, list):
            return [cut(v, targets) for v in node]
        return node

    new_defs = {name: cut(body, back[name]) if name in back else body for name, body in defs.items()}
    return {**schema, defs_key: new_defs}


def to_contents(items: list[dict[str, Any]]) -> list[types.Content]:
    """AG Studio conversation items → Gemini contents. Consecutive items of one role share a turn, so a
    model's parallel function calls and the user's matching function responses each stay together."""
    contents: list[types.Content] = []
    names: dict[str, str] = {}

    def add(role: str, part: types.Part) -> None:
        if contents and contents[-1].role == role:
            contents[-1].parts = [*(contents[-1].parts or []), part]
        else:
            contents.append(types.Content(role=role, parts=[part]))

    for item in items:
        kind = item.get("type")
        if kind == "message":
            text = _text_of(item)
            if text:
                add("model" if item.get("role") == "assistant" else "user", types.Part(text=text))
        elif kind == "function_call":
            call_id = item.get("callId") or item.get("id") or ""
            names[call_id] = item.get("name", "")
            try:
                args = json.loads(item.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            first_call = not (
                contents
                and contents[-1].role == "model"
                and any(p.function_call for p in contents[-1].parts or [])
            )
            signature = _SIGNATURES.get(call_id) or (SKIP_SIGNATURE if first_call else None)
            add(
                "model",
                types.Part(
                    function_call=types.FunctionCall(
                        id=call_id,
                        name=item.get("name", ""),
                        args=args if isinstance(args, dict) else {"value": args},
                    ),
                    thought_signature=signature,
                ),
            )
        elif kind == "function_call_output":
            call_id = item.get("callId", "")
            output = item.get("output", "")
            try:
                parsed = json.loads(output)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            response = parsed if isinstance(parsed, dict) else {"output": output}
            # Studio wraps a tool's result as {"success": true, "response": "<the tool's text>"}; our tools
            # answer in JSON, so hand Gemini the object rather than a string of escaped JSON.
            if isinstance(response.get("response"), str):
                try:
                    response = {**response, "response": json.loads(response["response"])}
                except json.JSONDecodeError:
                    pass
            add(
                "user",
                types.Part(
                    function_response=types.FunctionResponse(
                        id=call_id, name=names.get(call_id, "tool"), response=response
                    )
                ),
            )
        # Reasoning items are not replayed: Gemini carries its own reasoning through the call signatures.
    return contents


def to_tools(tools: list[dict[str, Any]] | None) -> list[types.Tool] | None:
    declarations = [
        types.FunctionDeclaration(
            name=t["name"],
            description=t.get("description") or "",
            parameters_json_schema=break_ref_loops(
                clean_schema(t.get("parameters") or {"type": "object", "properties": {}})
            ),
        )
        for t in tools or []
    ]
    return [types.Tool(function_declarations=declarations)] if declarations else None


def to_tool_config(choice: Any, has_tools: bool) -> types.ToolConfig | None:
    if not has_tools or choice in (None, "auto"):
        return None
    if choice == "none":
        return types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode=types.FunctionCallingConfigMode.NONE)
        )
    if choice == "required":
        return types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode=types.FunctionCallingConfigMode.ANY)
        )
    if isinstance(choice, dict) and choice.get("name"):
        return types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(
                mode=types.FunctionCallingConfigMode.ANY, allowed_function_names=[choice["name"]]
            )
        )
    return None


def build_config(request: dict[str, Any], model: str) -> types.GenerateContentConfig:
    tools = to_tools(request.get("tools"))
    fmt = request.get("responseFormat") or {"type": "text"}
    extra: dict[str, Any] = {}
    if fmt.get("type") == "json" and fmt.get("schema"):
        extra = {
            "response_mime_type": "application/json",
            "response_json_schema": clean_schema(fmt["schema"]),
        }
    return types.GenerateContentConfig(
        system_instruction=request.get("instructions") or None,
        tools=tools,
        tool_config=to_tool_config(request.get("toolChoice"), bool(tools)),
        temperature=0.2,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        thinking_config=gemini._thinking(model),
        **extra,
    )


def from_response(res: Any, model: str) -> dict[str, Any]:
    """Gemini reply → AgLlmResponse: text becomes an assistant message, function calls become tool calls.
    Thought parts stay on the server: the owner sees the agent's answer and its tool calls."""
    candidate = res.candidates[0] if getattr(res, "candidates", None) else None
    parts = (
        candidate.content.parts if candidate is not None and candidate.content is not None else None
    ) or []
    output: list[dict[str, Any]] = []
    text: list[str] = []

    def flush_text() -> None:
        if text:
            output.append(
                {
                    "id": f"msg_{uuid.uuid4().hex[:12]}",
                    "kind": "output",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "text", "text": "".join(text), "annotations": []}],
                }
            )
            text.clear()

    for part in parts:
        if getattr(part, "thought", False):
            continue
        call = getattr(part, "function_call", None)
        if call is not None:
            flush_text()
            call_id = call.id or f"call_{uuid.uuid4().hex[:12]}"
            if getattr(part, "thought_signature", None):
                _remember(call_id, part.thought_signature)
            output.append(
                {
                    "id": f"fc_{uuid.uuid4().hex[:12]}",
                    "kind": "output",
                    "type": "function_call",
                    "callId": call_id,
                    "name": call.name,
                    "arguments": json.dumps(call.args or {}),
                    "status": "completed",
                }
            )
        elif getattr(part, "text", None):
            text.append(part.text)
    flush_text()

    finish = str(getattr(candidate, "finish_reason", "") or "")
    response: dict[str, Any] = {
        "id": getattr(res, "response_id", None) or f"resp_{uuid.uuid4().hex[:12]}",
        "createdAt": int(time.time()),
        "output": output,
        "status": "incomplete" if finish.endswith("MAX_TOKENS") else "completed",
        "model": getattr(res, "model_version", None) or model,
    }
    if response["status"] == "incomplete":
        response["incompleteDetails"] = {"reason": "max_output_tokens"}
    usage = getattr(res, "usage_metadata", None)
    if usage is not None:
        response["usage"] = {
            "inputTokens": usage.prompt_token_count or 0,
            "outputTokens": usage.candidates_token_count or 0,
            "totalTokens": usage.total_token_count or 0,
            "reasoningTokens": getattr(usage, "thoughts_token_count", None) or 0,
        }
    if not output:
        raise TurnError(
            502, "empty_reply", f"Gemini returned no answer (finish reason: {finish or 'unknown'})"
        )
    return response


def _retryable(exc: Exception) -> bool:
    text = str(exc)
    return isinstance(exc, TimeoutError) or any(
        code in text for code in ("429", "500", "502", "503", "504", "RESOURCE_EXHAUSTED", "UNAVAILABLE")
    )


async def run_turn(request: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    if not settings.ai_enabled:
        raise TurnError(
            503, "ai_disabled", "The AI assistant is switched off on this server (AI_ENABLED=false)."
        )
    if not settings.gemini_api_key:
        raise TurnError(503, "ai_not_configured", "The AI assistant needs GEMINI_API_KEY on the server.")
    items = request.get("input")
    if not isinstance(items, list) or not items:
        raise TurnError(400, "bad_request", "input must be a non-empty list of conversation items")

    chain = gemini.models(settings)
    wanted = (request.get("model") or {}).get("id")
    order = [wanted, *[m for m in chain if m != wanted]] if wanted in chain else chain
    contents = to_contents(items)
    last: Exception | None = None
    # The whole chain on the first key, then again on the backup key (GEMINI_API_KEY_2) if one is set.
    for n, (key, _breaker) in enumerate(gemini.lanes(settings)):
        client = gemini.client_for(key)
        for model in order:
            try:
                res = await asyncio.wait_for(
                    client.aio.models.generate_content(
                        model=model, contents=contents, config=build_config(request, model)
                    ),
                    timeout=TURN_TIMEOUT_S,
                )
                return from_response(res, model)
            except TurnError:
                raise
            except Exception as exc:  # every provider failure is reported to the chat
                last = exc
                log.warning(
                    "studio_agent_turn_failed",
                    model=model,
                    backup_key=n > 0,
                    error=f"{type(exc).__name__}: {str(exc)[:200]}",
                )
                if not _retryable(exc):
                    break
        if last is not None and not _retryable(last):
            break
    raise TurnError(
        502, "ai_failed", f"Gemini could not complete this step: {type(last).__name__}: {str(last)[:160]}"
    )
