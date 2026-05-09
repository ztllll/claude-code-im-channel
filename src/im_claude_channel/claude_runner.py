"""Run a claude CLI subprocess and stream events back to the daemon.

Two modes are exposed:

- :func:`run` — legacy one-shot wrapper around `claude -p --output-format json`.
  Kept for ad-hoc scripts; new code should prefer :func:`run_stream`.
- :func:`run_stream` — streams `claude -p --output-format stream-json --verbose`
  line-by-line and invokes ``on_event`` for every NDJSON record. The daemon
  uses this to push live progress updates into a Feishu card while the
  underlying turn (which can run for many minutes when claude loops through
  Read/Edit/Bash/...) is still in flight.

Stream-json shape (one JSON object per line):

  {"type":"system","subtype":"init","session_id":"...","tools":[...],...}
  {"type":"assistant","message":{"content":[{"type":"thinking",...}]},...}
  {"type":"assistant","message":{"content":[{"type":"text","text":"..."}]},...}
  {"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"file_path":"..."}}]},...}
  {"type":"user","message":{"content":[{"type":"tool_result",...}]},...}
  {"type":"result","subtype":"success","result":"<final assistant text>","session_id":"...",...}
"""

from __future__ import annotations

import json
import logging
import selectors
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import ClaudeConfig

log = logging.getLogger(__name__)


@dataclass
class ClaudeResult:
    text: str
    session_id: str | None
    is_error: bool
    raw: dict


class ClaudeRunError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Legacy one-shot mode (kept so older callers keep compiling).
# ---------------------------------------------------------------------------


def run(
    prompt: str,
    cfg: ClaudeConfig,
    *,
    resume_session_id: str | None = None,
) -> ClaudeResult:
    """Spawn `claude -p --output-format json` and return the parsed result."""
    cmd: list[str] = [cfg.binary, "-p", "--output-format", "json"]
    cmd.extend(cfg.extra_args)
    if resume_session_id:
        cmd.extend(["--resume", resume_session_id])

    work_dir = cfg.work_dir or None
    if work_dir:
        Path(work_dir).mkdir(parents=True, exist_ok=True)

    log.info("claude_runner: cmd=%s resume=%s cwd=%s", cmd[:3] + ["..."], resume_session_id, work_dir)

    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=cfg.timeout_seconds,
            cwd=work_dir,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise ClaudeRunError(f"claude CLI timed out after {cfg.timeout_seconds}s") from e

    if proc.returncode != 0:
        raise ClaudeRunError(
            f"claude CLI exited {proc.returncode}\nstderr: {proc.stderr[:500]}"
        )

    raw = (proc.stdout or "").strip()
    if not raw:
        raise ClaudeRunError(f"claude CLI returned empty output. stderr: {proc.stderr[:500]}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ClaudeRunError(f"claude CLI returned non-JSON output: {raw[:500]}") from e

    text = (data.get("result") or "").strip() or "(empty)"
    session_id = data.get("session_id")
    is_error = bool(data.get("is_error"))

    if is_error:
        log.warning("claude returned is_error=true: %s", data.get("subtype") or data)

    return ClaudeResult(text=text, session_id=session_id, is_error=is_error, raw=data)


# ---------------------------------------------------------------------------
# Stream mode — line-by-line NDJSON with progress callback.
# ---------------------------------------------------------------------------


_HEARTBEAT_INTERVAL = 5.0  # synthetic UI heartbeat when stdout is quiet


def run_stream(
    prompt: str,
    cfg: ClaudeConfig,
    *,
    resume_session_id: str | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> ClaudeResult:
    """Run claude in stream-json mode and pipe events to ``on_event``.

    Two timeouts cooperate:

    - ``cfg.timeout_seconds`` — total wall-clock cap for the whole turn. Set
      generously (default 7200s); long real tool loops are legitimate.
    - ``cfg.idle_timeout_seconds`` — kill the subprocess if no claude event
      shows up for this long. Real tool loops emit events every few seconds;
      sustained silence is almost always a stuck upstream API call.

    A synthetic ``{"type": "heartbeat", ...}`` event is injected every 5s of
    stdout silence so the UI can keep refreshing the elapsed counter without
    touching the idle timer.
    """
    cmd: list[str] = [cfg.binary, "-p", "--output-format", "stream-json", "--verbose"]
    cmd.extend(cfg.extra_args)
    if resume_session_id:
        cmd.extend(["--resume", resume_session_id])

    work_dir = cfg.work_dir or None
    if work_dir:
        Path(work_dir).mkdir(parents=True, exist_ok=True)

    log.info(
        "claude_runner: stream cmd=%s resume=%s cwd=%s total=%ss idle=%ss",
        cmd[:5] + ["..."], resume_session_id, work_dir,
        cfg.timeout_seconds, cfg.idle_timeout_seconds,
    )

    proc = subprocess.Popen(  # noqa: S603 - cmd is constructed from trusted config
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=work_dir,
        bufsize=1,
    )

    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except BrokenPipeError as e:
        proc.kill()
        raise ClaudeRunError("claude CLI closed stdin prematurely") from e

    start = time.monotonic()
    total_deadline = start + cfg.timeout_seconds
    idle_seconds = max(30, int(cfg.idle_timeout_seconds))
    last_event_ts = start
    last_heartbeat_ts = start

    final_event: dict | None = None
    last_session_id: str | None = None
    last_text: str = ""

    def _emit(ev: dict) -> None:
        if on_event is None:
            return
        try:
            on_event(ev)
        except Exception:  # noqa: BLE001 — UI failures must not abort the run
            log.exception("on_event handler raised; continuing")

    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)

    try:
        while True:
            now = time.monotonic()

            if now > total_deadline:
                proc.kill()
                raise ClaudeRunError(
                    f"claude CLI exceeded total timeout {cfg.timeout_seconds}s"
                )
            if now - last_event_ts > idle_seconds:
                proc.kill()
                raise ClaudeRunError(
                    f"claude CLI idle for {idle_seconds}s "
                    f"(no events from upstream — likely stuck network)"
                )

            # Wake at whichever boundary comes first: next heartbeat tick,
            # idle deadline, or total deadline. Cap at 5s so we always re-check.
            poll_until = min(
                last_heartbeat_ts + _HEARTBEAT_INTERVAL,
                last_event_ts + idle_seconds,
                total_deadline,
            )
            poll_timeout = max(0.05, min(5.0, poll_until - now))

            ready = sel.select(timeout=poll_timeout)
            now = time.monotonic()

            if not ready:
                # No stdout activity. Emit a heartbeat so the UI shows
                # "still alive" without resetting the idle timer.
                if now - last_heartbeat_ts >= _HEARTBEAT_INTERVAL:
                    _emit({"type": "heartbeat", "_elapsed": round(now - start, 1)})
                    last_heartbeat_ts = now
                continue

            line = proc.stdout.readline()
            if not line:
                # readline returns "" only on EOF.
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
                break

            line = line.strip()
            if not line:
                continue

            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                log.warning("non-json stream line skipped: %s", line[:200])
                continue

            now = time.monotonic()
            ev["_elapsed"] = round(now - start, 1)
            last_event_ts = now
            last_heartbeat_ts = now  # real event fully replaces heartbeat tick

            sid = ev.get("session_id")
            if sid:
                last_session_id = sid

            is_terminal = ev.get("type") == "result"
            if is_terminal:
                final_event = ev
                last_text = (ev.get("result") or "").strip() or last_text

            _emit(ev)

            if is_terminal:
                # claude binary sometimes keeps stdout open for a long time
                # after the result event (cache writes, MCP cleanup, etc).
                # Don't wait for EOF — the result event is the contract.
                log.info(
                    "claude_runner: result received at %.1fs, exiting stream loop",
                    ev.get("_elapsed", 0),
                )
                break
    finally:
        try:
            sel.close()
        except Exception:  # noqa: BLE001
            pass
        if proc.stdout:
            try:
                proc.stdout.close()
            except OSError:
                pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    stderr = ""
    if proc.stderr:
        try:
            stderr = proc.stderr.read() or ""
        except OSError:
            stderr = ""

    if final_event is None:
        # No result event ever arrived. Either claude crashed or we killed it
        # before it could finish. The returncode tells us which.
        if proc.returncode and proc.returncode != 0:
            raise ClaudeRunError(
                f"claude CLI exited {proc.returncode} before sending result. "
                f"stderr: {stderr[:500]}"
            )
        raise ClaudeRunError(
            "claude CLI ended without a `result` event. "
            f"stderr: {stderr[:300]}"
        )

    # We have a final result. The subprocess may exit non-zero because we
    # SIGTERM'd it after the result event (claude often keeps stdout open
    # doing cache writes / MCP cleanup, and we don't want to wait). That's
    # not a failure — the business contract is the `result` event.
    if proc.returncode and proc.returncode not in (0, -15, 143):
        log.warning(
            "claude CLI exited %s after delivering result; stderr: %s",
            proc.returncode, stderr[:200],
        )

    is_error = bool(final_event.get("is_error"))
    if is_error:
        log.warning(
            "claude returned is_error=true: %s",
            final_event.get("subtype") or final_event.get("error") or "<no detail>",
        )

    return ClaudeResult(
        text=last_text or "(empty)",
        session_id=last_session_id,
        is_error=is_error,
        raw=final_event,
    )
