import pytest


@pytest.fixture(autouse=True)
def assume_online(request, monkeypatch):
    """Tests don't depend on the real connection; the ones about a lost
    connection pass their own check to the job. Live tests use the real one."""
    if request.node.get_closest_marker("live"):
        return
    from slack_to_zotero import http_fetch
    monkeypatch.setattr(http_fetch, "online", lambda *a, **k: True)
