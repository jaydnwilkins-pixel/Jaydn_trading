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


@pytest.fixture(autouse=True)
def _no_real_saxo_files(monkeypatch, tmp_path):
    """Tests must never read or rotate the real Saxo sign-ins in etc/ (or
    write the real alert state in var/): every token and alert file path is
    redirected into the test's own temp directory."""
    from app.tools import saxo_live
    monkeypatch.setattr(saxo_live, "token_file",
                        lambda which: tmp_path / f"saxo_{which}_token.json")
    monkeypatch.setattr(saxo_live, "alert_state_file",
                        lambda which: tmp_path / f"saxo_{which}_alert.json")
    monkeypatch.setattr(saxo_live, "TOKEN_FILE",
                        tmp_path / "saxo_default_token.json")
