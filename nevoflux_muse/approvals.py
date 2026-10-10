"""Approvals the head asks for while a call runs (M2a spec §4.4).

The head asks with a form elicitation whose schema is a single required
boolean, `approve`. The bridge has no person to ask, so it parks the question
under a short random id and lets `nf call` return at once (exit 3); `nf
approve` or `nf deny` answers it later. A question can be answered for
`window` seconds, less than the head's 60, so an answer always reaches the
head in time. After that the bridge waits up to `hold` seconds for the head to
give up on its own (it cancels the request, or the session ends), then answers
"cancel" itself so nothing is left hanging.

Which call a question belongs to: the bridge stamps every tools/call with
_meta[CALL_META] = the call's id. A head that copies it into the elicitation's
_meta pins the question to that call; a stamp naming no running call is
declined at once: nobody is waiting for it, and approving another call must
never authorise it. An unstamped question goes to the oldest running call
without an open question: exact while one call runs at a time, which is how
agents use `nf`. Other _meta keys (the real head adds progressToken, equal to
the request id) are ignored.

Once a call has finished, or the bridge is stopping, no answer is accepted:
stop answers every open question "cancel" before it takes the calls down.

The log names approval ids, tools and outcomes, never the head's message: it
can quote what the call would type or open.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from mcp import types

log = logging.getLogger(__name__)

CALL_META = "io.nevoflux/call"
APPROVAL_WINDOW = 55.0
APPROVAL_HOLD = 75.0
APPROVAL_SCHEMA = {"type": "object", "properties": {"approve": {"type": "boolean"}},
                   "required": ["approve"]}


def is_approval_form(params: object) -> bool:
    """True for the head's approval question: a form with one boolean field, approve."""
    if not isinstance(params, types.ElicitRequestFormParams):
        return False
    schema = params.requested_schema
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or set(props) != {"approve"}:
        return False
    approve = props["approve"]
    return isinstance(approve, dict) and approve.get("type") == "boolean"


class Deadline:
    """A timeout that cancels the task it watches when it passes; it can be pushed later."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.expired = False
        self._loop = asyncio.get_running_loop()
        self._at = self._loop.time() + seconds
        self._task: asyncio.Future | None = None
        self._timer: asyncio.TimerHandle | None = None

    def watch(self, task: asyncio.Future) -> None:
        self._task = task
        self._arm()

    def postpone(self, seconds: float) -> None:
        """Move the deadline to `seconds` from now, unless it is already later."""
        at = self._loop.time() + seconds
        if at > self._at:
            self._at = at
            if self._timer is not None:
                self._arm()

    def restart(self) -> None:
        """Give the watched task its whole `seconds` again, from now: earlier or later."""
        self._at = self._loop.time() + self.seconds
        if self._timer is not None:
            self._arm()

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _arm(self) -> None:
        self.stop()
        self._timer = self._loop.call_at(self._at, self._fire)

    def _fire(self) -> None:
        self._timer = None
        if self._task is not None and not self._task.done():
            self.expired = True
            self._task.cancel()


@dataclass(eq=False)
class Call:
    id: str
    tool: str
    task: asyncio.Task | None = None
    deadline: Deadline | None = None
    approval: Approval | None = None
    asked: asyncio.Event = field(default_factory=asyncio.Event)


def _finished(call: Call) -> bool:
    return call.task is not None and call.task.done()


@dataclass(eq=False)
class Approval:
    id: str
    message: str
    expires_at: float
    answer: asyncio.Future
    call: Call | None = None

    def public(self, now: float) -> dict:
        return {"id": self.id, "message": self.message,
                "expires_in": max(0, int(self.expires_at - now))}


class Approvals:
    def __init__(self, *, window: float = APPROVAL_WINDOW, hold: float = APPROVAL_HOLD,
                 clock: Callable[[], float] = time.monotonic):
        self.window, self.hold, self._clock = window, hold, clock
        self._calls: dict[str, Call] = {}  # running calls, in start order
        self._open: dict[str, Approval] = {}

    def now(self) -> float:
        return self._clock()

    def _new_id(self) -> str:
        while True:
            new = secrets.token_hex(4)
            if new not in self._calls and new not in self._open:
                return new

    def open_call(self, tool: str) -> Call:
        call = Call(id=self._new_id(), tool=tool)
        self._calls[call.id] = call
        return call

    def close_call(self, call: Call) -> None:
        self._calls.pop(call.id, None)

    def _owner(self, meta: object) -> tuple[bool, Call | None]:
        """(stamped, owner). A stamped question belongs to that running call or to none."""
        wanted = meta.get(CALL_META) if isinstance(meta, dict) else None
        if wanted is not None:
            # Never guess another call: approving that one would authorise this action.
            call = self._calls.get(wanted) if isinstance(wanted, str) else None
            return True, call if call is not None and not _finished(call) else None
        for call in self._calls.values():
            if call.approval is None and not _finished(call):
                return False, call
        return False, None

    async def elicit(self, context, params) -> types.ElicitResult:
        """The ClientSession elicitation callback: park the question until it is answered."""
        if not is_approval_form(params):
            log.info("declined a question that is not an approval")
            return types.ElicitResult(action="decline")
        meta = getattr(context, "meta", None)
        if meta is None:
            meta = params.meta
        stamped, call = self._owner(meta)
        if stamped and call is None:
            log.info("declined a question for a call that is not running")
            return types.ElicitResult(action="decline")
        approval = Approval(id=self._new_id(), message=params.message,
                            expires_at=self._clock() + self.window,
                            answer=asyncio.get_running_loop().create_future(), call=call)
        self._open[approval.id] = approval
        if call is not None:
            call.approval = approval
            call.asked.set()
            if call.deadline is not None:  # the wait must not eat the call's own timeout
                call.deadline.postpone(self.hold + call.deadline.seconds)
        log.info("approval %s asked for %s", approval.id,
                 f"call {call.tool!r}" if call is not None else "no running call")
        outcome = "gone"
        try:
            result = await asyncio.wait_for(approval.answer, self.hold)
            outcome = {"accept": "approved", "decline": "denied"}.get(result.action,
                                                                       "cancelled")
            return result
        except asyncio.TimeoutError:
            outcome = "expired"
            return types.ElicitResult(action="cancel")
        finally:
            self._open.pop(approval.id, None)
            if call is not None and call.approval is approval:
                call.approval = None
            if not approval.answer.done():
                approval.answer.cancel()
            log.info("approval %s -> %s", approval.id, outcome)

    def answer(self, approval_id: str, approve: bool) -> Approval | None:
        """Answer an open question; None when it is not open (expired, answered, unknown)."""
        approval = self._open.get(approval_id)
        if approval is None or approval.answer.done() or self._clock() >= approval.expires_at:
            return None
        if approval.call is not None and _finished(approval.call):
            return None  # the call it would authorise is over: nothing left to approve
        approval.answer.set_result(
            types.ElicitResult(action="accept", content={"approve": True}) if approve is True
            else types.ElicitResult(action="decline"))
        call = approval.call
        if call is not None and call.deadline is not None:
            # The question postponed the deadline by hold + timeout; once answered the call
            # gets its own timeout again, from now, not whatever is left of the hold.
            call.deadline.restart()
        return approval

    def cancel_open(self) -> None:
        """Answer every open question "cancel" now, so no later answer can approve it."""
        for approval in list(self._open.values()):
            if not approval.answer.done():
                approval.answer.set_result(types.ElicitResult(action="cancel"))

    def pending(self) -> list[dict]:
        """The questions still open to an answer: [{"id", "tool", "message", "expires_in"}]."""
        now = self._clock()
        return [{**a.public(now), "tool": a.call.tool if a.call is not None else None}
                for a in self._open.values() if not a.answer.done() and now < a.expires_at]
