"""The head's M3 task fixtures (plan A) through the client's own code paths."""

import argparse
import json
from pathlib import Path

from mcp import types

from nevoflux_muse import cli, tasks

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "muse"
WAITS = ("wait_running", "wait_awaiting_approval", "wait_question_and_plan", "wait_desktop_only",
         "wait_guarded_approval", "wait_done_with_image", "wait_done_again",
         "wait_done_screenshot_error", "wait_failed", "wait_cancelled")
SENT_KEYS = {"nevoflux_task_start": {"prompt", "session"},
             "nevoflux_task_wait": {"task_id", "timeout_secs"},
             "nevoflux_task_approve": {"task_id", "approval_id", "choice", "answer"},
             "nevoflux_task_cancel": {"task_id"}}
REQUIRED = {"nevoflux_task_start": ["prompt"], "nevoflux_task_wait": ["task_id"],
            "nevoflux_task_approve": ["task_id", "approval_id", "choice"],
            "nevoflux_task_cancel": ["task_id"]}


def fixture(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def as_result(entry):
    """A task_results.json wait entry as the CallToolResult the head sends (plan A Task 16)."""
    sc = entry["structuredContent"]
    content = [{"type": "text", "text": json.dumps(sc)}]
    if entry["image"] is not None:
        content.append(entry["image"])
    return {"content": content, "structuredContent": sc, "isError": entry["isError"]}


def test_every_wait_case_is_present_and_parses():
    f = fixture("task_results.json")
    for key in WAITS:
        raw = as_result(f[key])
        assert types.CallToolResult.model_validate(raw).is_error is False, key


def test_start_approve_and_cancel_results():
    f = fixture("task_results.json")
    assert tasks.is_task_id(f["start"]["task_id"]) and isinstance(f["start"]["session"], str)
    assert f["start"]["state"] == "running"
    assert f["approve"]["ok"] is True and isinstance(f["approve"]["state"], str)
    assert tasks.is_task_id(f["cancel"]["task_id"]) and f["cancel"]["state"] == "cancelled"


def test_wait_results_print_and_exit_by_state(tmp_path, capsys):
    f = fixture("task_results.json")
    seen, kinds = set(), set()
    for key in WAITS:
        raw = as_result(f[key])
        code = cli._task_wait_output(argparse.Namespace(json=True), raw, tmp_path)
        out = json.loads(capsys.readouterr().out)
        state = out["state"]
        seen.add(state)
        assert code == cli._WAIT_EXIT[state], key
        for a in out.get("approvals") or []:
            assert tasks.is_task_id(a["approval_id"])
            kinds.add(a["kind"])
            # never allow_always (C8); a desktop-only approval offers the agent nothing (R25)
            assert a["choices"] == ([] if a.get("desktop_only") is True else ["allow", "deny"])
        if f[key]["image"] is not None:
            assert Path(out["screenshot"]).is_file()
    assert seen == {"running", "awaiting_approval", "done", "failed", "cancelled"}
    assert kinds == {"tool", "question", "plan"}


def test_the_guarded_case_is_answerable_and_the_desktop_only_case_is_not(tmp_path, capsys):
    # Contract v1.2 R39: the same must-approve approval, the pairing's toggle off, then on.
    f = fixture("task_results.json")
    desk = f["wait_desktop_only"]["structuredContent"]["approvals"][0]
    guarded = f["wait_guarded_approval"]["structuredContent"]["approvals"][0]
    assert desk["desktop_only"] is True and desk["choices"] == []
    assert "desktop_only" not in guarded and guarded["choices"] == ["allow", "deny"]
    assert guarded["approval_id"] == desk["approval_id"]
    for key, offered in (("wait_desktop_only", False), ("wait_guarded_approval", True)):
        code = cli._task_wait_output(argparse.Namespace(json=False), as_result(f[key]), tmp_path)
        out = capsys.readouterr().out
        assert code == 3, key
        assert ("`nf approve <approval_id>`" in out) is offered, key


def test_the_tools_are_the_ones_the_client_calls():
    names = [t["name"] for t in fixture("task_tools.json")["tools"]]
    assert tuple(names) == tasks.TOOLS


def test_the_schemas_match_the_client_limits():
    by_name = {t["name"]: t["inputSchema"] for t in fixture("task_tools.json")["tools"]}
    for name, required in REQUIRED.items():
        assert by_name[name]["required"] == required, name
    t = by_name[tasks.WAIT]["properties"]["timeout_secs"]
    assert (t["minimum"], t["maximum"], t["default"]) == (
        tasks.WAIT_MIN, tasks.WAIT_MAX, tasks.WAIT_DEFAULT)


def test_the_requests_use_only_arguments_the_client_sends():
    reqs = fixture("task_tools.json")["requests"]
    assert {r["params"]["name"] for r in reqs} == set(tasks.TOOLS)
    for req in reqs:
        assert req["method"] == "tools/call"
        params = req["params"]
        assert set(params.get("arguments") or {}) <= SENT_KEYS[params["name"]]
        if params["name"] == "nevoflux_task_approve":
            assert params["arguments"]["choice"] in {"allow", "deny"}


def test_the_progress_shapes_are_pinned():
    # 0.4.0 sends no progressToken (P1), but the head's shapes are pinned.
    f = fixture("task_results.json")
    msgs = f["progress_messages"]
    assert msgs["steps"] and all(isinstance(s, str) for s in msgs["steps"])
    assert isinstance(msgs["approval"], str) and isinstance(msgs["failed_step_example"], str)
    note = types.ProgressNotification.model_validate(f["progress_notification"])
    assert note.params.message == f["progress_notification"]["params"]["message"]
    assert isinstance(note.params.progress, (int, float))


def wait_json(entry, d, capsys):
    code = cli._task_wait_output(argparse.Namespace(json=True), as_result(entry), d)
    return code, json.loads(capsys.readouterr().out)


def test_done_cases_carry_their_reply_and_screenshot_fields(tmp_path, capsys):
    f = fixture("task_results.json")
    code, saved = wait_json(f["wait_done_with_image"], tmp_path, capsys)
    assert code == 0 and saved["reply"] == "Here is your calculator."
    assert Path(saved["screenshot"]).is_file() and "screenshot_error" not in saved
    # the head sends the image once: a later wait (no image) names the file saved above
    code, again = wait_json(f["wait_done_again"], tmp_path, capsys)
    assert code == 0 and again["reply"] == "Here is your calculator."
    assert again["screenshot"] == saved["screenshot"]
    code, err = wait_json(f["wait_done_screenshot_error"], tmp_path, capsys)
    assert code == 0 and err["reply"] == "Done." and err["screenshot_error"] == "no_tab"
    assert "screenshot" not in err
    code, failed = wait_json(f["wait_failed"], tmp_path, capsys)
    assert code == 1 and failed["error"]["code"] == "llm_error"
    assert "screenshot" not in failed


def test_text_mode_shows_the_reply_and_the_error_code(tmp_path, capsys):
    f = fixture("task_results.json")
    args = argparse.Namespace(json=False)
    cli._task_wait_output(args, as_result(f["wait_done_with_image"]), tmp_path)
    assert "Here is your calculator." in capsys.readouterr().out
    cli._task_wait_output(args, as_result(f["wait_done_screenshot_error"]), tmp_path)
    assert "No screenshot: no_tab" in capsys.readouterr().out
    cli._task_wait_output(args, as_result(f["wait_failed"]), tmp_path)
    assert "llm_error: the model call failed" in capsys.readouterr().out
