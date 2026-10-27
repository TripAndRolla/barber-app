import base64
import hashlib
from datetime import date, datetime, time

import pytest

from agenda import Agenda, BookingError
from settings import SettingsStore
from storage import GitHubFile, StorageConflict


class FakeGitHub:
    """Simula la Contents API di GitHub: file in memoria con SHA."""

    def __init__(self):
        self.files: dict[str, tuple[str, str]] = {}
        self.commits: list[str] = []

    def handler(self, gh: GitHubFile):
        def _request(method, data=None):
            if method == "GET":
                if gh.file_path not in self.files:
                    return 404, {"message": "Not Found"}
                content, sha = self.files[gh.file_path]
                return 200, {"content": base64.b64encode(content.encode()).decode(), "sha": sha}
            current = self.files.get(gh.file_path)
            if current and data.get("sha") != current[1]:
                return 409, {"message": "sha mismatch"}
            content = base64.b64decode(data["content"]).decode()
            sha = hashlib.sha1(content.encode()).hexdigest()
            self.files[gh.file_path] = (content, sha)
            self.commits.append(data["message"])
            return (201 if current is None else 200), {"content": {"sha": sha}}
        return _request


def make_file(fake, name):
    f = GitHubFile("negozio/dati", name, "token", cache_seconds=0)
    f._request = fake.handler(f)
    return f


def test_agenda_on_github_commits_each_change():
    fake = FakeGitHub()
    store = SettingsStore(make_file(fake, "settings.json"))
    agenda = Agenda(make_file(fake, "agenda.md"), store, clock=lambda: datetime(2026, 10, 26, 8, 0))
    b = agenda.create(day=date(2026, 10, 26), start=time(9, 0), service="Taglio", name="Mario Rossi", phone="3331234567")
    agenda.cancel(b.code)
    assert fake.commits == [f"Nuova prenotazione {b.code}: 2026-10-26 09:00", f"Cancellazione prenotazione {b.code}"]
    assert "| cancellata |" in fake.files["agenda.md"][0]
    # una nuova istanza (es. dopo il riavvio di Render) ritrova i dati
    again = Agenda(make_file(fake, "agenda.md"), store)
    assert again.get(b.code).status == "cancellata"


def test_conflict_is_reported_not_overwritten():
    fake = FakeGitHub()
    f = make_file(fake, "agenda.md")
    f.write("versione 1")
    fake.files["agenda.md"] = ("modifica manuale", "altro-sha")  # qualcuno modifica il file su GitHub
    f.cache_seconds = 60  # la copia in cache è vecchia
    with pytest.raises(StorageConflict):
        f.write("versione 2")
    assert fake.files["agenda.md"][0] == "modifica manuale"
    assert f.read() == "modifica manuale"  # dopo il conflitto rilegge


def test_conflict_becomes_friendly_booking_error():
    fake = FakeGitHub()
    store = SettingsStore(make_file(fake, "settings.json"))
    agenda_file = make_file(fake, "agenda.md")
    agenda = Agenda(agenda_file, store, clock=lambda: datetime(2026, 10, 26, 8, 0))
    agenda.create(day=date(2026, 10, 26), start=time(9, 0), service="Taglio", name="Mario Rossi", phone="3331234567")
    agenda_file.cache_seconds = 60
    content, _ = fake.files["agenda.md"]
    fake.files["agenda.md"] = (content, "cambiato")
    with pytest.raises(BookingError, match="riprova"):
        agenda.create(day=date(2026, 10, 26), start=time(10, 0), service="Barba", name="Luca", phone="3331112222")
