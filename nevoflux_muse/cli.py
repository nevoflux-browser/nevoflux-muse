"""The `nf` command.

Every subcommand is non-interactive and takes --json, because the one running
it is usually an agent, not a person at a terminal.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys
from pathlib import Path

from . import (
    PROTOCOL_MAX,
    PROTOCOL_MIN,
    __version__,
    auth,
    ipc,
    pairing,
    screenshots,
    state,
    tasks,
)


def _version(args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps({"client": __version__,
                          "protocol": {"min": PROTOCOL_MIN, "max": PROTOCOL_MAX}}))
    else:
        print(f"nevoflux-muse {__version__} (protocol {PROTOCOL_MIN}-{PROTOCOL_MAX})")
    return 0


class _Usage(Exception):
    pass


def _bridge_errors() -> tuple[type[Exception], ...]:
    from . import daemon
    return (ipc.BridgeDown, daemon.NotReady, daemon.Unsupported, daemon.AlreadyRunning,
            state.UnsupportedState)


def _notify(d, op: str) -> None:
    """Tell a running bridge to `op`; nothing to do when none runs."""
    from . import daemon
    if daemon.SUPPORTED:
        try:
            ipc.request(d, {"op": op}, 5.0)
        except ipc.BridgeDown:
            pass


def _ask(d, msg: dict, timeout: float) -> dict:
    from . import daemon
    daemon.ensure(d)
    return ipc.request(d, msg, timeout)


def _reply_failed(args: argparse.Namespace, reply: dict) -> int:
    err = dict(reply.get("error") or {})
    message = err.pop("message", "the bridge refused the request")
    if args.json:
        print(json.dumps({"error": message, **err}))
    else:
        print(f"nf: {message}", file=sys.stderr)
    return 1


EXIT_APPROVAL = 3
# bridge errors printed code-first, like the head's tool errors
_CODED = {"approval_expired", "task_required"}
_HINTS = {"approval_desktop_only": (
    "Ask the person at the desktop to answer it in NevoFlux, or to turn on "
    "\"Let Muse approve sensitive actions\" for this pairing there; do not retry.")}


def _coded_failure(args: argparse.Namespace, code: str, message: str, extra: dict) -> int:
    if args.json:
        print(json.dumps({"error": code, "message": message, **extra}))
    else:
        print(f"nf: {code}: {message}", file=sys.stderr)
        if code in _HINTS:
            print(_HINTS[code], file=sys.stderr)
    return 1


def _tool_error(result: dict) -> dict | None:
    """The head's structured tool error (M2a spec section 5), or None for any other result."""
    if not result.get("isError"):
        return None
    structured = result.get("structuredContent")
    err = structured.get("error") if isinstance(structured, dict) else None
    if isinstance(err, dict) and isinstance(err.get("code"), str):
        return err
    return None


def _call_reply(args: argparse.Namespace, reply: dict) -> int:
    """Print a call's outcome; `nf call`, `nf approve` and `nf deny` share it."""
    if not reply.get("ok"):
        err = dict(reply.get("error") or {})
        if err.get("code") in _CODED:
            code = err.pop("code")
            return _coded_failure(args, code, str(err.pop("message", "")), err)
        return _reply_failed(args, reply)
    if "approval_required" in reply:
        a = reply["approval_required"]
        if args.json:
            print(json.dumps({"approval_required": a}))
        else:
            print(f"NevoFlux asks for approval ({a['expires_in']}s left): {a['message']}")
            print(f"Answer with `nf approve {a['id']}` or `nf deny {a['id']}`.")
        return EXIT_APPROVAL
    if "answered" in reply:
        if reply["answered"] == "approved":
            print(json.dumps({"approved": True}) if args.json else "Approved.")
            return 0
        return _coded_failure(args, "approval_denied", "you denied the request", {})
    result = reply["result"]
    err = _tool_error(result)
    if err is not None:
        extra = dict(err)
        code = extra.pop("code")
        return _coded_failure(args, code, str(extra.pop("message", "")), extra)
    if args.json:
        print(json.dumps(result))
    else:
        _print_content(result)
    return 1 if result.get("isError") else 0


def _call_args(pairs: list[str], from_stdin: bool) -> dict:
    if from_stdin:
        if pairs:
            raise _Usage("key=value arguments cannot be combined with --args-stdin")
        try:
            obj = json.loads(sys.stdin.read())
        except ValueError:
            raise _Usage("--args-stdin needs a JSON object on stdin") from None
        if not isinstance(obj, dict):
            raise _Usage("--args-stdin needs a JSON object on stdin")
        return obj
    out: dict = {}
    for i, pair in enumerate(pairs, 1):
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise _Usage(f"argument {i} is not key=value")  # never echo it: it may be secret
        if key in out:
            raise _Usage(f"{key} is given twice")
        try:
            out[key] = json.loads(value)
        except ValueError:
            out[key] = value
    return out


def _print_content(result: dict) -> None:
    for block in result.get("content") or []:
        kind = block.get("type")
        if kind == "text":
            print(block.get("text", ""))
        elif kind == "image":
            size = len(base64.b64decode(block.get("data") or ""))
            print(f"[image {block.get('mimeType', '?')}, {size} bytes]")
        else:
            print(f"[{kind}]")


def _failed(args: argparse.Namespace, e: Exception) -> int:
    if args.json:
        print(json.dumps({"error": str(e)}))
    else:
        print(f"nf: {e}", file=sys.stderr)
    return 1


def _tools(args: argparse.Namespace) -> int:
    d = state.state_dir()
    try:
        reply = _ask(d, {"op": "tools"}, ipc.CONNECT_WAIT + ipc.CALL_TIMEOUT + 5)
    except _bridge_errors() as e:
        return _failed(args, e)
    if not reply.get("ok"):
        return _reply_failed(args, reply)
    if args.json:
        print(json.dumps(reply["tools"]))
    else:
        for tool in reply["tools"]:
            lines = (tool.get("description") or "").strip().splitlines()
            print(f"{tool['name']} — {lines[0]}" if lines else tool["name"])
    return 0


def _call(args: argparse.Namespace) -> int:
    d = state.state_dir()
    try:
        tool_args = _call_args(args.pairs, args.args_stdin)
    except _Usage as e:
        print(f"nf call: {e}", file=sys.stderr)
        return 2
    msg = {"op": "call", "tool": args.tool, "args": tool_args, "timeout": args.timeout}
    try:
        reply = _ask(d, msg, ipc.CONNECT_WAIT + args.timeout + 5)
    except _bridge_errors() as e:
        return _failed(args, e)
    return _call_reply(args, reply)


def _answer(args: argparse.Namespace) -> int:
    delegated = tasks.is_task_id(args.id)
    answer = getattr(args, "answer", None)  # only `nf approve` has --answer
    if not delegated and (args.task is not None or answer is not None):
        print(f"nf {args.command}: --task and --answer only go with a delegated task's "
              "approval_id (from `nf task wait`)", file=sys.stderr)
        return 2
    if answer is not None and len(answer) > tasks.ANSWER_MAX:
        print(f"nf {args.command}: the answer must be at most {tasks.ANSWER_MAX} characters",
              file=sys.stderr)
        return 2
    d = state.state_dir()
    msg = {"op": args.command, "id": args.id.lower() if delegated else args.id,
           "timeout": args.timeout}
    if args.task is not None:
        msg["task"] = args.task.lower()
    if answer is not None:
        msg["answer"] = answer
    try:
        reply = _ask(d, msg, args.timeout + 5)
    except _bridge_errors() as e:
        return _failed(args, e)
    if delegated:
        return _task_answer_reply(args, reply)
    return _call_reply(args, reply)


def _task_answer_reply(args: argparse.Namespace, reply: dict) -> int:
    result = _task_result(args, reply)
    if isinstance(result, int):
        return result
    answered = result["structuredContent"]
    if args.json:
        print(json.dumps(answered))
    else:
        task_state = answered.get("state", "?")
        text = f"Answered: the task is {task_state}. Follow it with `nf task wait`."
        if task_state == "awaiting_approval":
            text += " Other approvals may still be open; `nf task wait` shows the next one."
        print(text)
    return 0


def _pending(args: argparse.Namespace) -> int:
    d = state.state_dir()
    try:
        reply = _ask(d, {"op": "pending"}, 10.0)
    except _bridge_errors() as e:
        return _failed(args, e)
    if not reply.get("ok"):
        return _reply_failed(args, reply)
    pending = reply["pending"]
    if args.json:
        print(json.dumps(pending))
    elif not pending:
        print("No approvals are waiting.")
    else:
        for p in pending:
            message = " / ".join(str(p["message"]).splitlines())  # one line per approval
            print(f"{p['id']}  {p['tool'] or '?'}  {p['expires_in']}s  {message}")
    return 0


def _task_result(args: argparse.Namespace, reply: dict) -> dict | int:
    """A task tool's result, or the exit code once the failure is printed."""
    result = reply.get("result") if reply.get("ok") else None
    if reply.get("ok") and not isinstance(result, dict) and not (
            "approval_required" in reply or "answered" in reply):
        return _coded_failure(args, "bad_result",
                              "NevoFlux sent a task result this client cannot read", {})
    if not isinstance(result, dict) or result.get("isError"):
        return _call_reply(args, reply)  # bridge failures and the head's tool errors
    if not isinstance(result.get("structuredContent"), dict):
        return _coded_failure(args, "bad_result",
                              "NevoFlux sent a task result this client cannot read", {})
    return result


def _task_call(args: argparse.Namespace, tool: str, tool_args: dict,
               timeout: float) -> dict | int:
    msg = {"op": "call", "tool": tool, "args": tool_args, "timeout": timeout}
    try:
        reply = _ask(state.state_dir(), msg, ipc.CONNECT_WAIT + timeout + 5)
    except _bridge_errors() as e:
        return _failed(args, e)
    return _task_result(args, reply)


def _task_prompt(args: argparse.Namespace) -> str:
    if args.prompt_stdin:
        if args.prompt is not None:
            raise _Usage("give the prompt as an argument or with --prompt-stdin, not both")
        prompt = sys.stdin.read()
    elif args.prompt is None:
        raise _Usage("the prompt is missing: pipe it in with --prompt-stdin")
    else:
        prompt = args.prompt
    prompt = prompt.strip()
    if not 1 <= len(prompt) <= tasks.PROMPT_MAX:
        raise _Usage(f"prompt must be 1 to {tasks.PROMPT_MAX} characters")
    return prompt


def _task_start(args: argparse.Namespace) -> int:
    try:
        prompt = _task_prompt(args)
    except _Usage as e:
        print(f"nf task start: {e}", file=sys.stderr)
        return 2
    tool_args = {"prompt": prompt}
    if args.session is not None:
        if not args.session.strip():  # contract v1.5 R5: session must be a non-empty string
            print("nf task start: --session must not be empty", file=sys.stderr)
            return 2
        tool_args["session"] = args.session
    result = _task_call(args, tasks.START, tool_args, ipc.CALL_TIMEOUT)
    if isinstance(result, int):
        return result
    started = result["structuredContent"]
    if args.json:
        print(json.dumps(started))
    else:
        print(f"Task {started.get('task_id')} started in session {started.get('session')}.")
        print(f"Follow it with `nf task wait {started.get('task_id')}`.")
    return 0


_WAIT_EXIT = {"running": 0, "done": 0, "awaiting_approval": EXIT_APPROVAL,
              "failed": 1, "cancelled": 1}


def _one_line(text: object) -> str:
    return " / ".join(str(text).splitlines())


def _task_lines(t: dict) -> list[str]:
    """`nf task wait` for people: steps, approvals, then the outcome."""
    lines = [f"Task {t.get('task_id', '?')}: {t.get('state', '?')}"]
    if t.get("steps_truncated"):
        lines.append("  (older steps were dropped)")
    for s in t.get("steps") or []:
        lines.append(f"  {s.get('at', '?')}  {s.get('tool', '?')}  {s.get('status', '?')}")
    approvals = t.get("approvals") or []
    for a in approvals:
        lines.append(f"  approval {a.get('approval_id', '?')}  {a.get('kind', '?')}  "
                     f"{a.get('tool') or '?'}  {a.get('expires_in', '?')}s  "
                     f"{_one_line(a.get('summary', ''))}")
        if a.get("desktop_only") is True:  # contract v1.1 R25: not ours to answer
            lines.append("    waiting for the person at the desktop (you cannot answer this one)")
        if a.get("kind") == "question":
            lines.append(f"    question: {_one_line(a.get('question', ''))}")
            if a.get("options"):
                lines.append(f"    options: {' | '.join(_one_line(o) for o in a['options'])}")
        for step in a.get("plan_steps") or []:
            lines.append(f"    plan: {_one_line(step)}")
    if any(a.get("desktop_only") is not True for a in approvals):
        lines.append("Answer with `nf approve <approval_id>` (add `--answer TEXT` for a "
                     "question) or `nf deny <approval_id>`.")
    elif approvals:
        lines.append("Waiting for the person at the desktop to answer in NevoFlux; "
                     "run `nf task wait` again.")
    if t.get("state") == "done":
        if t.get("reply"):
            lines.append(str(t["reply"]))
        if t.get("reply_truncated"):
            lines.append("(the reply was cut short)")
        if "screenshot" in t:
            lines.append(f"Screenshot: {t['screenshot']}")
        elif "screenshot_error" in t:
            lines.append(f"No screenshot: {t['screenshot_error']}")
    if "screenshot_save_error" in t:
        lines.append(f"The screenshot could not be saved: {t['screenshot_save_error']}")
    err = t.get("error")
    if t.get("state") == "failed" and isinstance(err, dict):
        lines.append(f"{err.get('code', '?')}: {err.get('message', '')}")
    return lines


def _task_wait_output(args: argparse.Namespace, result: dict, d: Path) -> int:
    out = dict(result["structuredContent"])
    task_id = out.get("task_id")
    content = result.get("content")
    image = None  # contract R12: the image is content[1] of a done result, nowhere else
    if (out.get("state") == "done" and isinstance(content, list) and len(content) > 1
            and isinstance(content[1], dict) and content[1].get("type") == "image"):
        image = content[1]
    if image is not None:
        try:
            out["screenshot"] = str(screenshots.save(d, task_id, image))
        except screenshots.Unsaved as e:
            out["screenshot_save_error"] = e.reason
    elif out.get("state") == "done" and "screenshot_error" not in out:
        found = screenshots.find(d, task_id)  # the head sends the image only once
        if found is not None:
            out["screenshot"] = str(found)
    if args.json:
        print(json.dumps(out))
    else:
        print("\n".join(_task_lines(out)))
    return _WAIT_EXIT.get(out.get("state"), 1)


def _task_wait(args: argparse.Namespace) -> int:
    tool_args = {"task_id": args.task_id.lower(), "timeout_secs": args.timeout_secs}
    result = _task_call(args, tasks.WAIT, tool_args, args.timeout_secs + tasks.WAIT_SLACK)
    if isinstance(result, int):
        return result
    return _task_wait_output(args, result, state.state_dir())


def _task_cancel(args: argparse.Namespace) -> int:
    result = _task_call(args, tasks.CANCEL, {"task_id": args.task_id.lower()}, ipc.CALL_TIMEOUT)
    if isinstance(result, int):
        return result
    out = result["structuredContent"]
    if args.json:
        print(json.dumps(out))
    else:
        print(f"Task {out.get('task_id')}: {out.get('state')}.")
    return 0


def _wait_secs(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("an integer from 1 to 60") from None
    if not tasks.WAIT_MIN <= value <= tasks.WAIT_MAX:
        raise argparse.ArgumentTypeError("an integer from 1 to 60")
    return value


def _ensure(args: argparse.Namespace) -> int:
    from . import daemon
    d = state.state_dir()
    try:
        started = daemon.ensure(d)
    except daemon.NotReady:
        # Not an error for a watchdog: the person has not finished pairing yet.
        print(json.dumps({"running": False, "reason": "not_ready"}) if args.json
              else "Not paired or not signed in yet: nothing to start.")
        return 0
    except _bridge_errors() as e:
        if args.json:
            print(json.dumps({"running": False, "reason": "failed", "error": str(e)}))
        else:
            print(f"nf: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"running": True, "started": started}))
    else:
        print("Bridge started." if started else "Bridge already running.")
    return 0


def _daemon(args: argparse.Namespace) -> int:
    from . import daemon
    d = state.state_dir()
    try:
        if not daemon.SUPPORTED:
            raise daemon.Unsupported()
        if daemon.alive(d):
            raise daemon.AlreadyRunning("a bridge is already running for this state directory")
        if not args.detach:
            return daemon.run_foreground(d)
        daemon.ensure(d)
        pid = (daemon.alive(d) or {}).get("pid")
        if pid is None:
            raise ipc.BridgeDown("the bridge started but does not answer")
    except _bridge_errors() as e:
        return _failed(args, e)
    print(json.dumps({"running": True, "pid": pid}) if args.json
          else f"Bridge running (pid {pid}).")
    return 0


def _setup(args: argparse.Namespace) -> int:
    from . import daemon
    d = state.state_dir()
    try:
        result = daemon.setup(d, service=not args.no_service)
    except _bridge_errors() as e:
        return _failed(args, e)
    if args.json:
        print(json.dumps(result))
    else:
        started = "; bridge started" if result["started"] else ""
        print(f"Supervision: {result['supervision']}{started}.")
    return 0


def _positive(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("a number of seconds") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("a positive number of seconds")
    return value


def _unpair(args: argparse.Namespace) -> int:
    from . import daemon
    d = state.state_dir()
    try:
        with daemon.held(d):  # no bridge may serve the old pairing meanwhile
            unpaired = state.unpair(d)
    except (ipc.BridgeDown, OSError) as e:
        return _failed(args, e)
    if args.json:
        print(json.dumps({"unpaired": unpaired, "state_dir": str(d)}))
    elif unpaired:
        # The protocol has no way to tell the head, so the browser keeps
        # listing this agent; without the key it can no longer connect.
        print("Unpaired. Remove this agent in NevoFlux too.")
    else:
        print("Not paired.")
    return 0


def _reset(args: argparse.Namespace) -> int:
    from . import daemon
    d = state.state_dir()
    revoked: bool | None = None
    error = None
    try:
        with daemon.held(d):  # no bridge may start while the files go
            if not args.local_only:
                try:
                    revoked = auth.revoke_and_verify(d) or None
                except (auth.AccountError, state.UnsupportedState, OSError) as e:
                    revoked, error = False, str(e)
            keep = (state.ACCOUNT_FILE,) if revoked is False else ()
            removed, left = state.reset(d, keep=keep)
    except (ipc.BridgeDown, OSError) as e:
        return _failed(args, e)
    if args.json:
        out = {"revoked": revoked, "removed": removed, "left": left, "state_dir": str(d)}
        if error:
            out["error"] = error
        print(json.dumps(out))
    else:
        if revoked is False:
            print(f"Could not revoke the account token: {error}. Kept {state.ACCOUNT_FILE}: run "
                  f"`nf reset` again when the account service is reachable, or "
                  f"`nf reset --local-only` to delete it anyway (it then stays valid on the "
                  f"server until it expires).", file=sys.stderr)
        print(f"Removed {', '.join(removed)} from {d}." if removed else "Nothing to remove.")
        if left:
            print(f"Left {d} in place: it also holds {', '.join(left)}.")
    return 1 if revoked is False else 0


def _pair(args: argparse.Namespace) -> int:
    d = state.state_dir()
    try:
        block = pairing.pair(sys.stdin.read(), d)
    except (ValueError, OSError) as e:
        return _failed(args, e)
    _notify(d, "reload")
    if args.json:
        print(json.dumps({"paired": True, "relay": block.relay,
                          "channel": block.channel}))
    else:
        print(f"Paired with channel {block.channel} on {block.relay}.")
    return 0


def _auth_begin(args: argparse.Namespace) -> int:
    d = state.state_dir()
    try:
        if state.read_state(d, state.ACCOUNT_FILE) is not None:
            if not args.reset:
                print(json.dumps({"already_authorized": True}) if args.json
                      else "Already authorized. Use --reset to sign in again.")
                return 0
            auth.revoke_and_verify(d)
            (d / state.ACCOUNT_FILE).unlink(missing_ok=True)
            _notify(d, "reload")
        dc = auth.begin(d, auth.AccountClient(auth.account_url()))
    except (auth.AccountError, state.UnsupportedState, OSError) as e:
        return _failed(args, e)
    if args.json:
        print(json.dumps({
            "verification_uri": dc.verification_uri,
            "verification_uri_complete": dc.verification_uri_complete,
            "user_code": dc.user_code,
            "expires_in": dc.expires_in,
        }))
    else:
        print(f"Open {dc.verification_uri_complete} (or {dc.verification_uri} and enter "
              f"{dc.user_code}) and approve with your NevoFlux account. The code expires in "
              f"{dc.expires_in // 60} minutes; `nf status` reports when it is approved.")
    return 0


def _status(args: argparse.Namespace) -> int:
    d = state.state_dir()
    try:
        outcome = auth.poll_if_due(d)
        pending = state.read_state(d, state.AUTH_PENDING_FILE, auth.PENDING_FIELDS)
        paired = state.read_state(d, state.PAIRING_FILE, pairing.PAIRING_FIELDS) is not None
        authorized = state.read_state(d, state.ACCOUNT_FILE, auth.ACCOUNT_FIELDS) is not None
    except (state.UnsupportedState, OSError) as e:
        return _failed(args, e)
    out: dict = {"paired": paired, "auth": "ok" if authorized else "pending" if pending else "none"}
    if pending is not None:
        out["state"] = "awaiting_device_approval"
        out["user_code"] = pending["user_code"]
        out["verification_uri"] = pending["verification_uri"]
        out["verification_uri_complete"] = pending["verification_uri_complete"]
        out["expires_in"] = max(0, int(pending["expires_at"] - auth.now()))
        if outcome is not None and outcome.poll_error:
            out["poll_error"] = outcome.poll_error
    elif not paired:
        out["state"] = "not_paired"
    elif not authorized:
        out["state"] = "not_authorized"
    else:
        out["state"] = "ready"
    if out["state"] == "ready":
        _bridge_status(d, out, approved=outcome is not None and outcome.status == "approved")
    mode = state.supervision(d)
    if mode is not None:
        out["supervision"] = mode
    if outcome is not None and outcome.error:
        out["last_error"] = outcome.error
    if args.json:
        print(json.dumps({"state": out.pop("state"), **out}))
    else:
        text = _STATUS_TEXT.get(out["state"], "State: {state}").format(**out)
        if "last_error" in out:
            text += f" (last attempt: {out['last_error']})"
        if "poll_error" in out:
            text += f" ({out['poll_error']})"
        print(text)
    return 0


_STATUS_TEXT = {
    "awaiting_device_approval": "Waiting for approval: open {verification_uri_complete} "
                                "(code {user_code}, {expires_in}s left).",
    "not_paired": "Not paired: pipe the /pair-agent block into `nf pair --from-stdin`.",
    "not_authorized": "Not signed in: run `nf auth begin`.",
    "ready": "Paired and signed in.",
    "connecting": "Connecting to the browser...",
    "head_offline": "The browser is not online (NevoFlux is closed, this agent was paused in "
                    "the NevoFlux sidebar, or it was removed there).",
    "connected": "Connected to the browser.",
    "incompatible": "The browser speaks another agent protocol: upgrade nevoflux-muse.",
    "auth_revoked": "The sign-in is no longer valid: run `nf auth begin --reset`.",
    "account_mismatch": "Signed in with another NevoFlux account than the browser: run "
                        "`nf auth begin --reset`.",
    "relay_refused": "The relay refused the channel: pair again.",
    "error": "The bridge hit an unexpected error; see bridge.log.",
    "not_ready": "The bridge has no pairing or sign-in.",
    "bridge_down": "The bridge is not running: {bridge_error}",
}


def _bridge_status(d, out: dict, approved: bool) -> None:
    from . import daemon
    if not daemon.SUPPORTED:
        out["bridge"] = "unsupported on Windows"
        return
    try:
        if approved:
            _notify(d, "reload")  # a running bridge may still hold the old sign-in
        daemon.ensure(d)
        reply = ipc.request(d, {"op": "status"}, 5.0)
    except _bridge_errors() as e:
        out["state"], out["bridge_error"] = "bridge_down", str(e)
        return
    if not reply.get("ok"):
        out["state"], out["bridge_error"] = "bridge_down", "the bridge refused the status request"
        return
    out["state"] = reply.get("state", "error")
    out["bridge"] = {"pid": reply.get("pid"), "since": reply.get("since")}
    if reply.get("head"):
        out["head"] = reply["head"]
    if reply.get("last_error"):
        out["last_error"] = reply["last_error"]


_NEXT_STEPS = [
    "In NevoFlux, remove this agent (/unpair): the browser still lists it.",
    "Delete the Muse scheduled task that runs ensure.sh.",
]


def _install_root() -> Path | None:
    """Where setup.sh installed us: NF_INSTALL_ROOT, or the venv's parent if it holds
    installed_tag. None when nf was installed some other way."""
    env = os.environ.get("NF_INSTALL_ROOT")
    if env:
        root = Path(env)
    elif sys.prefix != sys.base_prefix:
        root = Path(sys.prefix).parent
    else:
        return None
    return root if (root / "installed_tag").exists() else None


def _points_into(link: Path, root: Path) -> bool:
    target = (link.parent / os.readlink(link)).resolve()
    return root.resolve() in target.parents


def _uninstall(args: argparse.Namespace) -> int:
    from . import daemon
    d = state.state_dir()
    steps: list[dict] = []

    def finish(ok: bool) -> int:
        if args.json:
            print(json.dumps({"ok": ok, "steps": steps,
                              "next_steps": _NEXT_STEPS if ok else []}))
        else:
            for s in steps:
                mark = "ok" if s["ok"] else "FAILED"
                detail = s.get("error") or s.get("skipped") or ""
                print(f"{s['step']}: {mark}{' — ' + detail if detail else ''}")
            print("\n".join(["Next:"] + [f"- {n}" for n in _NEXT_STEPS]) if ok else
                  "Uninstall stopped; fix the error above and run `nf uninstall` again.")
        return 0 if ok else 1

    revoked = False
    failed = False
    error = None
    try:
        with daemon.held(d):
            if daemon.SUPPORTED and state.supervision(d) == "systemd-user":
                daemon.remove_unit()
                steps.append({"step": "service", "ok": True})
            try:
                revoked = auth.revoke_and_verify(d)
            except (auth.AccountError, state.UnsupportedState, OSError) as e:
                failed, error = True, str(e)
            keep = (state.ACCOUNT_FILE,) if failed else ()
            removed, _ = state.reset(d, keep=keep)
    except (ipc.BridgeDown, OSError) as e:
        steps.append({"step": "stop", "ok": False, "error": str(e)})
        return finish(False)
    if failed:
        steps.append({"step": "revoke", "ok": False, "error": error})
        return finish(False)
    steps.append({"step": "revoke", "ok": True, "revoked": bool(revoked)})
    steps.append({"step": "state", "ok": True, "removed": removed})
    root = _install_root()
    if root is None:
        steps.append({"step": "install_root", "ok": True,
                      "skipped": "no setup.sh install found (no installed_tag)"})
    else:
        try:
            shutil.rmtree(root)
        except FileNotFoundError:
            pass
        except OSError as e:
            steps.append({"step": "install_root", "ok": False, "error": str(e)})
            return finish(False)
        steps.append({"step": "install_root", "ok": True, "path": str(root)})
    link = Path.home() / ".local" / "bin" / "nf"
    if root is not None and link.is_symlink() and _points_into(link, root):
        link.unlink()
        steps.append({"step": "link", "ok": True, "path": str(link)})
    else:
        steps.append({"step": "link", "ok": True, "skipped": "no link of ours"})
    return finish(True)


def _parser() -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
    """The parser, and the `call` subparser (parsed intermixed: see main)."""
    parser = argparse.ArgumentParser(prog="nf")
    sub = parser.add_subparsers(dest="command")

    def command(parent, name, run, summary):
        cmd = parent.add_parser(name, help=summary)
        cmd.add_argument("--json", action="store_true")
        cmd.set_defaults(run=run)
        return cmd

    daemon_cmd = command(sub, "daemon", _daemon, "run the bridge (Linux/macOS)")
    daemon_cmd.add_argument("--detach", action="store_true",
                            help="start it in the background and return")
    command(sub, "ensure", _ensure, "start the bridge unless it already runs")
    command(sub, "tools", _tools, "list the browser tools the head offers")
    call = command(sub, "call", _call, "call a browser tool")
    call.add_argument("tool")
    call.add_argument("pairs", nargs="*", metavar="key=value",
                      help="arguments; values that parse as JSON are JSON, others strings")
    call.add_argument("--args-stdin", action="store_true",
                      help="read the arguments as one JSON object from stdin")
    call.add_argument("--timeout", type=_positive, default=ipc.CALL_TIMEOUT,
                      help="seconds to wait for the result (default 120)")
    for name, summary in (("approve", "approve what NevoFlux asked about; print the outcome"),
                          ("deny", "refuse what NevoFlux asked about")):
        answer = command(sub, name, _answer, summary)
        answer.add_argument("id", help="the id from approval_required, or an approval_id "
                                       "from `nf task wait`")
        answer.add_argument("--timeout", type=_positive, default=ipc.CALL_TIMEOUT,
                            help="seconds to wait for the outcome (default 120)")
        answer.add_argument("--task", help="the task_id an approval_id belongs to; needed only "
                                           "when the bridge restarted since `nf task wait`")
        if name == "approve":
            answer.add_argument("--answer", help="your answer to a delegated task's question")
    command(sub, "pending", _pending, "list the approvals NevoFlux is waiting for")
    task_cmd = sub.add_parser("task", help="hand NevoFlux a whole task to do (delegation)")
    task_sub = task_cmd.add_subparsers(dest="task_command")
    start = command(task_sub, "start", _task_start, "start a delegated task; returns at once")
    start.add_argument("prompt", nargs="?", help="what to do (prefer --prompt-stdin)")
    start.add_argument("--prompt-stdin", action="store_true",
                       help="read the prompt from stdin (it may hold private text)")
    start.add_argument("--session",
                       help="continue a session that an earlier task of yours started")
    wait = command(task_sub, "wait", _task_wait,
                   "wait for news of a task: steps, approvals, the result")
    wait.add_argument("task_id")
    wait.add_argument("--timeout-secs", type=_wait_secs, default=tasks.WAIT_DEFAULT,
                      help="return after at most this many seconds, 1 to 60 (default 30)")
    cancel = command(task_sub, "cancel", _task_cancel, "stop a delegated task")
    cancel.add_argument("task_id")
    command(sub, "version", _version, "print client and protocol versions")
    pair_cmd = command(sub, "pair", _pair, "pair with a browser: pipe in the block from /pair-agent")
    pair_cmd.add_argument("--from-stdin", action="store_true", required=True,
                          help="read the pairing block from stdin (never from an argument)")
    auth_cmd = sub.add_parser("auth", help="sign in to the NevoFlux account")
    begin = command(auth_cmd.add_subparsers(dest="auth_command"), "begin", _auth_begin,
                    "start a device sign-in and return at once; `nf status` finishes it")
    begin.add_argument("--reset", action="store_true",
                       help="revoke the current sign-in first (to switch accounts)")
    setup_cmd = command(sub, "setup", _setup,
                        "choose how the bridge is kept running (setup.sh runs this)")
    setup_cmd.add_argument("--no-service", action="store_true",
                           help="do not use a systemd user unit; rely on the watchdog")
    command(sub, "status", _status, "where pairing and sign-in stand")
    command(sub, "unpair", _unpair, "forget the paired browser, keep the account login")
    command(sub, "uninstall", _uninstall,
            "revoke the sign-in, stop the bridge, remove nevoflux-muse")
    reset = command(sub, "reset", _reset,
                    "revoke the sign-in and forget everything (run before uninstalling)")
    reset.add_argument("--local-only", action="store_true",
                       help="delete local state without revoking on the server")
    return parser, call


def main(argv: list[str] | None = None) -> int:
    parser, call = _parser()
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["call"]:  # `nf call t a=1 --timeout 5 b=2`: options among the pairs
        args = call.parse_intermixed_args(argv[1:])
    else:
        args = parser.parse_args(argv)
    if not hasattr(args, "run"):
        parser.print_usage(sys.stderr)
        return 2
    try:
        return args.run(args)
    except Exception as e:  # noqa: BLE001 - last resort; str(e) could carry a secret
        name = type(e).__name__
        if getattr(args, "json", False):
            print(json.dumps({"error": f"unexpected {name}"}))
        else:
            print(f"nf: unexpected {name}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
