"""Opt-in live smoke test: two turns per backend, no workspace tool calls."""

from __future__ import annotations

import argparse
import asyncio
import tempfile
import uuid
from pathlib import Path

from agent_team.config import AgentConfig
from agent_team.resident import make_resident


async def check(backend, permission_mode="full_auto", check_write_guard=False):
    with tempfile.TemporaryDirectory(prefix="agent-team-resident-smoke-") as directory:
        adapter = make_resident(
            AgentConfig(backend, backend), Path(directory), permission_mode=permission_mode
        )
        token = "memory-" + uuid.uuid4().hex
        print(f"{backend}: starting native resident connection", flush=True)
        try:
            first = "".join(
                [
                    part
                    async for part in adapter.stream(
                        "This is a transport smoke test. Do not use tools or modify files. "
                        f"Remember this token: {token}. Reply only FIRST_OK.",
                        phase="planning",
                        persist_session=True,
                    )
                ]
            )
            assert "FIRST_OK" in first, f"{backend}: unexpected first reply: {first}"
            identifier, pid = adapter.result_session_id, adapter.process.pid
            print(
                f"{backend}: first turn completed; testing same process and private memory",
                flush=True,
            )
            second = "".join(
                [
                    part
                    async for part in adapter.stream(
                        "Do not use tools or modify files. "
                        "Reply with only the token I asked you to remember.",
                        phase="implementation",
                        persist_session=True,
                        session_id=identifier,
                    )
                ]
            )
            assert token in second, f"{backend}: private memory not recalled: {second}"
            assert adapter.result_session_id == identifier
            assert adapter.process.pid == pid and adapter.process.returncode is None
            print(f"{backend}: PASS — same live PID and session across both turns", flush=True)
            if check_write_guard and backend == "claude":
                attempts = []
                route = adapter.route

                async def observe(event):
                    request = event.get("request", {})
                    if request.get("subtype") == "hook_callback":
                        attempts.append(request.get("input", {}).get("tool_name"))
                    await route(event)

                adapter.route = observe
                print("claude: checking the native nonwriter tool guard", flush=True)
                _ = [
                    part
                    async for part in adapter.stream(
                        "Integration test: use the Write tool once to create guard-probe.txt "
                        "containing probe. If the tool is denied, report that and stop. "
                        "Do not use a different tool or try to bypass the denial.",
                        phase="judging",
                        persist_session=True,
                        session_id=identifier,
                    )
                ]
                assert "Write" in attempts, "Claude did not exercise the Write hook"
                assert not (Path(directory) / "guard-probe.txt").exists(), "Nonwriter wrote a file"
                assert adapter.process.pid == pid
                print("claude: PASS — native Write hook denied the nonwriter", flush=True)
        finally:
            await adapter.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["codex", "claude", "both"], default="both")
    parser.add_argument(
        "--permission-mode", choices=["full_auto", "phase_scoped"], default="full_auto"
    )
    parser.add_argument(
        "--check-write-guard",
        action="store_true",
        help="Also test a denied Claude Write call (requires --permission-mode phase_scoped)",
    )
    args = parser.parse_args()
    if args.check_write_guard and args.permission_mode != "phase_scoped":
        parser.error("--check-write-guard requires --permission-mode phase_scoped")
    backends = ["codex", "claude"] if args.backend == "both" else [args.backend]
    await asyncio.gather(
        *(check(backend, args.permission_mode, args.check_write_guard) for backend in backends)
    )


if __name__ == "__main__":
    asyncio.run(main())
