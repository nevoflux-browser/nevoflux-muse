import uuid

import pytest

from nevoflux_muse import tasks
from nevoflux_muse.testing.fake_head import (
    APPROVAL_ID,
    TASK_ID,
    approval_item,
    task_result,
    task_state,
)


def test_task_ids_are_uuids():
    assert tasks.is_task_id(TASK_ID) and tasks.is_task_id(TASK_ID.upper())
    for bad in ("3f9a1c2e", "", None, 7, TASK_ID + "0", "../" + TASK_ID[3:]):
        assert not tasks.is_task_id(bad), bad


def test_the_tool_names_are_the_contracts():
    assert tasks.TOOLS == ("nevoflux_task_start", "nevoflux_task_wait",
                           "nevoflux_task_approve", "nevoflux_task_cancel")


def test_the_index_learns_approvals_from_a_wait():
    index = tasks.ApprovalIndex()
    other = str(uuid.uuid4())
    index.learn(task_result(task_state("awaiting_approval", approvals=[
        approval_item(), approval_item("question", approval_id=other)])))
    assert index.task_of(APPROVAL_ID) == TASK_ID and index.task_of(other) == TASK_ID
    assert index.task_of(APPROVAL_ID.upper()) == TASK_ID  # contract R7: matched case-insensitively
    assert index.task_of(str(uuid.uuid4())) is None


@pytest.mark.parametrize("result", [
    {},
    {"structuredContent": None},
    {"structuredContent": {"task_id": "x", "approvals": [approval_item()]}},
    {"structuredContent": {"task_id": TASK_ID, "approvals": "none"}},
    {"structuredContent": {"task_id": TASK_ID, "approvals": [{"approval_id": "3f9a1c2e"}, 5]}},
])
def test_the_index_ignores_what_it_cannot_read(result):
    index = tasks.ApprovalIndex()
    index.learn(result)
    assert len(index) == 0


def test_the_index_forgets_the_oldest_beyond_its_limit():
    index = tasks.ApprovalIndex(limit=2)
    ids = [str(uuid.uuid4()) for _ in range(3)]
    for approval_id in ids:
        index.learn(task_result(task_state("awaiting_approval",
                                           approvals=[approval_item(approval_id=approval_id)])))
    assert len(index) == 2
    assert index.task_of(ids[0]) is None and index.task_of(ids[2]) == TASK_ID


def test_a_desktop_only_approval_is_still_indexed():  # contract R39: the daemon decides
    index = tasks.ApprovalIndex()
    index.learn(task_result(task_state("awaiting_approval", approvals=[
        approval_item(tool=None, choices=[], desktop_only=True)])))
    assert index.task_of(APPROVAL_ID) == TASK_ID
