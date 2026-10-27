"""Regole di prenotazione e sincronizzazione con agenda.md.

Il file Markdown è l'unica fonte di verità: ogni operazione lo rilegge,
applica la modifica e lo riscrive in modo atomico. Così eventuali modifiche
manuali (o del receptionist-bot) vengono sempre rispettate.
Servizi e orari arrivano da SettingsStore, modificabile dal proprietario.
"""
from __future__ import annotations

import os
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from settings import GIORNI, Settings, SettingsStore
from storage import LocalFile, StorageConflict, StorageError

STATUS_CONFIRMED = "confermata"
STATUS_MODIFIED = "modificata"
STATUS_CANCELLED = "cancellata"

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # senza 0/O e 1/I
CODE_LENGTH = 6
DEFAULT_DURATION = 45  # usata solo per righe scritte a mano senza "Ora fine"

COLUMNS = ["Codice", "Data", "Ora inizio", "Ora fine", "Cliente", "Telefono", "Email", "Servizio", "Prezzo", "Stato", "Note"]
# Intestazioni accettate in lettura (anche formati scritti a mano)
_HEADER_ALIASES = {
    "codice": "code",
    "data": "day",
    "ora": "start",
    "ora inizio": "start",
    "inizio": "start",
    "ora fine": "end",
    "fine": "end",
    "cliente": "name",
    "contatto": "contact",  # vecchio formato: telefono o e-mail nella stessa colonna
    "telefono": "phone",
    "email": "email",
    "e-mail": "email",
    "servizio": "service",
    "prezzo": "price",
    "stato": "status",
    "note": "notes",
}

MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
        "agosto", "settembre", "ottobre", "novembre", "dicembre"]


class BookingError(Exception):
    """Errore di validazione mostrato direttamente al cliente."""


def _t(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def _add_minutes(t: time, minutes: int) -> time:
    return (datetime.combine(date.min, t) + timedelta(minutes=minutes)).time()


def format_day_it(d: date) -> str:
    return f"{GIORNI[d.weekday()]} {d.day} {MESI[d.month - 1]} {d.year}"


def parse_date(value: str) -> date:
    """Data da un campo <input type=date>."""
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        raise BookingError("Seleziona una data valida.")


def parse_time(value: str) -> time:
    try:
        return datetime.strptime((value or "").strip(), "%H:%M").time()
    except ValueError:
        raise BookingError("Seleziona un orario.")


def normalize_phone(raw: str) -> str:
    """Numero italiano nel formato "+39 3331234567". Accetta spazi, trattini e prefisso +39/0039."""
    digits = re.sub(r"[\s./()-]", "", raw or "")
    if digits.startswith("+39"):
        digits = digits[3:]
    elif digits.startswith("0039"):
        digits = digits[4:]
    if not re.fullmatch(r"[03]\d{5,10}", digits):
        raise BookingError("Inserisci un numero di telefono italiano valido (es. +39 333 1234567).")
    return f"+39 {digits}"


def normalize_code(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


@dataclass
class Booking:
    code: str
    day: date
    start: time
    end: time
    name: str
    phone: str
    service: str
    status: str = STATUS_CONFIRMED
    notes: str = ""
    price: float | None = None  # prezzo applicato al momento della prenotazione
    email: str = ""

    @property
    def active(self) -> bool:
        return self.status.strip().lower() != STATUS_CANCELLED

    @property
    def starts_at(self) -> datetime:
        return datetime.combine(self.day, self.start)

    @property
    def day_label(self) -> str:
        return format_day_it(self.day)

    @property
    def duration(self) -> int:
        return int((datetime.combine(self.day, self.end) - self.starts_at).total_seconds() // 60)

    def amount(self, settings: Settings) -> float:
        """Importo dell'appuntamento: prezzo registrato o, per righe senza prezzo, quello di listino."""
        if self.price is not None:
            return self.price
        svc = settings.service(self.service, include_inactive=True)
        return svc.price if svc and svc.price is not None else 0.0

    def overlaps(self, day: date, start: time, end: time) -> bool:
        return self.day == day and self.start < end and start < self.end

    def fits(self, settings: Settings) -> bool:
        """True se l'appuntamento rientra negli orari attualmente configurati."""
        return any(_t(a) <= self.start and self.end <= _t(b) for a, b in settings.windows_for(self.day))


# --------------------------------------------------------------------------
# Markdown <-> Booking
# --------------------------------------------------------------------------

def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]


def _cell(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    return value.replace("|", "\\|")


def _parse_day(value: str) -> date:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError(f"data non valida: {value!r}")


def _parse_price(value: str) -> float | None:
    value = (value or "").replace("€", "").strip()
    if not value:
        return None
    if "," in value:  # formato italiano 1.234,50
        value = value.replace(".", "").replace(",", ".")
    return round(float(value), 2)


def _row_to_booking(fields: dict[str, str]) -> Booking:
    start = _t(fields["start"].strip())
    end_raw = fields.get("end", "").strip()
    phone, email = fields.get("phone", "").strip(), fields.get("email", "").strip()
    legacy = fields.get("contact", "").strip()
    if legacy and "@" in legacy:
        email = email or legacy
    elif legacy:
        phone = phone or legacy
    return Booking(
        code=normalize_code(fields.get("code", "")),
        day=_parse_day(fields["day"]),
        start=start,
        end=_t(end_raw) if end_raw else _add_minutes(start, DEFAULT_DURATION),
        name=fields.get("name", "").strip(),
        phone=phone,
        email=email,
        service=fields.get("service", "").strip(),
        status=(fields.get("status", "").strip() or STATUS_CONFIRMED).lower(),
        notes=fields.get("notes", "").strip(),
        price=_parse_price(fields.get("price", "")),
    )


def parse_agenda(text: str) -> tuple[list[Booking], list[str]]:
    """Restituisce (prenotazioni, righe di tabella non interpretabili)."""
    bookings: list[Booking] = []
    unparsed: list[str] = []
    header: list[str | None] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            if header is not None and stripped:
                header = None  # la tabella è finita
            continue
        cells = _split_row(stripped)
        if header is None:
            keys = [_HEADER_ALIASES.get(c.lower()) for c in cells]
            if "day" in keys and "start" in keys:
                header = keys
            continue
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue  # riga separatrice
        fields = {k: v for k, v in zip(header, cells) if k}
        try:
            bookings.append(_row_to_booking(fields))
        except (KeyError, ValueError):
            unparsed.append(stripped)
    return bookings, unparsed


def render_agenda(bookings: list[Booking], unparsed: list[str], shop_name: str, now: datetime) -> str:
    lines = [
        "# Agenda Prenotazioni",
        "",
        f"{shop_name} — file sincronizzato automaticamente dalla web app.",
        f"Ultimo aggiornamento: {now:%d/%m/%Y %H:%M}",
        "",
        "| " + " | ".join(COLUMNS) + " |",
        "|" + "|".join("---" for _ in COLUMNS) + "|",
    ]
    for b in sorted(bookings, key=lambda b: (b.day, b.start, b.code)):
        values = [b.code, b.day.isoformat(), f"{b.start:%H:%M}", f"{b.end:%H:%M}",
                  b.name, b.phone, b.email, b.service,
                  "" if b.price is None else f"{b.price:.2f}".replace(".", ","), b.status, b.notes]
        lines.append("| " + " | ".join(_cell(v) for v in values) + " |")
    lines.extend(unparsed)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Agenda
# --------------------------------------------------------------------------

class Agenda:
    def __init__(self, path, settings: SettingsStore, clock=datetime.now):
        """path: percorso di agenda.md oppure un oggetto di storage (LocalFile / GitHubFile)."""
        self.path = path if hasattr(path, "write") else LocalFile(path)
        self.settings = settings
        self._clock = clock
        self._lock = threading.RLock()

    # -- persistenza ------------------------------------------------------
    def _load(self) -> tuple[list[Booking], list[str]]:
        text = self.path.read()
        if text is None:
            return [], []
        return parse_agenda(text)

    def _save(self, bookings: list[Booking], unparsed: list[str], message: str = "Aggiorna agenda") -> None:
        content = render_agenda(bookings, unparsed, self.settings.load().shop_name, self._clock())
        try:
            self.path.write(content, message)
        except StorageConflict:
            raise BookingError("L'agenda è stata appena aggiornata da un'altra parte: riprova.") from None
        except StorageError:
            raise BookingError("Impossibile salvare in questo momento, riprova tra qualche istante.") from None

    # -- consultazione ----------------------------------------------------
    def all(self) -> list[Booking]:
        with self._lock:
            bookings, _ = self._load()
        return sorted(bookings, key=lambda b: (b.day, b.start))

    def get(self, code: str) -> Booking | None:
        code = normalize_code(code)
        if not code:
            return None
        return next((b for b in self.all() if b.code == code), None)

    def now(self) -> datetime:
        return self._clock()

    def available_slots(self, day: date, service: str, exclude_code: str | None = None) -> list[time]:
        with self._lock:
            bookings, _ = self._load()
        return self._free_slots(bookings, self.settings.load(), day, service, normalize_code(exclude_code or ""))

    def _free_slots(self, bookings: list[Booking], settings: Settings, day: date,
                    service_name: str, exclude_code: str) -> list[time]:
        service = settings.service(service_name)
        if service is None:
            raise BookingError("Servizio non valido.")
        now = self._clock()
        if day < now.date() or day > now.date() + timedelta(days=settings.horizon_days):
            return []
        busy = [b for b in bookings
                if b.active and b.day == day and (not b.code or b.code != exclude_code)]
        slots = []
        for open_s, close_s in settings.windows_for(day):
            t, close = _t(open_s), _t(close_s)
            step_start = datetime.combine(day, t)
            while True:
                start_dt = step_start
                end_dt = start_dt + timedelta(minutes=service.duration)
                if end_dt > datetime.combine(day, close):
                    break  # nessun servizio a cavallo della chiusura/pausa
                start, end = start_dt.time(), end_dt.time()
                if start_dt > now and not any(b.overlaps(day, start, end) for b in busy):
                    slots.append(start)
                step_start += timedelta(minutes=settings.slot_step)
        return slots

    # -- modifiche --------------------------------------------------------
    def create(self, *, day: date, start: time, service: str, name: str, phone: str, email: str = "",
               notes: str = "", by_owner: bool = False, force: bool = False) -> Booking:
        """Nuova prenotazione. by_owner: telefono facoltativo; force: ignora orari, chiusure e date passate
        (resta il controllo di sovrapposizione)."""
        name, phone, email, notes = _clean_customer(name, phone, email, notes, phone_required=not by_owner)
        with self._lock:
            bookings, unparsed = self._load()
            svc = self._check_slot(bookings, day, start, service, exclude_code="", force=force)
            booking = Booking(code=_new_code({b.code for b in bookings}), day=day, start=start,
                              end=_add_minutes(start, svc.duration), name=name, phone=phone,
                              email=email, service=svc.name, notes=notes, price=svc.price)
            bookings.append(booking)
            self._save(bookings, unparsed, f"Nuova prenotazione {booking.code}: {day.isoformat()} {start:%H:%M}")
        return booking

    def update(self, code: str, *, day: date, start: time, service: str) -> Booking:
        code = normalize_code(code)
        with self._lock:
            bookings, unparsed = self._load()
            booking = self._editable(bookings, code)
            svc = self._check_slot(bookings, day, start, service, exclude_code=code)
            booking.day, booking.start, booking.service = day, start, svc.name
            booking.end = _add_minutes(start, svc.duration)
            booking.price = svc.price
            booking.status = STATUS_MODIFIED
            self._save(bookings, unparsed, f"Modifica prenotazione {code}")
        return booking

    def cancel(self, code: str) -> Booking:
        code = normalize_code(code)
        with self._lock:
            bookings, unparsed = self._load()
            booking = self._editable(bookings, code)
            booking.status = STATUS_CANCELLED
            self._save(bookings, unparsed, f"Cancellazione prenotazione {code}")
        return booking

    def _editable(self, bookings: list[Booking], code: str) -> Booking:
        booking = next((b for b in bookings if code and b.code == code), None)
        if booking is None:
            raise BookingError("Codice prenotazione non trovato.")
        if not booking.active:
            raise BookingError("Questa prenotazione è già stata cancellata.")
        if booking.starts_at <= self._clock():
            raise BookingError("Non è possibile modificare un appuntamento già iniziato o passato.")
        return booking

    def _check_slot(self, bookings, day: date, start: time, service: str, exclude_code: str, force: bool = False):
        settings = self.settings.load()
        svc = settings.service(service)
        if svc is None:
            raise BookingError("Seleziona un servizio valido.")
        if force:
            end_dt = datetime.combine(day, start) + timedelta(minutes=svc.duration)
            if end_dt.date() != day:
                raise BookingError("L'appuntamento deve terminare entro la mezzanotte.")
            clash = next((b for b in bookings if b.active and (not b.code or b.code != exclude_code)
                          and b.overlaps(day, start, end_dt.time())), None)
            if clash:
                raise BookingError(f"Si sovrappone all'appuntamento di {clash.name} "
                                   f"({clash.start:%H:%M}-{clash.end:%H:%M}).")
            return svc
        if not settings.windows_for(day):
            raise BookingError("Il negozio è chiuso nel giorno selezionato.")
        if start not in self._free_slots(bookings, settings, day, svc.name, exclude_code):
            raise BookingError("L'orario scelto non è più disponibile. Scegline un altro.")
        return svc


def _clean_customer(name: str, phone: str, email: str, notes: str,
                    phone_required: bool = True) -> tuple[str, str, str, str]:
    name = re.sub(r"\s+", " ", name or "").strip()
    phone = (phone or "").strip()
    email = (email or "").strip().lower()
    notes = re.sub(r"\s+", " ", notes or "").strip()
    if len(name) < 2 or len(name) > 60:
        raise BookingError("Inserisci nome e cognome (2-60 caratteri).")
    if phone or phone_required:
        if not phone:
            raise BookingError("Il numero di telefono è obbligatorio.")
        phone = normalize_phone(phone)
    if email and (len(email) > 80 or not re.fullmatch(r"[^@\s|]+@[^@\s|]+\.[^@\s|]+", email)):
        raise BookingError("Inserisci un indirizzo e-mail valido oppure lascia il campo vuoto.")
    if len(notes) > 200:
        raise BookingError("Le note possono contenere al massimo 200 caratteri.")
    return name, phone, email, notes


def _new_code(existing: set[str]) -> str:
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        if code not in existing:
            return code
