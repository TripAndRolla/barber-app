import base64
import re
from datetime import datetime

import pytest

import app as webapp
import notifications
import ratelimit
from agenda import Agenda
from settings import SettingsStore

NOW = datetime(2026, 10, 26, 8, 0)


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = SettingsStore(tmp_path / "settings.json")
    agenda = Agenda(tmp_path / "agenda.md", store, clock=lambda: NOW)
    monkeypatch.setitem(webapp.app.extensions, "settings", store)
    monkeypatch.setitem(webapp.app.extensions, "agenda", agenda)
    monkeypatch.setenv("BARBER_ADMIN_PASSWORD", "segreta123")
    ratelimit.reset()
    webapp.app.config["TESTING"] = True
    sent = []  # chiamate HTTP verso ntfy/CallMeBot
    monkeypatch.setattr(notifications, "_request", lambda url, data=None, headers=None: sent.append((url, data, headers)))
    return agenda, store, sent


def csrf(client, url="/"):
    return re.search(r'name="_csrf" value="([^"]+)"', client.get(url).get_data(as_text=True)).group(1)


def book(client, token, time="09:00", phone="333 1234567", name="Mario Rossi", date="2026-10-27"):
    r = client.post("/prenota", data={"_csrf": token, "service": "Barba", "date": date, "time": time,
                                      "name": name, "phone": phone})
    return r.headers["Location"].rsplit("/", 1)[1]


def test_customer_login_and_own_bookings_only(env):
    with webapp.app.test_client() as c:
        token = csrf(c)
        mine1 = book(c, token, "09:00", "333 1234567")
        mine2 = book(c, token, "10:00", "+39 333-123-4567", date="2026-10-28")  # stesso numero scritto diverso
        other = book(c, token, "11:00", "347 0000000", name="Altro Cliente")
        assert c.get("/area-clienti").status_code == 302  # serve l'accesso

        bad = c.post("/area-clienti/accedi", data={"_csrf": token, "accesso": "340 9999999"})
        assert "Nessuna prenotazione trovata" in bad.get_data(as_text=True)  # numero senza prenotazioni
        bad = c.post("/area-clienti/accedi", data={"_csrf": token, "accesso": "ZZZZZZ"})
        assert "Nessuna prenotazione trovata" in bad.get_data(as_text=True)  # codice inesistente
        # accesso con il solo numero di telefono (scritto in un altro formato)
        ok = c.post("/area-clienti/accedi", data={"_csrf": token, "accesso": "+39 333 123 4567"})
        assert ok.status_code == 302
        area = c.get("/area-clienti").get_data(as_text=True)
        assert mine1 in area and mine2 in area and other not in area
        assert "Ciao Mario!" in area and 'data-copy="' + mine1 + '"' in area
        # la home precompila nome e telefono
        home = c.get("/").get_data(as_text=True)
        assert 'value="Mario Rossi"' in home and 'value="3331234567"' in home and "Le mie prenotazioni" in home
        c.post("/area-clienti/esci", data={"_csrf": token})
        assert c.get("/area-clienti").status_code == 302
        # accesso con il solo codice prenotazione: stesse prenotazioni dello stesso numero
        ok = c.post("/area-clienti/accedi", data={"_csrf": token, "accesso": mine2.lower()})
        assert ok.status_code == 302
        area = c.get("/area-clienti").get_data(as_text=True)
        assert mine1 in area and mine2 in area and other not in area


def test_customer_login_rate_limited(env):
    with webapp.app.test_client() as c:
        token = csrf(c)
        code = book(c, token)
        for _ in range(5):
            c.post("/area-clienti/accedi", data={"_csrf": token, "accesso": "XXXXXX"})
        r = c.post("/area-clienti/accedi", data={"_csrf": token, "accesso": code})
        assert "Troppi tentativi" in r.get_data(as_text=True)


def test_owner_alerted_on_customer_changes(env, isolated_notifications):
    agenda, store, sent = env
    s = store.load()
    s.notify_ntfy_topic = "barber-test-123"
    store.save(s)
    with webapp.app.test_client() as c:
        token = csrf(c)
        code = book(c, token, "09:00")
        assert isolated_notifications.all()[0]["type"] == "nuova"
        assert sent == []  # "nuova" non è tra gli eventi predefiniti da mandare al telefono

        # modifica dal link con il codice
        c.post(f"/gestisci/{code}/modifica", data={"_csrf": token, "service": "Taglio", "date": "2026-10-28", "time": "14:00"})
        ev = isolated_notifications.all()[0]
        assert (ev["type"], ev["via"]) == ("modifica", "link con codice")
        assert ev["old"] == "martedì 27 ottobre 2026 alle 09:00" and ev["new"] == "mercoledì 28 ottobre 2026 alle 14:00"
        assert ev["old_service"] == "Barba" and ev["service"] == "Taglio"
        url, body, headers = sent[-1]
        assert url == "https://ntfy.sh/barber-test-123" and "Prima: martedì 27 ottobre" in body.decode()
        assert headers["Click"].endswith("/admin/avvisi")

        # annullamento dall'area clienti
        c.post("/area-clienti/accedi", data={"_csrf": token, "accesso": "3331234567"})
        r = c.post(f"/gestisci/{code}/cancella", data={"_csrf": token, "next": "/area-clienti"})
        assert r.headers["Location"].endswith("/area-clienti")
        ev = isolated_notifications.all()[0]
        assert (ev["type"], ev["via"]) == ("cancellazione", "area clienti")
        assert len(sent) == 2
        title = base64.b64decode(sent[-1][2]["Title"].split("?")[3]).decode()  # "=?UTF-8?B?...?="
        assert title == "❌ Prenotazione annullata"
        area = c.get("/area-clienti").get_data(as_text=True)
        assert "Appuntamento annullato." in area and "Non hai appuntamenti in programma" in area


def test_admin_cancel_does_not_alert(env, isolated_notifications):
    agenda, store, sent = env
    with webapp.app.test_client() as c:
        token = csrf(c)
        code = book(c, token)
        c.post("/admin/login", data={"_csrf": token, "password": "segreta123"})
        before = len(isolated_notifications.all())
        c.post(f"/admin/prenotazioni/{code}/cancella", data={"_csrf": token})
        assert agenda.get(code).status == "cancellata"
        assert len(isolated_notifications.all()) == before  # azione del titolare: nessun avviso a se stesso


def test_admin_alerts_page_badge_and_settings(env, isolated_notifications):
    agenda, store, sent = env
    with webapp.app.test_client() as c:
        token = csrf(c)
        code = book(c, token)
        c.post(f"/gestisci/{code}/cancella", data={"_csrf": token})
        c.post("/admin/login", data={"_csrf": token, "password": "segreta123"})
        week = c.get("/admin/settimana").get_data(as_text=True)
        assert 'class="nav__badge">2<' in week and "2 nuovi avvisi" in week

        page = c.get("/admin/avvisi").get_data(as_text=True)
        assert "Prenotazione annullata" in page and "Nuova prenotazione" in page and page.count(">Nuovo<") == 2
        assert "nav__badge" not in c.get("/admin/settimana").get_data(as_text=True)  # letti

        # canali: nome non valido rifiutato, poi salvataggio e notifica di prova
        c.post("/admin/avvisi/impostazioni", data={"_csrf": token, "ntfy_topic": "x y", "evento-modifica": "1"})
        assert store.load().notify_ntfy_topic == ""
        c.post("/admin/avvisi/impostazioni", data={"_csrf": token, "ntfy_topic": "barber-stella-ab12",
                                                    "evento-nuova": "1", "evento-cancellazione": "1",
                                                    "callmebot_phone": "+39 328 1890726", "callmebot_apikey": "123456"})
        s = store.load()
        assert s.notify_events == ["nuova", "cancellazione"] and s.notify_callmebot_phone == "+393281890726"
        r = c.post("/admin/avvisi/prova", data={"_csrf": token}, follow_redirects=True)
        assert "Notifica di prova inviata" in r.get_data(as_text=True)
        assert [u.split("?")[0] for u, _, _ in sent] == ["https://ntfy.sh/barber-stella-ab12",
                                                         "https://api.callmebot.com/whatsapp.php"]
        assert "phone=%2B393281890726" in sent[1][0] and "apikey=123456" in sent[1][0]


def test_push_failure_does_not_break_booking(env, monkeypatch, isolated_notifications):
    agenda, store, sent = env
    s = store.load()
    s.notify_ntfy_topic = "barber-test-123"
    s.notify_events = ["nuova"]
    store.save(s)

    def down(*a, **k):
        raise OSError("rete non disponibile")
    monkeypatch.setattr(notifications, "_request", down)
    with webapp.app.test_client() as c:
        code = book(c, csrf(c))
        assert agenda.get(code) is not None and isolated_notifications.all()[0]["code"] == code
