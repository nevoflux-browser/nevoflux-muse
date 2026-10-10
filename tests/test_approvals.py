import asyncio
import logging
from types import SimpleNamespace

from mcp import types

from nevoflux_muse.approvals import (
    APPROVAL_SCHEMA,
    CALL_META,
    Approvals,
    Deadline,
    is_approval_form,
)

CTX = SimpleNamespace(meta=None)


def form(schema=None, message="Allow browser_click?", meta=None):
    raw = {"message": message, "requestedSchema": schema or APPROVAL_SCHEMA}
    if meta is not None:
        raw["_meta"] = meta
    return types.ElicitRequestFormParams.model_validate(raw)


def test_only_the_approval_form_is_an_approval():
    assert is_approval_form(form())
    assert not is_approval_form(form({"type": "object",
                                      "properties": {"name": {"type": "string"}}}))
    assert not is_approval_form(form({"type": "object", "properties": {
        "approve": {"type": "boolean"}, "why": {"type": "string"}}}))
    url = types.ElicitRequestURLParams.model_validate(
        {"mode": "url", "message": "m", "url": "https://x.example", "elicitationId": "1"})
    assert not is_approval_form(url)


async def test_other_questions_are_declined_and_never_parked():
    a = Approvals()
    other = form({"type": "object", "properties": {"name": {"type": "string"}}})
    result = await a.elicit(CTX, other)
    assert result.action == "decline" and a.pending() == []


async def test_approve_and_deny_answer_the_head():
    a = Approvals()
    call = a.open_call("browser_click")
    asking = asyncio.create_task(a.elicit(CTX, form()))
    await asyncio.sleep(0)
    [listed] = a.pending()
    assert listed["tool"] == "browser_click" and listed["message"] == "Allow browser_click?"
    assert 54 <= listed["expires_in"] <= 55 and len(listed["id"]) == 8
    assert call.approval is not None and call.asked.is_set()
    assert a.answer(listed["id"], True) is not None
    result = await asyncio.wait_for(asking, 1)
    assert result.action == "accept" and result.content == {"approve": True}
    assert a.pending() == [] and call.approval is None
    assert a.answer(listed["id"], True) is None  # answered: no longer open

    asking = asyncio.create_task(a.elicit(CTX, form()))
    await asyncio.sleep(0)
    a.answer(a.pending()[0]["id"], False)
    assert (await asyncio.wait_for(asking, 1)).action == "decline"


async def test_after_the_window_answers_are_refused_and_hold_cancels():
    a = Approvals(window=0.05, hold=0.2)
    asking = asyncio.create_task(a.elicit(CTX, form()))
    await asyncio.sleep(0)
    approval_id = next(iter(a._open))
    await asyncio.sleep(0.1)
    assert a.pending() == [] and a.answer(approval_id, True) is None
    assert (await asyncio.wait_for(asking, 1)).action == "cancel"
    assert a._open == {}


async def test_a_cancelled_question_is_forgotten():
    a = Approvals()
    asking = asyncio.create_task(a.elicit(CTX, form()))
    await asyncio.sleep(0)
    approval_id = a.pending()[0]["id"]
    asking.cancel()  # what the SDK does on notifications/cancelled or teardown
    await asyncio.gather(asking, return_exceptions=True)
    assert a.pending() == [] and a.answer(approval_id, True) is None


async def test_the_echoed_call_id_wins_otherwise_the_oldest_free_call():
    a = Approvals()
    first, second = a.open_call("hang"), a.open_call("browser_click")
    pinned = asyncio.create_task(a.elicit(CTX, form(meta={CALL_META: second.id})))
    await asyncio.sleep(0)
    assert second.approval is not None and first.approval is None
    unpinned = asyncio.create_task(a.elicit(CTX, form()))
    await asyncio.sleep(0)
    assert first.approval is not None  # oldest call without an open question
    third = asyncio.create_task(a.elicit(CTX, form()))
    await asyncio.sleep(0)
    assert {p["tool"] for p in a.pending()} == {"browser_click", "hang", None}
    for t in (pinned, unpinned, third):
        t.cancel()
    await asyncio.gather(pinned, unpinned, third, return_exceptions=True)


async def test_a_stamp_naming_no_running_call_is_never_given_to_another_call():
    a = Approvals()
    gone = a.open_call("browser_type")
    a.close_call(gone)  # e.g. the client already gave up on it
    running = a.open_call("browser_click")
    asking = asyncio.create_task(a.elicit(CTX, form(meta={CALL_META: gone.id})))
    await asyncio.sleep(0)
    assert running.approval is None and not running.asked.is_set()
    [listed] = a.pending()
    assert listed["tool"] is None
    asking.cancel()
    await asyncio.gather(asking, return_exceptions=True)


async def test_only_true_approves():
    a = Approvals()
    for answer in (1, "yes", [True]):
        asking = asyncio.create_task(a.elicit(CTX, form()))
        await asyncio.sleep(0)
        assert a.answer(a.pending()[0]["id"], answer) is not None
        assert (await asyncio.wait_for(asking, 1)).action == "decline"


async def test_the_heads_message_is_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    secret = "Type 'hunter2' into #password\nTab: tab 1\nWhy: test asks"
    a = Approvals()
    call = a.open_call("browser_type")
    asking = asyncio.create_task(a.elicit(CTX, form(message=secret)))
    await asyncio.sleep(0)
    a.answer(a.pending()[0]["id"], True)
    await asyncio.wait_for(asking, 1)
    a.close_call(call)
    assert caplog.records  # the bridge does log the approval, by id
    for record in caplog.records:
        text = record.getMessage()
        assert "hunter2" not in text and "Tab: tab 1" not in text


async def test_the_real_heads_request_shape_is_accepted():
    # tool_errors.json approval_request: a multi-line message, and _meta with the
    # echoed call id plus rmcp's integer progressToken (= the request id, variable).
    a = Approvals()
    a.open_call("hang")
    call = a.open_call("browser_click")
    message = "Click on element 'e1'\nTab: tab 1\nWhy: ask-guard wants a person to confirm"
    for token in (0, "7"):
        params = form(message=message, meta={CALL_META: call.id, "progressToken": token})
        assert is_approval_form(params)
        asking = asyncio.create_task(a.elicit(SimpleNamespace(meta=params.meta), params))
        await asyncio.sleep(0)
        [listed] = a.pending()
        assert listed["tool"] == "browser_click" and listed["message"] == message
        assert set(listed) == {"id", "tool", "message", "expires_in"}
        a.answer(listed["id"], True)
        assert (await asyncio.wait_for(asking, 1)).action == "accept"


async def test_deadline_fires_and_cancels():
    d = Deadline(0.05)
    task = asyncio.create_task(asyncio.sleep(5))
    d.watch(task)
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled() and d.expired


async def test_deadline_postpone():
    d = Deadline(0.05)
    task = asyncio.create_task(asyncio.sleep(0.2))
    d.watch(task)
    d.postpone(1.0)
    await task  # finishes on its own: the deadline moved past it
    assert not d.expired
    d.stop()


async def test_a_question_postpones_its_calls_deadline():
    a = Approvals(hold=2.0)
    call = a.open_call("browser_click")
    call.deadline = Deadline(0.05)
    call.task = asyncio.create_task(asyncio.sleep(0.2))
    call.deadline.watch(call.task)
    asking = asyncio.create_task(a.elicit(CTX, form()))
    await call.task
    assert not call.deadline.expired
    asking.cancel()
    await asyncio.gather(asking, return_exceptions=True)
    call.deadline.stop()
