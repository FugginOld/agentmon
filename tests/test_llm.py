"""The OpenAI-compatible wrapper, exercised with a fake client (no SDK, no network)."""

import asyncio
import functools
import gc
import json
from types import SimpleNamespace as NS

import pytest

from agentmon import llm, paths, sink
from agentmon.tail import RunWatcher


def response(content="Hello there", tool_calls=None, usage=(10, 4, 3)):
    msg = NS(content=content, tool_calls=[NS(function=NS(name=n)) for n in (tool_calls or [])])
    u = NS(prompt_tokens=usage[0], completion_tokens=usage[1], prompt_tokens_details=NS(cached_tokens=usage[2]))
    return NS(choices=[NS(message=msg)], usage=u)


def make_client(create):
    return NS(api_key="sk-secret-123", base_url="https://api.deepseek.com",
              chat=NS(completions=NS(create=create)), models=NS(list=lambda: ["m"]))


def load(run_id):
    w = RunWatcher(paths.run_file(run_id))
    w.poll()
    return w.run


def test_calls_become_steps_with_cumulative_usage():
    replies = iter([response("Hi\nsecond line"), response(None, tool_calls=["search", "fetch"], usage=(20, 6, 0))])
    client = llm.watch(make_client(lambda **kw: next(replies)), run="r1", agent="coder", name="DeepSeek")
    messages = [{"role": "user", "content": "say hi"}]
    first = client.chat.completions.create(model="deepseek-chat", messages=messages)
    client.chat.completions.create(model="deepseek-chat", messages=messages)
    assert first.choices[0].message.content.startswith("Hi")  # the caller gets the real response back

    run = load("r1")
    assert run.title == "DeepSeek" and run.agents["coder"].name == "DeepSeek"
    steps = list(run.steps.values())
    assert [s.summary for s in steps] == ["Hi", "→ search, fetch"]
    assert steps[0].title == "deepseek-chat · say hi" and steps[0].tool == "LLM"
    assert steps[0].tokens["in"] == 10
    # usage is absolute: the second call must add to the first
    assert run.agent_tokens("coder") == {"in": 30, "out": 10, "cache_read": 3}


def test_other_attributes_pass_through():
    client = llm.watch(make_client(lambda **kw: response()), run="r2")
    assert client.api_key == "sk-secret-123" and client.models.list() == ["m"]


def test_errors_are_reraised_and_never_log_the_message():
    class ApiError(Exception):
        status_code = 401

    def boom(**kw):
        raise ApiError("Incorrect API key provided: sk-secret-123")

    client = llm.watch(make_client(boom), run="r3")
    with pytest.raises(ApiError):
        client.chat.completions.create(model="m", messages=[{"role": "user", "content": "x"}])
    step = next(iter(load("r3").steps.values()))
    assert step.status == "error" and step.summary == "ApiError 401"
    assert "sk-secret" not in paths.run_file("r3").read_text()


def test_stream_ends_step_when_exhausted_and_reads_usage():
    chunks = [NS(choices=[NS(delta=NS(content="Hel", tool_calls=None))], usage=None),
              NS(choices=[NS(delta=NS(content="lo", tool_calls=None))], usage=None),
              NS(choices=[], usage=NS(prompt_tokens=7, completion_tokens=2, prompt_tokens_details=None))]
    client = llm.watch(make_client(lambda **kw: iter(chunks)), run="r4", agent="a")
    stream = client.chat.completions.create(model="m", messages=[{"role": "user", "content": "q"}], stream=True)
    step = next(iter(load("r4").steps.values()))
    assert step.status == "running"  # nothing consumed yet
    assert [c for c in stream] == chunks
    run = load("r4")
    step = next(iter(run.steps.values()))
    assert step.status == "done" and step.summary == "Hello" and run.agent_tokens("a")["in"] == 7


def test_abandoned_stream_still_ends_the_step():
    client = llm.watch(make_client(lambda **kw: iter([NS(choices=[], usage=None)] * 3)), run="r5")
    stream = client.chat.completions.create(model="m", messages=[], stream=True)
    for _ in stream:
        break
    assert next(iter(load("r5").steps.values())).status == "running"  # the stream is still open
    stream.close()
    assert next(iter(load("r5").steps.values())).status == "done"


def test_garbage_collected_stream_ends_the_step():
    client = llm.watch(make_client(lambda **kw: iter([NS(choices=[], usage=None)] * 3)), run="r5b")
    stream = client.chat.completions.create(model="m", messages=[], stream=True)
    next(stream)
    del stream  # abandoned without close(): once collected, the step ends
    gc.collect()
    assert next(iter(load("r5b").steps.values())).status == "done"


def test_stream_is_a_real_iterator_and_context_manager():
    chunks = [NS(choices=[], usage=None), NS(choices=[], usage=None)]
    closed = []

    class Src:
        def __iter__(self):
            return iter(chunks)

        def close(self):
            closed.append(1)

    client = llm.watch(make_client(lambda **kw: Src()), run="r5c")
    with client.chat.completions.create(model="m", messages=[], stream=True) as stream:
        assert iter(stream) is stream and next(stream) is chunks[0] and next(stream) is chunks[1]
        with pytest.raises(StopIteration):
            next(stream)
    assert closed == [1] and next(iter(load("r5c").steps.values())).status == "done"


def test_non_list_messages_do_not_break_the_call():
    seen = []
    client = llm.watch(make_client(lambda **kw: seen.append(kw) or response()), run="r8")
    gen = ({"role": "user", "content": "x"} for _ in range(1))  # the SDK accepts any iterable
    assert client.chat.completions.create(model="m", messages=gen).usage.prompt_tokens == 10
    assert len(seen) == 1


def test_odd_usage_from_the_provider_never_reaches_the_caller():
    bad = NS(choices=[], usage=NS(prompt_tokens="lots", completion_tokens=None, prompt_tokens_details=None))
    client = llm.watch(make_client(lambda **kw: bad), run="r9")
    assert client.chat.completions.create(model="m", messages=[]) is bad


@pytest.mark.parametrize("keeps_wrapped", [True, False])
def test_async_client_behind_a_plain_def_wrapper(keeps_wrapped):
    """The OpenAI SDK decorates `async def create` with a plain-def wrapper, so iscoroutinefunction is False.

    With functools.wraps the gate unwraps it and passes the call through untouched. A wrapper that hides
    __wrapped__ is caught at runtime instead: the caller still gets the real result and no step is left running.
    """
    def decorate(fn):
        def wrapper(*a, **k):
            return fn(*a, **k)
        return functools.wraps(fn)(wrapper) if keeps_wrapped else wrapper

    async def acreate(**kw):
        if kw.get("fail"):
            raise RuntimeError("boom")
        return iter([]) if kw.get("stream") else response()

    client = llm.watch(make_client(decorate(acreate)), run="r10")

    async def go():
        ok = await client.chat.completions.create(model="m", messages=[])
        streamed = await client.chat.completions.create(model="m", messages=[], stream=True)  # must not raise TypeError
        with pytest.raises(RuntimeError):
            await client.chat.completions.create(model="m", messages=[], fail=True)
        return ok, streamed

    ok, streamed = asyncio.run(go())
    assert ok.usage.prompt_tokens == 10 and list(streamed) == []
    statuses = sorted(s.status for s in load("r10").steps.values())
    assert statuses == ([] if keeps_wrapped else ["done", "done", "error"])  # never a step stuck "running"


def test_client_context_manager_passes_through():
    class Ctx(NS):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.exited = True

    inner = Ctx(chat=NS(completions=NS(create=lambda **kw: response())))
    with llm.watch(inner, run="r11") as c:
        c.chat.completions.create(model="m", messages=[])
    assert inner.exited


def test_monitoring_failure_never_breaks_the_call(monkeypatch):
    client = llm.watch(make_client(lambda **kw: response()), run="r6")

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(sink, "append", broken)
    assert client.chat.completions.create(model="m", messages=[]).choices[0].message.content == "Hello there"


def test_async_clients_are_passed_through_unmonitored():
    async def acreate(**kw):
        return response()

    client = llm.watch(make_client(acreate), run="r7")
    assert asyncio.run(client.chat.completions.create(model="m", messages=[])).usage.prompt_tokens == 10
    assert load("r7").steps == {}


def test_watch_reuses_an_existing_run_and_logs_no_credentials():
    llm.watch(make_client(lambda **kw: response()), run="shared", agent="a")
    llm.watch(make_client(lambda **kw: response()), run="shared", agent="b")
    lines = [json.loads(x) for x in paths.run_file("shared").read_text().splitlines()]
    assert [e["type"] for e in lines] == ["run_start", "agent_start", "agent_start"]
    assert "sk-secret" not in paths.run_file("shared").read_text()


def test_odd_usage_in_a_stream_never_reaches_the_callers_loop():
    chunks = [NS(choices=[], usage=NS(prompt_tokens="lots", completion_tokens=None, prompt_tokens_details=None))]
    client = llm.watch(make_client(lambda **kw: iter(chunks)), run="r12")
    assert list(client.chat.completions.create(model="m", messages=[], stream=True)) == chunks


def test_stream_never_iterated_still_ends_when_collected():
    client = llm.watch(make_client(lambda **kw: iter([])), run="r13")
    stream = client.chat.completions.create(model="m", messages=[], stream=True)
    assert next(iter(load("r13").steps.values())).status == "running"
    del stream
    gc.collect()
    assert next(iter(load("r13").steps.values())).status == "done"


def test_watched_client_can_be_copied():
    import copy

    client = llm.watch(make_client(lambda **kw: response()), run="r14")
    assert copy.copy(client).api_key == "sk-secret-123"
