"""Optional live two-turn session checks; uses the configured CLI accounts and quota."""

from __future__ import annotations

import argparse
import asyncio
import secrets
import tempfile
from pathlib import Path

from agent_team.adapters import CLIAdapter
from agent_team.config import load_config


async def probe(agent, workspace, deadline, permission_mode):
    token = secrets.token_hex(8)
    adapter = CLIAdapter(agent, workspace, permission_mode=permission_mode)
    try:
        async with asyncio.timeout(deadline):
            _ = "".join(
                [
                    text
                    async for text in adapter.stream(
                        f"Remember verification token {token}. Reply only ACK. Do not use tools.",
                        persist_session=True,
                    )
                ]
            )
        identifier = adapter.result_session_id
        if not identifier:
            raise ValueError("Backend did not return a persistent session ID")
        print(f"{agent.name}: created {identifier}", flush=True)
        # Use a NEW process wrapper to prove continuity is not an in-memory adapter cache.
        resumed = CLIAdapter(agent, workspace, permission_mode=permission_mode)
        async with asyncio.timeout(deadline):
            reply = "".join(
                [
                    text
                    async for text in resumed.stream(
                        "What verification token did I ask you to remember? "
                        "Reply with only the token. Do not use tools.",
                        persist_session=True,
                        session_id=identifier,
                    )
                ]
            )
        if resumed.result_session_id != identifier or token not in reply:
            raise ValueError("Resumed session did not preserve its ID and earlier context")
        print(f"{agent.name}: exact session resumed; earlier context retained", flush=True)
        return True
    except Exception as exc:
        detail = "invocation timed out" if isinstance(exc, TimeoutError) else str(exc)
        print(f"{agent.name}: FAILED: {detail}", flush=True)
        return False


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("team.toml"))
    parser.add_argument("--backend", choices=("codex", "claude"))
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    config = load_config(args.config)
    agents = [
        a
        for a in config.agents
        if a.backend in {"codex", "claude"} and (not args.backend or a.backend == args.backend)
    ]
    if not agents:
        raise SystemExit("No matching CLI agents configured")
    with tempfile.TemporaryDirectory(prefix="agent-team-sessions-") as directory:
        results = await asyncio.gather(
            *(probe(a, Path(directory), args.timeout, config.permission_mode) for a in agents)
        )
    if not all(results):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
