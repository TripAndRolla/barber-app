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


def register(client, token, email="mario@example.com", phone="333 1234567", password="password123", code=""):
    return client.post("/area-clienti/registrati", data={
        "_csrf": token, "name": "Mario Rossi", "email": email, "phone": phone,
        "password": password, "password2": password, "code": code})


def login(client, token, email="mario@example.com", password="password123"):
    return client.post("/area-clienti/accedi", data={"_csrf": token, "email": email, "password": password})


def logout(client, token):
    client.post("/area-clienti/esci", data={"_csrf": token})


def test_register_login_and_password_rules(env, isolated_accounts):
    with webapp.app.test_client() as c:
        token = csrf(c)
        assert c.get("/area-clienti").status_code == 302  # serve l'accesso
        r = register(c, token, password="corta")
        assert "almeno 8 caratteri" in r.get_data(as_text=True)
        r = register(c, token, phone="12345")
        assert "telefono" in r.get_data(as_text=True)
        assert register(c, token).status_code == 302
        account = isolated_accounts.by_email("MARIO@example.com")
        assert account["phone"] == "+39 3331234567" and "password123" not in str(account)  # solo l'hash
        logout(c, token)
        assert "esiste già un account" in register(c, token).get_data(as_text=True).lower()

        assert "non corretti" in login(c, token, password="sbagliata1").get_data(as_text=True)
        assert login(c, token, email="Mario@Example.com").status_code == 302
        area = c.get("/area-clienti").get_data(as_text=True)
        assert "Ciao Mario!" in area and "mario@example.com" in area
        # la home precompila i dati del cliente collegato
        home = c.get("/").get_data(as_text=True)
        assert 'value="Mario Rossi"' in home and 'value="3331234567"' in home and 'value="mario@example.com"' in home


def test_phone_alone_does_not_reveal_bookings(env):
    """Registrarsi con il numero di un altro non basta: serve il codice di una sua prenotazione."""
    with webapp.app.test_client() as c:
        token = csrf(c)
        victim = book(c, token, "09:00", "333 1234567")
        register(c, token, email="intruso@example.com", phone="333 1234567")
        area = c.get("/area-clienti").get_data(as_text=True)
        assert victim not in area and "Non hai appuntamenti in programma" in area
        r = c.post("/area-clienti/collega", data={"_csrf": token, "code": "ZZZZZZ"}, follow_redirects=True)
        assert "Nessuna prenotazione trovata" in r.get_data(as_text=True)


def test_code_links_all_bookings_of_the_number(env, isolated_accounts):
    with webapp.app.test_client() as c:
        token = csrf(c)
        first = book(c, token, "09:00", "333 1234567")
        second = book(c, token, "10:00", "+39 333-123-4567", date="2026-10-28")
        other = book(c, token, "11:00", "347 0000000", name="Altro Cliente")
        register(c, token, code=first.lower())  # codice inserito in registrazione
        area = c.get("/area-clienti").get_data(as_text=True)
        assert first in area and second in area and other not in area
        assert isolated_accounts.by_email("mario@example.com")["phone_verified"] is True
        # una prenotazione fatta da collegato entra subito nell'account
        third = book(c, token, "12:00", "333 1234567", date="2026-10-29")
        assert third in c.get("/area-clienti").get_data(as_text=True)


def test_booking_while_logged_in_is_linked_even_if_number_unverified(env):
    with webapp.app.test_client() as c:
        token = csrf(c)
        register(c, token)
        code = book(c, token)
        assert code in c.get("/area-clienti").get_data(as_text=True)


def test_login_rate_limited(env):
    with webapp.app.test_client() as c:
        token = csrf(c)
        register(c, token)
        logout(c, token)
        for _ in range(5):
            login(c, token, password="sbagliata1")
        assert "Troppi tentativi" in login(c, token).get_data(as_text=True)


def test_owner_reset_password_forces_change(env, isolated_accounts):
    with webapp.app.test_client() as c:
        token = csrf(c)
        register(c, token)
        logout(c, token)
        c.post("/admin/login", data={"_csrf": token, "password": "segreta123"})
        account = isolated_accounts.by_email("mario@example.com")
        assert "Mario Rossi" in c.get("/admin/account").get_data(as_text=True)
        c.post(f"/admin/account/{account['id']}/reset", data={"_csrf": token, "next": "/admin/account"})
        page = c.get("/admin/account").get_data(as_text=True)
        temp = re.search(r'<code>([a-z0-9]{10})</code>', page).group(1)
        assert "https://wa.me/393331234567?text=" in page  # invio della password al cliente
        assert "<code>" not in c.get("/admin/account").get_data(as_text=True)  # mostrata una volta sola
        c.post("/admin/logout", data={"_csrf": token})

        assert "non corretti" in login(c, token).get_data(as_text=True)  # la vecchia non vale più
        r = login(c, token, password=temp)
        assert r.headers["Location"].endswith("/area-clienti/password")
        assert c.get("/area-clienti").headers["Location"].endswith("/area-clienti/password")  # obbligatorio
        r = c.post("/area-clienti/password", data={"_csrf": token, "new": "nuovapass1", "new2": "nuovapass1"})
        assert r.headers["Location"].endswith("/area-clienti")
        logout(c, token)
        assert login(c, token, password="nuovapass1").status_code == 302


def test_customer_changes_own_password(env):
    with webapp.app.test_client() as c:
        token = csrf(c)
        register(c, token)
        r = c.post("/area-clienti/password", data={"_csrf": token, "current": "sbagliata1", "new": "altrapass1", "new2": "altrapass1"})
        assert "non è corretta" in r.get_data(as_text=True)
        c.post("/area-clienti/password", data={"_csrf": token, "current": "password123", "new": "altrapass1", "new2": "altrapass1"})
        logout(c, token)
        assert login(c, token, password="altrapass1").status_code == 302


def test_admin_deletes_account(env, isolated_accounts):
    with webapp.app.test_client() as c:
        token = csrf(c)
        register(c, token)
        account = isolated_accounts.by_email("mario@example.com")
        c.post("/admin/login", data={"_csrf": token, "password": "segreta123"})
        c.post(f"/admin/account/{account['id']}/elimina", data={"_csrf": token})
        assert isolated_accounts.all() == []
        assert c.get("/area-clienti").status_code == 302  # la sessione del cliente non vale più


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
        register(c, token, code=code)
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
