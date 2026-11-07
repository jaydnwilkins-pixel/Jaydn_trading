"""Every run records who started it, so the Trading page never shows a
blank "started by" (2026-11-06: nine runs had no recorded source)."""
import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.graph import ronathan_agent
from app.graph.ronathan_agent import asker_for


class Desk:
    def __init__(self):
        self.calls = []

    def start_run(self, inst, kind="pipeline", *, background=True,
                  trigger=None):
        self.calls.append((inst.ticker, kind, trigger))
        return "wi_x"


@pytest.mark.parametrize("text,thread,who", [
    ("[from Bob] research NVDA", "main", "Bob, via Ronathan's chat"),
    ("  [from Bob] hi", "dashboard", "Bob, via Ronathan's chat"),
    ("research NVDA", "dashboard", "you, via Ronathan's chat on the dashboard"),
    ("research NVDA", "main",
     "Ronathan's chat (/ask: voice or another assistant)"),
])
def test_chat_asker(text, thread, who):
    assert asker_for(text, thread) == who


def test_ronathan_run_tool_records_the_asker(monkeypatch):
    desk = Desk()
    tools = {t.name: t for t in ronathan_agent.build_tools(None, None, desk,
                                                          None)}
    monkeypatch.setitem(ronathan_agent.CURRENT_ASKER, "who",
                        "Bob, via Ronathan's chat")
    tools["run_pipeline"].invoke({"ticker": "NVDA", "exchange": "NASDAQ"})
    assert desk.calls[-1][2] == "chat: Bob, via Ronathan's chat"


def test_api_run_without_a_source_is_still_recorded():
    desk = Desk()
    client = TestClient(create_app(desk, None, "sec"))
    body = {"ticker": "NVDA", "exchange": "NASDAQ", "currency": "USD"}
    assert client.post("/api/run", json=body).status_code == 200
    assert "no source given" in desk.calls[-1][2]
    client.post("/api/run", json={**body,
                                  "trigger": "launched from the Trading page"})
    assert desk.calls[-1][2] == "launched from the Trading page"
