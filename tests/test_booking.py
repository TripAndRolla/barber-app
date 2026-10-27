from datetime import date, datetime, time

import pytest

from agenda import Agenda, BookingError, parse_agenda
from customers import build_customers
from settings import Closure, Service, SettingsError, SettingsStore

NOW = datetime(2026, 10, 26, 8, 0)  # lunedì mattina
MON, SAT, SUN = date(2026, 10, 26), date(2026, 10, 31), date(2026, 11, 1)


@pytest.fixture
def store(tmp_path):
    return SettingsStore(tmp_path / "settings.json")


@pytest.fixture
def agenda(tmp_path, store):
    return Agenda(tmp_path / "agenda.md", store, clock=lambda: NOW)


def hhmm(slots):
    return [f"{t:%H:%M}" for t in slots]


def test_no_service_across_lunch_break(agenda):
    slots = hhmm(agenda.available_slots(MON, "Taglio"))
    assert "12:00" in slots and "12:15" not in slots  # 12:15+45 = 13:00 > 12:45
    assert "14:00" in slots and "17:15" in slots and "17:30" not in slots
    barba = hhmm(agenda.available_slots(MON, "Barba"))
    assert "12:15" in barba and "12:30" not in barba


def test_saturday_morning_only_and_sunday_closed(agenda):
    assert max(hhmm(agenda.available_slots(SAT, "Barba"))) == "12:15"
    assert agenda.available_slots(SUN, "Taglio") == []


def test_create_writes_markdown_and_blocks_overlap(agenda, tmp_path):
    b = agenda.create(day=MON, start=time(9, 0), service="Taglio", name="Mario Rossi", phone="333 1234567")
    text = (tmp_path / "agenda.md").read_text(encoding="utf-8")
    assert f"| {b.code} | 2026-10-26 | 09:00 | 09:45 | Mario Rossi |" in text
    slots = hhmm(agenda.available_slots(MON, "Barba"))
    assert "09:00" not in slots and "09:30" not in slots and "09:45" in slots
    with pytest.raises(BookingError):
        agenda.create(day=MON, start=time(9, 15), service="Barba", name="Luca Bianchi", phone="3331112233", email="luca@example.com")


def test_modify_and_cancel_by_code(agenda):
    b = agenda.create(day=MON, start=time(10, 0), service="Barba", name="Anna Verdi", phone="3331112222")
    # il proprio slot resta disponibile durante la modifica
    assert time(10, 0) in agenda.available_slots(MON, "Taglio", exclude_code=b.code)
    updated = agenda.update(b.code.lower(), day=SAT, start=time(11, 0), service="Taglio")
    assert (updated.day, updated.end, updated.status) == (SAT, time(11, 45), "modificata")
    assert time(10, 0) in agenda.available_slots(MON, "Taglio")
    agenda.cancel(b.code)
    assert agenda.get(b.code).status == "cancellata"
    assert time(11, 0) in agenda.available_slots(SAT, "Taglio")
    with pytest.raises(BookingError):
        agenda.cancel(b.code)


def test_markdown_escaping_and_manual_rows(agenda, tmp_path):
    agenda.create(day=MON, start=time(14, 0), service="Barba", name="Pipe | Name", phone="3330000000",
                  notes="nota | con pipe")
    path = tmp_path / "agenda.md"
    # riga aggiunta a mano (senza codice né ora fine) nel vecchio formato
    path.write_text(path.read_text(encoding="utf-8") + "| | 2026-10-26 | 15:00 | | Walk-in | | | Taglio | | confermata | |\n",
                    encoding="utf-8")
    bookings, unparsed = parse_agenda(path.read_text(encoding="utf-8"))
    assert not unparsed and len(bookings) == 2
    assert bookings[0].name == "Pipe | Name"
    assert time(15, 0) not in agenda.available_slots(MON, "Taglio")


def test_owner_settings_change_services_prices_and_hours(agenda, store):
    s = store.load()
    s.services = [Service("Taglio", 45, 15.0), Service("Barba", 30, 8.5), Service("Taglio + Barba", 75, 20.0)]
    s.hours[6] = [("10:00", "12:00")]  # apertura straordinaria la domenica
    s.hours[0] = []                    # chiuso il lunedì
    s.closures = [Closure(SAT, SAT, "Ferie")]
    store.save(s)
    assert agenda.available_slots(MON, "Taglio") == []
    assert agenda.available_slots(SAT, "Taglio") == []
    assert agenda.available_slots(SUN, "Taglio") == []  # 1° novembre: Ognissanti
    assert hhmm(agenda.available_slots(date(2026, 11, 8), "Taglio + Barba")) == ["10:00", "10:15", "10:30", "10:45"]
    assert store.load().service("Barba").price_label == "€ 8,50"


def test_national_holidays():
    from settings import Settings, easter_sunday
    assert [easter_sunday(y) for y in (2025, 2026, 2027)] == [date(2025, 4, 20), date(2026, 4, 5), date(2027, 3, 28)]
    s = Settings()
    h2026 = s.holidays_in(2026)
    assert h2026[date(2026, 4, 6)].startswith("Lunedì dell'Angelo")
    assert date(2026, 4, 25) in h2026 and date(2026, 10, 4) in h2026
    assert date(2025, 10, 4) not in s.holidays_in(2025)  # San Francesco festivo solo dal 2026
    assert s.windows_for(date(2026, 12, 25)) == []
    s.national_holidays_open = ["natale"]
    assert s.windows_for(date(2026, 12, 25))


def test_invalid_settings_rejected(store):
    s = store.load()
    s.hours[1] = [("13:00", "12:00")]
    with pytest.raises(SettingsError):
        store.save(s)


def test_old_agenda_format_is_read(agenda, tmp_path):
    (tmp_path / "agenda.md").write_text(
        "# Agenda Prenotazioni\n\n| Data | Ora | Cliente | Contatto | Servizio | Note | Stato |\n"
        "|------|-----|---------|----------|----------|------|-------|\n"
        "| 26/10/2026 | 10:00 | Pino | 333 | Barba | | confermata |\n", encoding="utf-8")
    assert time(10, 0) not in agenda.available_slots(MON, "Barba")


def test_owner_force_insert_still_blocks_overlaps(agenda):
    b = agenda.create(day=SUN, start=time(10, 0), service="Taglio", name="Amico", phone="",
                      by_owner=True, force=True)
    assert b.phone == "" and b.day == SUN
    with pytest.raises(BookingError, match="sovrappone"):
        agenda.create(day=SUN, start=time(10, 30), service="Barba", name="Altro", phone="", by_owner=True, force=True)
    with pytest.raises(BookingError):  # senza force la domenica resta chiusa
        agenda.create(day=SUN, start=time(12, 0), service="Barba", name="Altro", phone="", by_owner=True)
    with pytest.raises(BookingError):  # il cliente deve lasciare un contatto
        agenda.create(day=MON, start=time(9, 0), service="Barba", name="Cliente", phone="")


def test_customers_history_and_spend(tmp_path, store):
    s = store.load()
    s.services = [Service("Taglio", 45, 15.0), Service("Barba", 30, 8.0)]
    store.save(s)
    past = Agenda(tmp_path / "agenda.md", store, clock=lambda: datetime(2025, 3, 1, 8, 0))
    past.create(day=date(2025, 3, 3), start=time(9, 0), service="Taglio", name="Mario", phone="+39 333 111 2222")
    s.services[0].price = 18.0  # aumento di listino
    store.save(s)
    a = Agenda(tmp_path / "agenda.md", store, clock=lambda: NOW)
    a.create(day=MON, start=time(9, 0), service="Taglio", name="Mario Rossi", phone="3331112222")
    a.create(day=MON, start=time(10, 0), service="Barba", name="Mario Rossi", phone="333 111 2222")
    gone = a.create(day=date(2026, 10, 27), start=time(9, 0), service="Barba", name="Mario Rossi", phone="3331112222")
    a.cancel(gone.code)
    [mario] = build_customers(a.all(), store.load(), datetime(2026, 10, 26, 12, 0))
    assert mario.name == "Mario Rossi" and len(mario.bookings) == 4
    assert (mario.visits, mario.cancelled) == (3, 1)
    assert mario.spend_year == 26.0   # 18 + 8 nel 2026
    assert mario.spend_total == 41.0  # + 15 del 2025, al prezzo di allora


def test_past_slots_hidden(tmp_path, store):
    late = Agenda(tmp_path / "a.md", store, clock=lambda: datetime(2026, 10, 26, 16, 10))
    assert hhmm(late.available_slots(MON, "Barba"))[0] == "16:15"
    assert late.available_slots(date(2026, 10, 24), "Barba") == []


@pytest.mark.parametrize("raw", ["333 1234567", "+39 333 1234567", "+393331234567", "0039 333-123-4567", "0832 123456"])
def test_phone_normalized_to_plus39(raw):
    from agenda import normalize_phone
    assert normalize_phone(raw).startswith("+39 ")
    assert " " not in normalize_phone(raw)[4:]


@pytest.mark.parametrize("raw", ["", "12345", "+44 7700 900123", "abc", "933 1234567"])
def test_invalid_phone_rejected(agenda, raw):
    with pytest.raises(BookingError):
        agenda.create(day=MON, start=time(9, 0), service="Barba", name="Mario Rossi", phone=raw)


def test_phone_and_email_saved_in_separate_columns(agenda, tmp_path):
    b = agenda.create(day=MON, start=time(9, 0), service="Barba", name="Mario Rossi",
                      phone="333 1234567", email="Mario@Example.com")
    assert (b.phone, b.email) == ("+39 3331234567", "mario@example.com")
    text = (tmp_path / "agenda.md").read_text(encoding="utf-8")
    assert "| Cliente | Telefono | Email | Servizio |" in text
    assert "| Mario Rossi | +39 3331234567 | mario@example.com | Barba |" in text
    with pytest.raises(BookingError, match="e-mail"):
        agenda.create(day=MON, start=time(10, 0), service="Barba", name="Luca", phone="3331112222", email="non-valida")
