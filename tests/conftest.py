"""Suite-wide guards. The default provider is the Claude Agent SDK on the
Max login, so without this any test that reaches the real registry would
spend subscription quota and depend on this machine being signed in."""
import pytest


@pytest.fixture(autouse=True)
def _no_real_claude_sdk(monkeypatch):
    import claude_agent_sdk

    from app.providers import claude_sdk

    def blocked(*a, **k):
        raise claude_sdk.SdkError("offline test: real Claude SDK calls are "
                                  "blocked")
    monkeypatch.setattr(claude_agent_sdk, "query", blocked)
    # deterministic regardless of whether this machine is signed in
    monkeypatch.setattr(claude_sdk, "not_ready",
                        lambda home=None: "not signed in to Claude (test)")
