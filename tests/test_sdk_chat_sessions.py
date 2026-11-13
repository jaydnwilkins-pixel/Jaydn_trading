"""Ronathan's chat on the Claude SDK is a real multi-turn session (B8): each
turn resumes the thread's session and sends only the new message, instead of
flattening the whole history into one prompt. chat_threads stays the record.
Offline: scripted conversations via `_query`."""
import json

import claude_agent_sdk as sdk
import pytest

from app.graph import ronathan_agent
from app.graph.ronathan_agent import Ronathan
from app.providers import claude_sdk
from tests.test_claude_sdk import conn, result  # noqa: F401 — fixture

REFUSAL = "API Error: Opus 5's safeguards flagged this message"


class Desk:
    """A scripted SDK: answers every conversation with "answer N" in a
    session of its own ("s1", "s2", …; a resumed one keeps its id), unless
    `fail(prompt, options)` returns something to do instead — an exception
    to raise, or a ResultMessage to end on."""

    def __init__(self, fail=None):
        self.calls, self.fail, self.sessions = [], fail, 0

    @property
    def seen(self):
        return [(o.model, o.resume, p) for p, o in self.calls]

    def __call__(self, *, prompt, options):
        self.calls.append((prompt, options))
        bad = self.fail(prompt, options) if self.fail else None
        n = len(self.calls)

        async def gen():
            if isinstance(bad, Exception):
                raise bad
            if bad is not None:
                yield bad
                return
            if not options.resume:
                self.sessions += 1
            sid = options.resume or f"s{self.sessions}"
            yield sdk.AssistantMessage(
                content=[sdk.TextBlock(text=f"answer {n}")], model="m")
            yield result(result=f"answer {n}", session_id=sid)
        return gen()


def make_ronathan(conn, q, *, fallback="claude-sonnet-5"):
    ronathan = Ronathan(conn, repo=None, desk=None, market=None)
    ronathan._ensure_agent()
    _, system, tools = ronathan._sdk_chat
    ronathan._sdk_chat = (claude_sdk.SdkChat("claude-opus-5", _query=q,
                                           fallback_model=fallback),
                        system, tools)
    return ronathan


def ask(ronathan, text, thread="t"):
    return [json.loads(x) for x in ronathan.ask_stream(text, thread)]


def saved(conn, thread="t"):
    row = conn.execute("SELECT messages FROM chat_threads WHERE thread=?",
                       (thread,)).fetchone()
    return json.loads(row[0]) if row else None


def texts(out):
    return [o["text"] for o in out if o["kind"] == "text"]


def errors(out):
    return [o for o in out if o["kind"] == "error"]


OLD = [{"role": "user", "content": "old question"},
       {"role": "assistant", "content": "old answer"}]


# ── the normal case ──────────────────────────────────────────────────────
def test_each_turn_resumes_the_session_and_sends_only_the_new_message(conn):
    q = Desk()
    ronathan = make_ronathan(conn, q)
    ask(ronathan, "what do I hold?")
    ask(ronathan, "and how much cash?")
    out = ask(ronathan, "thanks")
    assert q.seen == [("claude-opus-5", None, "what do I hold?"),
                      ("claude-opus-5", "s1", "and how much cash?"),
                      ("claude-opus-5", "s1", "thanks")]
    assert texts(out) == ["answer 3"] and out[-1] == {"kind": "done"}
    # chat_threads is still the whole conversation
    assert saved(conn) == [
        {"role": "user", "content": "what do I hold?"},
        {"role": "assistant", "content": "answer 1"},
        {"role": "user", "content": "and how much cash?"},
        {"role": "assistant", "content": "answer 2"},
        {"role": "user", "content": "thanks"},
        {"role": "assistant", "content": "answer 3"}]


def test_the_session_to_resume_is_the_one_the_last_turn_ended_in(conn):
    async def q(*, prompt, options):
        # a CLI that gives a resumed conversation a new id each time
        yield result(result="ok", session_id=f"after {prompt}")
    ronathan = make_ronathan(conn, q)
    ask(ronathan, "one")
    assert ronathan._sessions == {"t": "after one"}
    ask(ronathan, "two")
    assert ronathan._sessions == {"t": "after two"}


def test_threads_have_separate_sessions(conn):
    q = Desk()
    ronathan = make_ronathan(conn, q)
    ask(ronathan, "hello", "dashboard")
    ask(ronathan, "hi", "main")
    ask(ronathan, "again", "dashboard")
    assert [r for _, r, _ in q.seen] == [None, None, "s1"]
    assert ronathan._sessions == {"dashboard": "s1", "main": "s2"}


def test_sessions_stay_isolated_in_the_work_dir(conn):
    q = Desk()
    ronathan = make_ronathan(conn, q)
    ask(ronathan, "one")
    ask(ronathan, "two")
    o = q.calls[1][1]
    assert o.resume == "s1" and o.cwd == str(claude_sdk.WORK_DIR)
    assert o.setting_sources == [] and o.tools == [] and o.strict_mcp_config
    assert o.env["ANTHROPIC_API_KEY"] == ""
    assert not o.continue_conversation and o.session_store is None


# ── no session to resume ─────────────────────────────────────────────────
def test_after_a_restart_a_new_session_starts_from_a_short_recap(conn):
    q = Desk()
    first = make_ronathan(conn, q)
    first._histories["t"] = [
        {"role": "user" if i % 2 == 0 else "assistant",
         "content": f"turn {i} " + "blah " * 2000} for i in range(40)]
    first._save("t")
    ronathan = make_ronathan(conn, q)                   # "restart": ids are gone
    out = ask(ronathan, "what changed today?")
    [(model, resume, prompt)] = q.seen
    assert resume is None and not errors(out)
    assert prompt.endswith("Jaydn's new message:\nwhat changed today?")
    assert "turn 39 blah" in prompt and "turn 34 blah" in prompt
    assert "turn 33" not in prompt                  # the last six only
    assert len(prompt) < 3500                       # not 40 × 10k characters
    assert "Conversation so far" not in prompt
    assert len(saved(conn)) == 42                   # the record is untouched
    assert ask(ronathan, "and?") and q.seen[-1][1:] == ("s1", "and?")


@pytest.mark.parametrize("failure", [
    sdk.ProcessError("Command failed", exit_code=1,
                     stderr="No conversation found with session ID: s1"),
    result(subtype="error_during_execution", is_error=True, session_id="",
           errors=["No conversation found with session ID: s1"]),
], ids=["cli exits", "error result"])
def test_a_session_that_cannot_be_resumed_falls_back_to_a_recap(conn, failure,
                                                                caplog):
    q = Desk(fail=lambda p, o: failure if o.resume == "s1" else None)
    ronathan = make_ronathan(conn, q)
    ask(ronathan, "what do I hold?")
    out = ask(ronathan, "and how much cash?")
    assert not errors(out) and texts(out) == ["answer 3"]
    (_, r2, p2), (m3, r3, p3) = q.seen[1:]
    assert (r2, p2) == ("s1", "and how much cash?")
    assert (m3, r3) == ("claude-opus-5", None)      # same model, new session
    assert "Jaydn asked: what do I hold?" in p3
    assert "You answered: answer 1" in p3
    assert p3.endswith("Jaydn's new message:\nand how much cash?")
    assert len(saved(conn)) == 4 and ronathan._sessions == {"t": "s2"}
    assert "could not be resumed" in caplog.text
    ask(ronathan, "thanks")
    assert q.seen[-1][1:] == ("s2", "thanks")


# ── refusals ─────────────────────────────────────────────────────────────
def refused():
    return result(is_error=True, result=REFUSAL, session_id="poisoned")


def test_a_refused_session_is_never_resumed_again(conn):
    q = Desk(fail=lambda p, o: refused() if o.resume == "s1" else None)
    ronathan = make_ronathan(conn, q)
    ask(ronathan, "what do I hold?")
    out = ask(ronathan, "tell me about the stop")
    assert not errors(out) and texts(out) == ["answer 3"]
    assert [r for _, r, _ in q.seen] == [None, "s1", None]
    assert q.seen[2][0] == "claude-opus-5"          # a recap, same model
    assert "You answered: answer 1" in q.seen[2][2]
    assert len(saved(conn)) == 4                    # nothing was dropped
    ask(ronathan, "thanks")
    assert q.seen[-1][1:] == ("s2", "thanks")       # the new one, not s1


def test_a_refused_recap_goes_to_the_backup_model_then_starts_fresh(conn):
    q = Desk(fail=lambda p, o: refused() if "old question" in p else None)
    ronathan = make_ronathan(conn, q)
    ronathan._histories["t"] = list(OLD)
    out = ask(ronathan, "status?")
    assert [(m, r) for m, r, _ in q.seen] == [
        ("claude-opus-5", None), ("claude-sonnet-5", None),
        ("claude-sonnet-5", None)]
    assert q.seen[-1][2] == "status?"               # only the question
    assert any("fresh conversation" in t for t in texts(out))
    assert saved(conn) == [{"role": "user", "content": "status?"},
                           {"role": "assistant", "content": "answer 3"}]
    ask(ronathan, "thanks")
    assert q.seen[-1][1:] == ("s1", "thanks")       # and it carries on there


def test_a_refused_first_message_is_not_sent_twice_the_same_way(conn):
    q = Desk(fail=lambda p, o: refused())
    ronathan = make_ronathan(conn, q)
    out = ask(ronathan, "hello")
    # no history, so "fresh" would repeat the backup attempt word for word
    assert q.seen == [("claude-opus-5", None, "hello"),
                      ("claude-sonnet-5", None, "hello")]
    assert errors(out) and "safeguards flagged" in errors(out)[0]["text"]
    assert ronathan._histories["t"] == [] and ronathan._sessions == {}


def test_without_a_backup_model_a_refusal_still_ends_in_a_fresh_start(conn):
    q = Desk(fail=lambda p, o: refused() if "old question" in p else None)
    ronathan = make_ronathan(conn, q, fallback=None)
    ronathan._histories["t"] = list(OLD)
    out = ask(ronathan, "status?")
    assert [(m, r) for m, r, _ in q.seen] == [("claude-opus-5", None)] * 2
    assert q.seen[-1][2] == "status?" and not errors(out)


# ── turns that fail ──────────────────────────────────────────────────────
def test_a_turn_that_fails_half_way_gives_up_its_session(conn):
    state = {"break": False}

    def q(*, prompt, options):
        calls.append((options.resume, prompt))

        async def gen():
            yield sdk.AssistantMessage(
                content=[sdk.TextBlock(text="Let me look.")], model="m")
            if state["break"]:
                raise RuntimeError("connection reset")
            yield result(result="done", session_id="s1")
        return gen()
    calls = []
    ronathan = make_ronathan(conn, q)
    ask(ronathan, "what do I hold?")
    state["break"] = True
    out = ask(ronathan, "and cash?")
    assert errors(out) and "connection reset" in errors(out)[0]["text"]
    assert len(calls) == 2                          # half-said: not retried
    assert len(ronathan._histories["t"]) == 2         # no unanswered turn kept
    assert ronathan._sessions == {}
    # the session now holds a turn the record does not: start from the record
    state["break"] = False
    ask(ronathan, "and cash?")
    resume, prompt = calls[-1]
    assert resume is None and "Jaydn asked: what do I hold?" in prompt
    assert "and cash?" in prompt and len(saved(conn)) == 4


def test_a_timed_out_turn_is_not_retried(conn, monkeypatch):
    calls = []

    def slow(chat, system, prompt, tools, **kw):
        calls.append(kw.get("resume"))
        raise claude_sdk.SdkTimeout("Claude SDK chat turn timed out")
        yield  # noqa — generator
    q = Desk()
    ronathan = make_ronathan(conn, q)
    ask(ronathan, "one")
    monkeypatch.setattr(claude_sdk, "stream_chat", slow)
    out = ask(ronathan, "two")
    assert calls == ["s1"] and "timed out" in errors(out)[0]["text"]
    assert ronathan._sessions == {} and len(ronathan._histories["t"]) == 2


def test_other_errors_on_a_new_session_are_final(conn):
    q = Desk(fail=lambda p, o: RuntimeError("network down"))
    ronathan = make_ronathan(conn, q)
    out = ask(ronathan, "hi")
    assert len(q.calls) == 1 and "network down" in errors(out)[0]["text"]


def test_a_reader_who_hangs_up_leaves_no_half_turn_behind(conn):
    q = Desk()
    ronathan = make_ronathan(conn, q)
    ask(ronathan, "one")
    stream = ronathan.ask_stream("two", "t")
    assert json.loads(next(stream))["text"] == "answer 2"
    stream.close()                                  # the client went away
    assert ronathan._histories["t"] == saved(conn) and len(saved(conn)) == 2
    assert ronathan._sessions == {}                   # s1 may now hold "two"
    ask(ronathan, "three")
    assert q.seen[-1][1] is None and "You answered: answer 1" in q.seen[-1][2]


def test_the_auto_reset_starts_a_new_session_too(conn, monkeypatch):
    monkeypatch.setattr(ronathan_agent, "MAX_TURNS_PER_THREAD", 1)
    q = Desk()
    ronathan = make_ronathan(conn, q)
    ask(ronathan, "one")
    ask(ronathan, "two")                              # 4 messages: over 1 × 2
    ask(ronathan, "three")
    assert q.seen == [("claude-opus-5", None, "one"),
                      ("claude-opus-5", "s1", "two"),
                      ("claude-opus-5", None, "three")]
    assert len(saved(conn)) == 2


# ── the pieces ───────────────────────────────────────────────────────────
def test_recap_prompt_is_short_and_ends_with_the_new_message():
    assert claude_sdk.recap_prompt([], "hello") == "hello"
    assert claude_sdk.recap_prompt([("assistant", "  ")], "hello") == "hello"
    turns = [("user", "q1"), ("assistant", "a1\n\n| a | table |\n" + "x" * 900),
             ("user", "q2"), ("assistant", "a2")]
    p = claude_sdk.recap_prompt(turns, "now what?", max_turns=3)
    assert "q1" not in p                            # only the last three
    assert "- You answered: a1 | a | table | xxx" in p      # one line each
    assert "…\n- Jaydn asked: q2\n- You answered: a2\n" in p
    assert p.endswith("\n\nJaydn's new message:\nnow what?")
    assert max(len(line) for line in p.splitlines()) < 450


def test_stream_chat_resumes_and_reports_the_session():
    seen = []

    async def q(*, prompt, options):
        seen.append((prompt, options.resume))
        yield sdk.AssistantMessage(content=[sdk.TextBlock(text="hi")],
                                   model="m")
        yield result(result="hi", session_id="abc")
    chat = claude_sdk.SdkChat("m", _query=q)
    assert list(claude_sdk.stream_chat(chat, "S", "hello", [],
                                       resume="abc")) == [
        ("text", "hi"), ("session", "abc"), ("done", "hi")]
    assert seen == [("hello", "abc")]
    assert claude_sdk.base_options(model="m").resume is None


def test_the_session_id_survives_a_restart(conn):
    """It is kept in chat_threads.sdk_session, so a new process resumes the
    conversation instead of spending a recap turn on every thread."""
    from app.graph.ronathan_agent import _Sessions
    conn.execute("INSERT INTO chat_threads (thread, messages, updated_at)"
                 " VALUES ('t', '[]', 1)")
    s = _Sessions(conn)
    assert "t" not in s and s == {}
    s["t"] = "sess-1"
    again = _Sessions(conn)                     # "after a restart"
    assert again["t"] == "sess-1" and again == {"t": "sess-1"}
    assert again.pop("t") == "sess-1"           # a turn starts: forgotten...
    assert _Sessions(conn) == {} and again.pop("t", None) is None
    assert again.pop("never-seen", "x") == "x"
    # saving a thread's messages does not wipe the id
    s["t"] = "sess-2"
    conn.execute("INSERT INTO chat_threads (thread, messages, updated_at)"
                 " VALUES ('t', '[1]', 2) ON CONFLICT(thread) DO UPDATE SET"
                 " messages=excluded.messages, updated_at=excluded.updated_at")
    assert s["t"] == "sess-2"
