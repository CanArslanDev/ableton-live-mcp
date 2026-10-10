"""The dataset question is only asked when collection could actually happen."""

from MCP_Server.dataset import consent


def _fresh_state(tmp_path, monkeypatch):
    monkeypatch.setattr(consent, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(consent, "_STATE_FILE", tmp_path / "consent.json")
    consent.reset_for_tests()


def test_no_question_without_telemetry(tmp_path, monkeypatch):
    _fresh_state(tmp_path, monkeypatch)
    monkeypatch.setattr("MCP_Server.telemetry.is_telemetry_enabled", lambda: False)
    assert consent.needs_prompt()
    assert not consent.may_ask_now()
    assert consent.maybe_consent_notice() == ""


def test_question_when_collection_is_possible(tmp_path, monkeypatch):
    _fresh_state(tmp_path, monkeypatch)
    monkeypatch.setattr("MCP_Server.telemetry.is_telemetry_enabled", lambda: True)
    assert consent.may_ask_now()
