import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def isolated_notifications(tmp_path, monkeypatch):
    """Ogni test scrive gli avvisi al titolare in un file temporaneo, mai in notifiche.json vero."""
    import app as webapp
    from notifications import NotificationStore
    from storage import LocalFile
    store = NotificationStore(LocalFile(tmp_path / "notifiche.json"))
    monkeypatch.setitem(webapp.app.extensions, "notifications", store)
    return store
