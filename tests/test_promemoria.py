import json
import re
from datetime import date, datetime, time

import pytest

import promemoria_whatsapp as pw
from agenda import Agenda, Booking
from settings import DEFAULT_REMINDER_TEMPLATE, SettingsStore

NOW = datetime(2026, 10, 26, 8, 0)
MON, TUE = date(2026, 10, 26), date(2026, 10, 27)

EXPECTED = """Promemoria Appuntamento – Stefano Stella Barber Shop

ti ricordiamo il tuo appuntamento presso Stefano Stella Barber Shop per domani alle ore 17.30

📍 Merine (LE) Via Lecce 122
📞 Per modifiche o imprevisti: 328 189 0726

Ti aspettiamo per offrirti un servizio di stile e qualità.
— Il Team di Stefano Stella ✂️💈"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = SettingsStore(tmp_path / "settings.json")
    agenda = Agenda(tmp_path / "agenda.md", store, clock=lambda: NOW)
    monkeypatch.setenv("BARBER_AGENDA_PATH", str(tmp_path / "agenda.md"))
    monkeypatch.setenv("BARBER_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(pw, "SENT_FILE", tmp_path / "inviati.json")
    for var in ("WHATSAPP_PROVIDER", "GITHUB_TOKEN", "GITHUB_REPO"):
        monkeypatch.delenv(var, raising=False)
    return agenda


def test_default_message_matches_requested_text():
    b = Booking("K7M2QX", TUE, time(17, 30), time(18, 15), "Mario Rossi", "+39 3331234567", "Taglio")
    assert pw.message_text(b, DEFAULT_REMINDER_TEMPLATE, MON) == EXPECTED
    other_day = pw.message_text(b, DEFAULT_REMINDER_TEMPLATE, date(2026, 10, 20))
    assert "per martedì 27 ottobre alle ore 17.30" in other_day


def test_whatsapp_link_has_prefilled_text():
    b = Booking("K7M2QX", TUE, time(17, 30), time(18, 15), "Mario Rossi", "+39 3331234567", "Taglio")
    link = pw.whatsapp_link(b, "Ciao à domani ✂️")
    assert link.startswith("https://wa.me/393331234567?text=Ciao%20%C3%A0%20domani")
    assert pw.whatsapp_link(Booking("X", TUE, time(9), time(10), "A", "", "Taglio"), "x") is None


def test_sends_once_per_appointment(env, monkeypatch, tmp_path):
    a = env.create(day=TUE, start=time(9, 0), service="Taglio", name="Mario Rossi", phone="333 1234567")
    env.create(day=TUE, start=time(10, 0), service="Barba", name="Senza Tel", phone="", by_owner=True)
    gone = env.create(day=TUE, start=time(11, 0), service="Barba", name="Annullato", phone="3330000000")
    env.cancel(gone.code)
    env.create(day=date(2026, 10, 28), start=time(9, 0), service="Barba", name="Dopodomani", phone="3331111111")

    calls = []
    monkeypatch.setenv("WHATSAPP_PROVIDER", "meta")
    monkeypatch.setitem(pw.PROVIDERS, "meta", lambda to, text, params: calls.append((to, text, params)) or "wamid.1")

    assert pw.run(TUE, dry_run=False, today=MON) == 0
    assert [(to, p["ora"]) for to, _, p in calls] == [("393331234567", "09.00")]
    assert "per domani alle ore 09.00" in calls[0][1]
    sent = json.loads((tmp_path / "inviati.json").read_text(encoding="utf-8"))
    assert list(sent) == [f"{a.code}|2026-10-27|09:00"]

    pw.run(TUE, dry_run=False, today=MON)  # seconda esecuzione: nessun doppione
    assert len(calls) == 1


def test_dry_run_and_missing_provider_send_nothing(env, monkeypatch, tmp_path):
    env.create(day=TUE, start=time(9, 0), service="Taglio", name="Mario Rossi", phone="333 1234567")
    monkeypatch.setitem(pw.PROVIDERS, "meta", lambda *a: pytest.fail("non deve inviare"))
    assert pw.run(TUE, dry_run=True, today=MON) == 0
    assert pw.run(TUE, dry_run=False, today=MON) == 0  # provider non configurato -> modalità prova
    assert not (tmp_path / "inviati.json").exists()


def test_failed_send_is_retried_next_run(env, monkeypatch):
    env.create(day=TUE, start=time(9, 0), service="Taglio", name="Mario Rossi", phone="333 1234567")
    monkeypatch.setenv("WHATSAPP_PROVIDER", "twilio")

    def boom(*a):
        raise pw.SendError("HTTP 500")
    monkeypatch.setitem(pw.PROVIDERS, "twilio", boom)
    assert pw.run(TUE, dry_run=False, today=MON) == 1
    monkeypatch.setitem(pw.PROVIDERS, "twilio", lambda *a: "SM1")
    assert pw.run(TUE, dry_run=False, today=MON) == 0


def test_meta_template_params_configurable(monkeypatch):
    monkeypatch.setenv("META_TEMPLATE_PARAMS", "nome, ora")
    assert pw._template_values({"nome": "Mario", "ora": "17.30"}) == ["Mario", "17.30"]
    monkeypatch.delenv("META_TEMPLATE_PARAMS")
    assert pw._template_values({"nome": "Mario", "ora": "17.30"}) == ["17.30"]


def test_admin_manual_reminders(env, monkeypatch, tmp_path):
    import admin
    import app as webapp
    store = env.settings
    monkeypatch.setitem(webapp.app.extensions, "settings", store)
    monkeypatch.setitem(webapp.app.extensions, "agenda", env)
    monkeypatch.setenv("BARBER_ADMIN_PASSWORD", "segreta123")
    admin._failed.clear()
    b = env.create(day=TUE, start=time(17, 30), service="Barba", name="Mario Rossi", phone="333 1234567")
    with webapp.app.test_client() as c:
        token = re.search(r'name="_csrf" value="([^"]+)"', c.get("/admin/login").get_data(as_text=True)).group(1)
        c.post("/admin/login", data={"_csrf": token, "password": "segreta123"})
        page = c.get("/admin/promemoria").get_data(as_text=True)  # predefinito: domani (27/10)
        assert "Mario Rossi" in page and "Da inviare" in page
        assert "https://wa.me/393331234567?text=Promemoria%20Appuntamento" in page
        assert "Stefano Stella Barber Shop per domani alle ore 17.30" in page  # anteprima
        # senza fornitore l'invio diretto non è disponibile
        c.post("/admin/promemoria/invia", data={"_csrf": token, "data": "2026-10-27", "code": b.code})
        assert not (tmp_path / "inviati.json").exists()
        # invio manuale via WhatsApp + "segna come inviato"
        c.post("/admin/promemoria/segna", data={"_csrf": token, "data": "2026-10-27", "code": b.code})
        assert "Inviato" in c.get("/admin/promemoria?data=2026-10-27").get_data(as_text=True)
        # con fornitore: "Invia ora"
        c.post("/admin/promemoria/segna", data={"_csrf": token, "data": "2026-10-27", "code": b.code, "annulla": "1"})
        monkeypatch.setenv("WHATSAPP_PROVIDER", "meta")
        calls = []
        monkeypatch.setitem(pw.PROVIDERS, "meta", lambda to, text, params: calls.append(to) or "wamid")
        c.post("/admin/promemoria/invia", data={"_csrf": token, "data": "2026-10-27", "tutti": "1"})
        assert calls == ["393331234567"]
        # testo personalizzato con segnaposto non valido -> rifiutato
        c.post("/admin/promemoria/testo", data={"_csrf": token, "template": "Ciao {sconosciuto}"})
        assert store.load().reminder_template == DEFAULT_REMINDER_TEMPLATE
        c.post("/admin/promemoria/testo", data={"_csrf": token, "template": "Ciao {nome}, a {quando} alle {ora}!"})
        assert store.load().reminder_template == "Ciao {nome}, a {quando} alle {ora}!"


def test_guided_bulk_manual_sending(env, monkeypatch, tmp_path):
    import admin
    import app as webapp
    monkeypatch.setitem(webapp.app.extensions, "settings", env.settings)
    monkeypatch.setitem(webapp.app.extensions, "agenda", env)
    monkeypatch.setenv("BARBER_ADMIN_PASSWORD", "segreta123")
    admin._failed.clear()
    a = env.create(day=TUE, start=time(9, 0), service="Barba", name="Anna Uno", phone="3331111111")
    b = env.create(day=TUE, start=time(10, 0), service="Barba", name="Bruno Due", phone="3332222222")
    c_ = env.create(day=TUE, start=time(11, 0), service="Barba", name="Carlo Tre", phone="3333333333")
    env.create(day=TUE, start=time(12, 0), service="Barba", name="Senza Tel", phone="", by_owner=True)
    key = pw.reminder_key
    with webapp.app.test_client() as c:
        token = re.search(r'name="_csrf" value="([^"]+)"', c.get("/admin/login").get_data(as_text=True)).group(1)
        c.post("/admin/login", data={"_csrf": token, "password": "segreta123"})
        assert "Invio guidato a tutti (3)" in c.get("/admin/promemoria?data=2026-10-27").get_data(as_text=True)

        page = c.get("/admin/promemoria/guidato?data=2026-10-27").get_data(as_text=True)
        assert "Anna Uno" in page and "https://wa.me/393331111111?text=" in page and "0 di 3 inviati" in page
        # "Apri WhatsApp" -> segnato via fetch (risposta 204, senza pagina)
        r = c.post("/admin/promemoria/segna", data={"_csrf": token, "data": "2026-10-27", "code": key(a), "ajax": "1"})
        assert r.status_code == 204
        page = c.get("/admin/promemoria/guidato?data=2026-10-27").get_data(as_text=True)
        assert "Bruno Due" in page and "1 di 3 inviati" in page
        # salta Bruno -> tocca a Carlo
        page = c.get(f"/admin/promemoria/guidato?data=2026-10-27&salta={key(b)}").get_data(as_text=True)
        assert "Carlo Tre" in page and "wa.me/393333333333" in page
        # "Già inviato, avanti" senza JavaScript: torna alla pagina guidata
        nxt = f"/admin/promemoria/guidato?data=2026-10-27&salta={key(b)}"
        r = c.post("/admin/promemoria/segna", data={"_csrf": token, "data": "2026-10-27", "code": key(c_), "next": nxt})
        from urllib.parse import unquote
        assert unquote(r.headers["Location"]).endswith(nxt)
        done = c.get(nxt).get_data(as_text=True)
        assert "Fatto!" in done and "2 di 3" in done and "Saltati: Bruno Due" in done and "1 appuntamento senza telefono" in done
    sent = json.loads((tmp_path / "inviati.json").read_text(encoding="utf-8"))
    assert set(sent) == {key(a), key(c_)}
    assert sent[key(a)]["via"] == "WhatsApp manuale"
