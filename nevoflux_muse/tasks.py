"""Delegated tasks (M3): the head's four task tools, their limits, and their approvals.

A delegated task runs a whole NevoFlux agent turn on the person's desktop. Its
approvals are not MCP elicitations (approvals.py): `nevoflux_task_wait` lists
them and `nevoflux_task_approve` answers one by task id and approval id. `nf
approve <id>` only has the approval id, so the bridge remembers which task each
approval id was listed with (ApprovalIndex), for as long as it runs. Approval
and task ids are UUIDs; the bridge's own elicitation ids are 8 hex characters,
so the shape alone tells the two kinds apart (M3 contract R7).
"""

from __future__ import annotations

import re

START = "nevoflux_task_start"
WAIT = "nevoflux_task_wait"
APPROVE = "nevoflux_task_approve"
CANCEL = "nevoflux_task_cancel"
TOOLS = (START, WAIT, APPROVE, CANCEL)
PROMPT_MAX = 8000
ANSWER_MAX = 4000
WAIT_MIN, WAIT_MAX, WAIT_DEFAULT = 1, 60, 30
WAIT_SLACK = 30.0  # the bridge's timeout for a wait: timeout_secs plus this
INDEX_LIMIT = 256

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                   re.IGNORECASE)


def is_task_id(value: object) -> bool:
    """True for a UUID string: a task id or a delegated task's approval id."""
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


class ApprovalIndex:
    """Which task each approval id came with, newest INDEX_LIMIT kept."""

    def __init__(self, limit: int = INDEX_LIMIT):
        self._limit = limit
        self._task: dict[str, str] = {}

    def __len__(self) -> int:
        return len(self._task)

    def learn(self, result: dict) -> None:
        """Record the approvals a nevoflux_task_wait result lists; ignore anything else."""
        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            return
        task_id, approvals = structured.get("task_id"), structured.get("approvals")
        if not is_task_id(task_id) or not isinstance(approvals, list):
            return
        for item in approvals:
            approval_id = item.get("approval_id") if isinstance(item, dict) else None
            if is_task_id(approval_id):
                key = approval_id.lower()  # R7: lowercase on the wire; match either case
                self._task.pop(key, None)
                self._task[key] = task_id.lower()
        while len(self._task) > self._limit:
            del self._task[next(iter(self._task))]

    def task_of(self, approval_id: str) -> str | None:
        return self._task.get(approval_id.lower())
