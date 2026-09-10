"""Immutable Markdown projections of approved, durable workflow records."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from pathlib import Path


def document_path(namespace: str, version: int) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", namespace):
        raise ValueError("Invalid consensus document namespace")
    if type(version) is not int or version < 1:
        raise ValueError("Invalid consensus document version")
    return f"docs/agent-team/{namespace}/consensus-v{version:04d}.md"


def render_consensus(record: dict) -> str:
    proposal = record["proposal"]
    lines = [
        f"# Team consensus · proposal v{record['version']}",
        "",
        f"Recorded: {record['recorded_at']}",
        "",
        "Approved by: " + ", ".join(record["approvals"]),
        "",
        "This is an approved snapshot, not an implementation completion report. "
        "The room's current workflow determines whether it is active or under revision.",
        "",
    ]
    if record.get("recovered"):
        lines.extend(
            [
                "Recovered from an existing unanimous workflow. The recording time above is the "
                "migration time, not the original approval time.",
                "",
            ]
        )
    if record.get("supersedes"):
        version = record["supersedes"]
        lines.extend([f"Supersedes: [proposal v{version}](consensus-v{version:04d}.md)", ""])
    if request := record.get("revision_request"):
        lines.extend(
            [
                "## Reason for revision",
                "",
                f"Requested by: {request['speaker']}",
                "",
                request["reason"],
                "",
            ]
        )
    lines.extend(
        ["## Scope and approach", "", proposal["summary"], "", "## Acceptance criteria", ""]
    )
    for criterion in proposal["acceptance_criteria"]:
        lines.extend(["- " + criterion.replace("\n", "\n  ")])
    lines.extend(
        [
            "",
            "## Shared milestones",
            "",
            "Milestones are shared work, not exclusive assignments.",
            "",
        ]
    )
    for task in proposal["tasks"]:
        lines.extend([f"### {task['id']} · {task['title']}", "", task["details"], ""])
        lines.extend(["Dependencies: " + (", ".join(task["depends_on"]) or "none"), ""])
    lines.extend(
        [
            "## Agreed acceptance commands",
            "",
            "Commands are argv arrays, executed after peer and integration review.",
            "",
            "```json",
            json.dumps(proposal["acceptance_checks"], ensure_ascii=False, indent=2),
            "```",
            "",
            "## Revising this consensus",
            "",
            "Continue discussing in the room. Use `/revise <guidance>` to reopen planning, "
            "or let a member submit a versioned `request_revision` action. "
            "Existing files and this record remain; "
            "the next proposal requires fresh approval from every member before work continues.",
            "",
            "This file is coordinator-generated. Do not edit it to change the team's instructions. "
            "A revised proposal produces a new document; "
            "the durable public workflow is authoritative.",
            "",
        ]
    )
    return "\n".join(lines)


def write_consensus(workspace: Path, namespace: str, record: dict) -> Path:
    """Create a complete record atomically; never replace files or traverse symlinks."""
    relative = document_path(namespace, record["version"])
    if record["document"] != relative:
        raise ValueError("Consensus document path does not match its approved record")
    content = render_consensus(record).encode("utf-8")
    directory, filename = relative.rsplit("/", 1)
    # Directory-relative operations keep a symlink swap from redirecting a write.
    descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    temporary = None
    try:
        for part in directory.split("/"):
            try:
                os.mkdir(part, mode=0o755, dir_fd=descriptor)
            except FileExistsError:
                pass
            next_directory = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = next_directory

        def matches_existing():
            try:
                existing = os.open(
                    filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
                )
            except FileNotFoundError:
                return False
            with os.fdopen(existing, "rb") as handle:
                metadata = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_size != len(content)
                    or handle.read() != content
                ):
                    raise ValueError(
                        f"Consensus document already exists with different content: {relative}. "
                        "Preserve or move it before /resume; it will not be overwritten."
                    )
            return True

        if matches_existing():
            return workspace / relative
        temporary_name = f".{filename}.{uuid.uuid4().hex}.tmp"
        target = os.open(
            temporary_name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o644,
            dir_fd=descriptor,
        )
        temporary = temporary_name
        with os.fdopen(target, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(
                temporary,
                filename,
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            if not matches_existing():
                raise
        os.unlink(temporary, dir_fd=descriptor)
        temporary = None
        os.fsync(descriptor)
        return workspace / relative
    finally:
        if temporary:
            os.unlink(temporary, dir_fd=descriptor)
        os.close(descriptor)
