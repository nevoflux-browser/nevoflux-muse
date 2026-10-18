import argparse
import json
from pathlib import Path

import pytest
from mcp import types

from nevoflux_muse.approvals import APPROVAL_SCHEMA, is_approval_form
from nevoflux_muse.cli import _call_reply

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "muse" / "tool_errors.json"
EXTRAS = {"tier_denied": {"tier", "required"}, "approval_denied": {"reason"},
          "tab_closed": {"tab_id"}, "tool_failed": {"detail"},
          "task_busy": {"task_id"}, "rate_limited": {"retry_after"},
          "session_not_found": {"session"}, "task_not_found": {"task_id"},
          "approval_not_found": {"approval_id"}, "approval_desktop_only": {"approval_id"}}
CODES = {"unknown_tool", "tier_denied", "approval_denied", "approval_timeout",
         "browser_unavailable", "no_agent_tab", "tab_closed", "tool_failed",
         "task_busy", "rate_limited", "session_not_found", "task_not_found",
         "approval_not_found", "approval_desktop_only", "paused"}


def load():
    return json.loads(FIX.read_text(encoding="utf-8"))


def dump(result):
    return result.model_dump(mode="json", by_alias=True, exclude_none=True)


def test_the_fixture_has_the_agreed_top_level_shape():
    # Extra top-level keys (notes, call_meta_key, ...) are allowed; these are required.
    data = load()
    assert {"approval_request", "approve", "deny", "errors"} <= data.keys()


def test_every_code_is_there():
    assert set(load()["errors"]) == CODES


@pytest.mark.parametrize("code", sorted(CODES))
def test_each_error_parses_and_prints_its_code(code, capsys):
    raw = load()["errors"][code]
    assert types.CallToolResult.model_validate(raw).is_error is True
    assert _call_reply(argparse.Namespace(json=True), {"ok": True, "result": raw}) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == code and isinstance(out["message"], str)
    assert EXTRAS.get(code, set()) <= out.keys()


def test_the_approval_request_is_one_the_bridge_answers():
    # The head's request also carries params._meta.progressToken (added by rmcp,
    # equal to the id); the id and the token are never pinned here.
    req = load()["approval_request"]
    assert req["method"] == "elicitation/create"
    params = types.ElicitRequestFormParams.model_validate(req["params"])
    assert is_approval_form(params) and params.requested_schema == APPROVAL_SCHEMA


def test_our_answers_are_the_ones_the_head_expects():
    data = load()
    assert dump(types.ElicitResult(action="accept", content={"approve": True})) == data["approve"]
    assert dump(types.ElicitResult(action="decline")) == data["deny"]


def test_the_provider_form_of_task_busy(capsys):
    # Contract R38: busy because of the model provider, not a task: no task_id, a provider.
    raw = load()["task_busy_provider"]
    assert types.CallToolResult.model_validate(raw).is_error is True
    assert _call_reply(argparse.Namespace(json=True), {"ok": True, "result": raw}) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "task_busy" and isinstance(out["message"], str)
    assert isinstance(out["provider"], str) and "task_id" not in out
