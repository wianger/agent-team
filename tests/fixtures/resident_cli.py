"""Deterministic bidirectional CLI fixture; never contacts a model."""

import json
import sys
import uuid

backend = sys.argv[1]
session = None
turns = 0
permission = "auto"
waiting = None


def send(event):
    print(json.dumps(event), flush=True)


def result(text):
    send({"type": "result", "subtype": "success", "session_id": session, "result": text})


for raw in sys.stdin:
    event = json.loads(raw)
    if backend == "codex":
        method, params = event.get("method"), event.get("params", {})
        if method == "initialize":
            send({"id": event["id"], "result": {}})
        elif method in {"thread/start", "thread/resume"}:
            session = params.get("threadId") or str(uuid.uuid4())
            send({"id": event["id"], "result": {"thread": {"id": session}}})
        elif method == "turn/start":
            prompt = params["input"][0]["text"]
            if prompt == "exit":
                print("fixture exited before RPC completion", file=sys.stderr, flush=True)
                sys.exit(7)
            turns += 1
            turn_id = str(uuid.uuid4())
            base = {"threadId": session, "turnId": turn_id}
            send({"id": event["id"], "result": {"turn": {"id": turn_id}}})
            send({"method": "item/reasoning/textDelta", "params": {**base, "delta": "private"}})
            text = f"public {turns}"
            send(
                {
                    "method": "item/agentMessage/delta",
                    "params": {**base, "itemId": "msg", "delta": text},
                }
            )
            if prompt == "hang":
                continue
            if prompt == "malformed":
                print("not json", flush=True)
                continue
            send(
                {
                    "method": "item/completed",
                    "params": {**base, "item": {"id": "msg", "type": "agentMessage", "text": text}},
                }
            )
            send(
                {
                    "method": "turn/completed",
                    "params": {**base, "turn": {"id": turn_id, "status": "completed"}},
                }
            )
    elif event.get("type") == "control_request":
        request = event["request"]
        if request["subtype"] == "set_permission_mode":
            permission = request["mode"]
        send(
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": event["request_id"],
                    "response": {},
                },
            }
        )
    elif event.get("type") == "control_response":
        output = event["response"].get("response", {})
        decision = output.get("hookSpecificOutput", {}).get("permissionDecision", "allowed")
        result(decision)
        waiting = None
    elif event.get("type") == "user":
        session = event["session_id"]
        turns += 1
        prompt = event["message"]["content"]
        if turns == 1 or prompt == "bad-permission":
            send(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": session,
                    "permissionMode": "bypassPermissions"
                    if prompt == "bad-permission"
                    else permission,
                }
            )
        if prompt == "try-write" or prompt.startswith("try-tool:"):
            waiting = str(uuid.uuid4())
            send(
                {
                    "type": "control_request",
                    "request_id": waiting,
                    "request": {
                        "subtype": "hook_callback",
                        "callback_id": "phase_guard",
                        "input": {"tool_name": "Write" if prompt == "try-write" else prompt[9:]},
                    },
                }
            )
        elif prompt == "hang":
            send(
                {
                    "type": "stream_event",
                    "session_id": session,
                    "event": {"delta": {"type": "text_delta", "text": "working"}},
                }
            )
        elif prompt == "exit":
            sys.exit(7)
        else:
            result(f"public {turns}")
