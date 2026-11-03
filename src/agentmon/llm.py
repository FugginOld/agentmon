"""Watch calls to any OpenAI-compatible chat API (OpenAI, DeepSeek, Kimi/Moonshot, Qwen/DashScope, Gemini...).

    from openai import OpenAI
    from agentmon.llm import watch

    client = watch(OpenAI(base_url="https://api.deepseek.com", api_key=KEY), run="nightly", agent="coder", name="DeepSeek")
    client.chat.completions.create(model="deepseek-chat", messages=[...])   # appears as a step with token counts

Duck-typed on purpose: the OpenAI SDK is never imported, so this works with any client shaped like it. Monitoring
never changes a call's result or error, and request headers and API keys are never read or logged. Async clients
are not supported (their calls pass through; at most a step without a stream is recorded). Streaming is monitored,
but token counts only show if the provider sends usage.
"""

from __future__ import annotations

import inspect
import time
import uuid
from typing import Any

from . import events, paths, sink
from .hooks.claude_code import _clip_json, _short


def _get(obj: Any, *names: str) -> Any:
    """First present attribute or dict key, so SDK objects and plain dicts both work."""
    for name in names:
        value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        if value is not None:
            return value
    return None


class _Watch:
    """Event emitter for one watched client; a monitoring failure is swallowed, never raised."""

    def __init__(self, run: str, agent: str, model: str | None) -> None:
        self.run, self.agent, self.model = run, agent, model
        self.totals = {"in": 0, "out": 0, "cache_read": 0}

    def emit(self, etype: str, **fields: Any) -> None:
        try:
            sink.append(events.make(etype, self.run, **fields))
        except Exception:  # noqa: BLE001
            pass

    def start(self, kwargs: dict) -> str:
        sid = f"llm-{uuid.uuid4().hex[:8]}"
        try:
            messages = kwargs.get("messages")  # the SDK accepts any iterable; only look at lists
            last = _get(messages[-1], "content") if isinstance(messages, (list, tuple)) and messages else None
            text = last if isinstance(last, str) else ""
            self.emit("step_start", step=sid, agent=self.agent, tool="LLM",
                      title=f"{kwargs.get('model') or 'model'} · {_short(text, 28)}".rstrip(" ·"), input=_clip_json(last))
            self.model = kwargs.get("model") or self.model
        except Exception:  # noqa: BLE001
            pass
        return sid

    def fail(self, sid: str, exc: BaseException) -> None:
        # Class and HTTP status only: provider error text can echo (part of) the API key.
        status = getattr(exc, "status_code", None)
        self.emit("step_end", step=sid, agent=self.agent, status="error",
                  summary=f"{type(exc).__name__}{f' {status}' if status else ''}")

    def finish(self, sid: str, text: str | None, tool_calls: list[str], usage: Any) -> None:
        try:
            self._finish(sid, text, tool_calls, usage)
        except Exception:  # noqa: BLE001 · odd provider output must not reach the caller
            pass

    def _finish(self, sid: str, text: str | None, tool_calls: list[str], usage: Any) -> None:
        summary = f"→ {', '.join(tool_calls)}" if tool_calls else (_short(text.strip().splitlines()[0], 48) if text and text.strip() else "ok")
        step_tokens = None
        if usage is not None:
            details = _get(usage, "prompt_tokens_details")
            step_tokens = {"in": int(_get(usage, "prompt_tokens") or 0), "out": int(_get(usage, "completion_tokens") or 0),
                           "cache_read": int((_get(details, "cached_tokens") if details is not None else 0) or 0)}
            for k, v in step_tokens.items():
                self.totals[k] += v
        self.emit("step_end", step=sid, agent=self.agent, status="done", summary=summary, tokens=step_tokens)
        if step_tokens:
            self.emit("usage", agent=self.agent, model=self.model, tokens=dict(self.totals))

    def observe(self, resp: Any) -> tuple[str | None, list[str]]:
        choices = _get(resp, "choices") or []
        message = _get(choices[0], "message") if choices else None
        calls = (_get(message, "tool_calls") if message is not None else None) or []
        names = [str(_get(_get(c, "function"), "name") or "tool") for c in calls]
        return (_get(message, "content") if message is not None else None), names


class _Stream:
    """Wraps a streamed response; the step ends when the stream is exhausted, closed, or fails."""

    def __init__(self, stream: Any, watch: _Watch, sid: str) -> None:
        self._s, self._w, self._sid = stream, watch, sid
        self._text: list[str] = []
        self._calls: list[str] = []
        self._usage: Any = None
        self._done = False
        self._it = None

    def _end(self) -> None:
        if not self._done:
            self._done = True
            self._w.finish(self._sid, "".join(self._text), self._calls, self._usage)

    def _observe(self, chunk: Any) -> None:
        try:
            self._usage = _get(chunk, "usage") or self._usage
            choices = _get(chunk, "choices") or []
            delta = _get(choices[0], "delta") if choices else None
            if delta is not None:
                if isinstance(_get(delta, "content"), str) and sum(map(len, self._text)) < 200:
                    self._text.append(_get(delta, "content"))
                for call in _get(delta, "tool_calls") or []:
                    name = _get(_get(call, "function"), "name")
                    if name and name not in self._calls:
                        self._calls.append(str(name))
        except Exception:  # noqa: BLE001 – never break the caller's stream
            pass

    def _gen(self):
        try:
            for chunk in self._s:
                self._observe(chunk)
                yield chunk
        except Exception as exc:
            if not self._done:
                self._done = True
                self._w.fail(self._sid, exc)
            raise
        finally:
            self._end()

    def __iter__(self):
        return self

    def __next__(self):
        if self._it is None:
            self._it = self._gen()
        return next(self._it)

    def __del__(self) -> None:  # abandoned without close(): the step still ends once the stream is collected
        try:
            self._end()
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        self._end()
        close = getattr(self._s, "close", None)
        if close:
            close()

    def __enter__(self):
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __getattr__(self, name: str) -> Any:
        if name == "_s":  # not set yet (copy/pickle): don't recurse
            raise AttributeError(name)
        return getattr(self._s, name)


async def _await(awaitable: Any, watch: _Watch, sid: str, stream: bool) -> Any:
    """Result of an async client that slipped past the ``async def`` check: the caller still gets it unchanged.

    shortcut: an async stream is returned as-is and its step closes without tokens, upgrade if async is needed.
    """
    try:
        resp = await awaitable
    except BaseException as exc:
        watch.fail(sid, exc)
        raise
    try:
        text, calls = (None, []) if stream else watch.observe(resp)
        watch.finish(sid, text, calls, None if stream else _get(resp, "usage"))
    except Exception:  # noqa: BLE001
        pass
    return resp


class _Proxy:
    """Passes everything through to the client except ``chat.completions.create``."""

    def __init__(self, target: Any, watch: _Watch, path: tuple[str, ...] = ()) -> None:
        self._t, self._w, self._p = target, watch, path

    def __getattr__(self, name: str) -> Any:
        if name in ("_t", "_w", "_p"):  # not set yet (copy/pickle): don't recurse
            raise AttributeError(name)
        value = getattr(self._t, name)
        path = self._p + (name,)
        if path in (("chat",), ("chat", "completions")):
            return _Proxy(value, self._w, path)
        if path == ("chat", "completions", "create") and not inspect.iscoroutinefunction(inspect.unwrap(value)):
            return self._wrap(value)
        return value

    def __enter__(self):
        self._t.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._t.__exit__(*exc)

    def _wrap(self, create):
        watch = self._w

        def call(*args: Any, **kwargs: Any):
            sid = watch.start(kwargs)
            try:
                resp = create(*args, **kwargs)
            except BaseException as exc:
                watch.fail(sid, exc)
                raise
            if inspect.isawaitable(resp):
                return _await(resp, watch, sid, bool(kwargs.get("stream")))
            if kwargs.get("stream"):
                return _Stream(resp, watch, sid)
            try:
                text, calls = watch.observe(resp)
                watch.finish(sid, text, calls, _get(resp, "usage"))
            except Exception:  # noqa: BLE001
                pass
            return resp

        return call


def watch(client: Any, run: str | None = None, agent: str = "llm", name: str | None = None,
          model: str | None = None) -> Any:
    """Return ``client`` wrapped so every ``chat.completions.create`` call shows up in agentmon.

    ``run`` groups calls into one run (default: a new one per ``watch()``); ``agent`` is the lane,
    ``name`` its label (for example "DeepSeek").
    """
    run = run or f"llm-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    w = _Watch(run, agent, model)
    try:
        if not paths.run_file(run).exists():
            w.emit("run_start", title=name or agent, source="llm")
        w.emit("agent_start", agent=agent, name=name or agent, model=model)
    except Exception:  # noqa: BLE001
        pass
    return _Proxy(client, w)
