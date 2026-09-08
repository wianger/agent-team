"""Example adapter: read the complete prompt from stdin, emit JSONL on stdout."""

import json
import sys

prompt = sys.stdin.read()
reply = f"Received {len(prompt)} characters of complete context. Connect any model or agent here."
print(json.dumps({"type": "delta", "text": reply}, ensure_ascii=False), flush=True)
print(json.dumps({"type": "done"}), flush=True)
