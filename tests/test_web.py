import re
from datetime import datetime

import pytest

import admin
import app as webapp
from agenda import Agenda
from settings import SettingsStore

NOW = datetime(2026, 10, 26, 8, 0)


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = SettingsStore(tmp_path / "settings.json")
    monkeypatch.setitem(webapp.app.extensions, "settings", store)
    monkeypatch.setitem(webapp.app.extensions, "agenda", Agenda(tmp_path / "agenda.md", store, clock=lambda: NOW))
    monkeypatch.delenv("BARBER_ADMIN_PASSWORD", raising=False)
    admin._failed.clear()
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        yield c


def csrf(client, url):
    html = client.get(url).get_data(as_text=True)
    return re.search(r'name="_csrf" value="([^"]+)"', html).group(1)


def login(client, pw="segreta123"):
    token = csrf(client, "/admin/setup")
    client.post("/admin/setup", data={"_csrf": token, "password": pw, "password2": pw})
    return csrf(client, "/admin/settimana")


def test_full_customer_flow(client, tmp_path):
    token = csrf(client, "/")
    assert client.get("/api/slots?date=2026-10-26&service=Taglio").json["slots"][0] == "09:00"
    r = client.post("/prenota", data={"_csrf": token, "service": "Taglio", "date": "2026-10-26", "time": "09:00",
                                      "name": "Mario Rossi", "phone": "333 1234567", "email": "mario@example.com"})
    assert r.status_code == 302
    code = r.headers["Location"].rsplit("/", 1)[1]
    page = client.get(f"/gestisci/{code}").get_data(as_text=True)
    assert "Prenotazione confermata" in page and code in page
    assert code in (tmp_path / "agenda.md").read_text(encoding="utf-8")

    client.post(f"/gestisci/{code}/modifica", data={"_csrf": token, "service": "Barba",
                                                     "date": "2026-10-27", "time": "14:00"})
    assert "| 2026-10-27 | 14:00 | 14:30 |" in (tmp_path / "agenda.md").read_text(encoding="utf-8")
    client.post(f"/gestisci/{code}/cancella", data={"_csrf": token})
    assert "cancellata" in (tmp_path / "agenda.md").read_text(encoding="utf-8")


def test_post_without_csrf_is_rejected(client):
    assert client.post("/prenota", data={}).status_code == 400


def test_admin_panel_requires_password(client):
    assert "Pannello Admin" in client.get("/").get_data(as_text=True)  # link nel footer
    for url in ("/admin", "/admin/settimana", "/admin/clienti", "/admin/listino", "/admin/impostazioni"):
        assert client.get(url).status_code == 302
    login(client)
    client.post("/admin/logout", data={"_csrf": csrf(client, "/")})
    assert client.get("/admin/clienti").status_code == 302
    assert client.get("/admin/setup").status_code == 302  # la password non si può più reimpostare da qui
    token = csrf(client, "/admin/login")
    bad = client.post("/admin/login", data={"_csrf": token, "password": "sbagliata"})
    assert "non corretta" in bad.get_data(as_text=True)
    ok = client.post("/admin/login", data={"_csrf": token, "password": "segreta123"})
    assert ok.status_code == 302 and client.get("/admin/clienti").status_code == 200


def test_login_blocked_after_too_many_attempts(client):
    login(client)
    client.post("/admin/logout", data={"_csrf": csrf(client, "/")})
    token = csrf(client, "/admin/login")
    for _ in range(5):
        client.post("/admin/login", data={"_csrf": token, "password": "sbagliata"})
    r = client.post("/admin/login", data={"_csrf": token, "password": "segreta123"})
    assert "Troppi tentativi" in r.get_data(as_text=True)


def test_holidays_block_bookings(client):
    token = login(client)
    # Natale 2026 è venerdì: chiuso di default
    r = client.get("/api/slots?date=2026-12-25&service=Taglio").json
    assert r["slots"] == [] and "Natale" in r["message"]
    # il titolare decide di lavorare l'8 dicembre (martedì)
    assert client.get("/api/slots?date=2026-12-08&service=Taglio").json["slots"] == []
    closed = ["capodanno", "epifania", "pasqua", "pasquetta", "liberazione", "lavoro", "repubblica",
              "ferragosto", "san_francesco", "ognissanti", "natale", "santo_stefano"]
    client.post("/admin/festivita", data={"_csrf": token, "closed": closed})
    assert client.get("/api/slots?date=2026-12-08&service=Taglio").json["slots"]
    # festività locale ricorrente
    client.post("/admin/festivita-locali", data={"_csrf": token, "date": "2026-11-11", "name": "San Martino"})
    r = client.get("/api/slots?date=2026-11-11&service=Barba").json
    assert r["slots"] == [] and "San Martino" in r["message"]
    assert "San Martino" in client.get("/admin/impostazioni").get_data(as_text=True)


def test_admin_sections(client, tmp_path):
    token = login(client)
    # 3. listino
    r = client.post("/admin/listino", data={
        "_csrf": token, "service_count": "3",
        "svc-0-name": "Taglio", "svc-0-duration": "45", "svc-0-price": "15,00", "svc-0-active": "1",
        "svc-1-name": "Barba", "svc-1-duration": "30", "svc-1-price": "8", "svc-1-active": "1",
        "svc-2-name": "Taglio + Barba", "svc-2-duration": "75", "svc-2-price": "20", "svc-2-active": "1",
    })
    assert r.status_code == 302
    assert "€ 15,00" in client.get("/").get_data(as_text=True)

    # 2. pianificazione: inserimento manuale e cancellazione
    r = client.post("/admin/appuntamenti", data={"_csrf": token, "service": "Taglio + Barba", "date": "2026-10-28",
                                                  "time": "09:00", "name": "Gino", "phone": ""})
    assert r.status_code == 302
    text = (tmp_path / "agenda.md").read_text(encoding="utf-8")
    assert "| 2026-10-28 | 09:00 | 10:15 | Gino |  |  | Taglio + Barba | 20,00 | confermata |" in text
    week = client.get("/admin/settimana?data=2026-10-28").get_data(as_text=True)
    assert "Gino" in week and "26/10 – 01/11/2026" in week
    code = re.search(r"\| ([A-Z0-9]{6}) \| 2026-10-28", text).group(1)
    client.post(f"/admin/prenotazioni/{code}/cancella", data={"_csrf": token})
    assert "cancellata" in (tmp_path / "agenda.md").read_text(encoding="utf-8")

    # 1. anagrafica
    page = client.get("/admin/clienti").get_data(as_text=True)
    assert "Gino" in page
    cid = re.search(r'/admin/clienti/([0-9a-f]{10})', page).group(1)
    assert "Storico appuntamenti" in client.get(f"/admin/clienti/{cid}").get_data(as_text=True)

    # 4. orari e chiusure
    form = {"_csrf": token, "shop_name": "Barber Shop Stefano Stella", "address": "", "phone": "",
            "slot_step": "15", "horizon_days": "30",
            "day-0-open": "1", "day-0-w0-start": "09:00", "day-0-w0-end": "13:00"}
    assert client.post("/admin/impostazioni", data=form).status_code == 302
    assert client.post("/admin/impostazioni", data=dict(form, **{"day-0-w0-end": "08:00"})).status_code == 422
    client.post("/admin/chiusure", data={"_csrf": token, "start": "2026-11-02", "end": "2026-11-02", "reason": "Ferie"})
    slots = client.get("/api/slots?date=2026-11-02&service=Barba").json
    assert slots["slots"] == [] and "Ferie" in slots["message"]


def test_ping_is_lightweight(client, monkeypatch):
    # il ping non deve leggere agenda né impostazioni (su Render sarebbero chiamate a GitHub)
    agenda = webapp.app.extensions["agenda"]
    monkeypatch.setattr(agenda, "all", lambda: pytest.fail("il ping non deve leggere l'agenda"))
    monkeypatch.setattr(agenda.settings, "load", lambda: pytest.fail("il ping non deve leggere le impostazioni"))
    for method in (client.get, client.head):
        r = method("/ping")
        assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    assert client.get("/ping").get_data(as_text=True) == "OK"


def test_admin_pages_are_never_cached(client):
    login(client)
    for url in ("/admin/login", "/admin/settimana", "/admin/promemoria", "/admin/clienti"):
        assert client.get(url).headers.get("Cache-Control") == "no-store", url


def test_confirmation_popup_only_right_after_booking(client):
    token = csrf(client, "/")
    r = client.post("/prenota", data={"_csrf": token, "service": "Barba", "date": "2026-10-26", "time": "17:30",
                                      "name": "Mario Rossi", "phone": "333 1234567"})
    code = r.headers["Location"].rsplit("/", 1)[1]
    page = client.get(f"/gestisci/{code}").get_data(as_text=True)
    assert "<dialog class=\"confirm\" data-autoshow" in page
    assert "Prenotazione confermata!" in page and code in page and "alle <strong>17:30</strong>" in page
    assert "<em>Hai bisogno di un sistema automatizzato per la tua azienda?" in page
    assert 'href="https://wa.me/393348505104"' in page
    assert page.count(f'data-copy="{code}"') == 2  # pulsante "Copia" nel popup e nella scheda
    # riaprendo la pagina con il codice il popup non ricompare, il pulsante "Copia" resta
    again = client.get(f"/gestisci/{code}").get_data(as_text=True)
    assert "data-autoshow" not in again and again.count(f'data-copy="{code}"') == 1
