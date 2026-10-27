"""Configurazione del negozio modificabile dal proprietario (settings.json)."""
from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from storage import LocalFile, StorageError

GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
        "agosto", "settembre", "ottobre", "novembre", "dicembre"]
MAX_WINDOWS_PER_DAY = 2

DEFAULT_REMINDER_TEMPLATE = """Promemoria Appuntamento – Stefano Stella Barber Shop

ti ricordiamo il tuo appuntamento presso Stefano Stella Barber Shop per {quando} alle ore {ora}

📍 Merine (LE) Via Lecce 122
📞 Per modifiche o imprevisti: 328 189 0726

Ti aspettiamo per offrirti un servizio di stile e qualità.
— Il Team di Stefano Stella ✂️💈"""

# segnaposto disponibili nel testo del promemoria, con un esempio
REMINDER_PLACEHOLDERS = {
    "quando": "domani",
    "ora": "17.30",
    "giorno": "mercoledì 28 ottobre",
    "data": "28/10/2026",
    "nome": "Mario",
    "nome_completo": "Mario Rossi",
    "servizio": "Taglio",
    "codice": "K7M2QX",
}


def easter_sunday(year: int) -> date:
    """Domenica di Pasqua (calendario gregoriano, algoritmo di Meeus/Jones/Butcher)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


# Festività nazionali italiane: (chiave, nome, regola). La regola è (mese, giorno) oppure
# lo scarto in giorni dalla domenica di Pasqua. Il 4 ottobre è festivo dal 2026 (L. 151/2025).
NATIONAL_HOLIDAYS = [
    ("capodanno", "Capodanno", (1, 1)),
    ("epifania", "Epifania", (1, 6)),
    ("pasqua", "Pasqua", 0),
    ("pasquetta", "Lunedì dell'Angelo (Pasquetta)", 1),
    ("liberazione", "Festa della Liberazione", (4, 25)),
    ("lavoro", "Festa dei Lavoratori", (5, 1)),
    ("repubblica", "Festa della Repubblica", (6, 2)),
    ("ferragosto", "Ferragosto", (8, 15)),
    ("san_francesco", "San Francesco d'Assisi, patrono d'Italia", (10, 4)),
    ("ognissanti", "Ognissanti", (11, 1)),
    ("immacolata", "Immacolata Concezione", (12, 8)),
    ("natale", "Natale", (12, 25)),
    ("santo_stefano", "Santo Stefano", (12, 26)),
]
_HOLIDAY_FROM_YEAR = {"san_francesco": 2026}


def national_holiday_dates(year: int) -> list[tuple[str, str, date]]:
    out = []
    for key, name, rule in NATIONAL_HOLIDAYS:
        if year < _HOLIDAY_FROM_YEAR.get(key, 0):
            continue
        day = easter_sunday(year) + timedelta(days=rule) if isinstance(rule, int) else date(year, *rule)
        out.append((key, name, day))
    return out


class SettingsError(Exception):
    """Errore di validazione mostrato al proprietario."""


def format_price(value: float | None) -> str:
    if value is None:
        return ""
    return f"€ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


@dataclass
class Service:
    name: str
    duration: int  # minuti
    price: float | None = None  # euro; None = non mostrato
    active: bool = True

    @property
    def price_label(self) -> str:
        return format_price(self.price)


@dataclass
class Closure:
    """Chiusura straordinaria o ferie: blocca le prenotazioni dal giorno `start` al giorno `end` inclusi."""
    start: date
    end: date
    reason: str = ""

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end

    @property
    def label(self) -> str:
        if self.start == self.end:
            return f"{self.start:%d/%m/%Y}"
        return f"dal {self.start:%d/%m/%Y} al {self.end:%d/%m/%Y}"


@dataclass
class LocalHoliday:
    """Festività locale che si ripete ogni anno (es. santo patrono)."""
    month: int
    day: int
    name: str

    @property
    def label(self) -> str:
        return f"{self.day} {MESI[self.month - 1]}"

    def on(self, year: int) -> date | None:
        try:
            return date(year, self.month, self.day)
        except ValueError:  # 29 febbraio negli anni non bisestili
            return None


@dataclass
class Settings:
    shop_name: str = "Barber Shop Stefano Stella"
    address: str = "Via Lecce 122, Merine (LE)"
    phone: str = "3281890726"
    vat_number: str = "05377850754"
    services: list[Service] = field(default_factory=lambda: [
        Service("Taglio", 45),
        Service("Barba", 30),
    ])
    # chiave = weekday() (0 lunedì ... 6 domenica), valore = fasce [inizio, fine]
    hours: dict[int, list[tuple[str, str]]] = field(default_factory=lambda: {
        0: [("09:00", "12:45"), ("14:00", "18:00")],
        1: [("09:00", "12:45"), ("14:00", "18:00")],
        2: [("09:00", "12:45"), ("14:00", "18:00")],
        3: [("09:00", "12:45"), ("14:00", "18:00")],
        4: [("09:00", "12:45"), ("14:00", "18:00")],
        5: [("09:00", "12:45")],
        6: [],
    })
    slot_step: int = 15
    horizon_days: int = 60
    closures: list[Closure] = field(default_factory=list)
    national_holidays_open: list[str] = field(default_factory=list)  # festività in cui si lavora
    local_holidays: list[LocalHoliday] = field(default_factory=list)
    reminder_template: str = DEFAULT_REMINDER_TEMPLATE
    admin_password_hash: str = ""

    # -- helper ---------------------------------------------------------
    @property
    def active_services(self) -> list[Service]:
        return [s for s in self.services if s.active]

    def service(self, name: str, include_inactive: bool = False) -> Service | None:
        wanted = (name or "").strip().lower()
        for s in self.services:
            if s.name.lower() == wanted and (s.active or include_inactive):
                return s
        return None

    def closure_for(self, day: date) -> Closure | None:
        return next((c for c in self.closures if c.contains(day)), None)

    def holidays_in(self, year: int) -> dict[date, str]:
        """Festività (nazionali attive + locali) dell'anno: {data: nome}."""
        out = {d: name for key, name, d in national_holiday_dates(year) if key not in self.national_holidays_open}
        for h in self.local_holidays:
            d = h.on(year)
            if d:
                out.setdefault(d, h.name)
        return out

    def holiday_for(self, day: date) -> str | None:
        return self.holidays_in(day.year).get(day)

    def closed_reason(self, day: date) -> str | None:
        """Motivo della chiusura straordinaria del giorno (ferie, festività), se c'è."""
        closure = self.closure_for(day)
        if closure:
            return closure.reason or "Chiusura straordinaria"
        return self.holiday_for(day)

    def windows_for(self, day: date) -> list[tuple[str, str]]:
        if self.closed_reason(day):
            return []
        return self.hours.get(day.weekday(), [])

    def upcoming_closures(self, today: date) -> list[Closure]:
        return sorted((c for c in self.closures if c.end >= today), key=lambda c: c.start)

    def upcoming_holidays(self, today: date, days: int) -> list[tuple[date, str]]:
        """Festività nei prossimi `days` giorni che cadono in un giorno normalmente aperto."""
        end = today + timedelta(days=days)
        found = {}
        for year in range(today.year, end.year + 1):
            found.update(self.holidays_in(year))
        return sorted((d, n) for d, n in found.items() if today <= d <= end and self.hours.get(d.weekday()))

    def hours_summary(self) -> list[tuple[str, str]]:
        """[(giorno, 'hh:mm-hh:mm / hh:mm-hh:mm' oppure 'Chiuso')] per la pagina pubblica."""
        rows = []
        for i, nome in enumerate(GIORNI):
            windows = self.hours.get(i, [])
            label = " / ".join(f"{a} - {b}" for a, b in windows) if windows else "Chiuso"
            rows.append((nome.capitalize(), label))
        return rows

    # -- (de)serializzazione ---------------------------------------------
    def to_json(self) -> dict:
        data = asdict(self)
        data["hours"] = {str(k): [list(w) for w in v] for k, v in self.hours.items()}
        data["closures"] = [{"start": c.start.isoformat(), "end": c.end.isoformat(), "reason": c.reason}
                            for c in sorted(self.closures, key=lambda c: c.start)]
        data["local_holidays"] = [asdict(h) for h in sorted(self.local_holidays, key=lambda h: (h.month, h.day))]
        return data

    @classmethod
    def from_json(cls, data: dict) -> "Settings":
        default = cls()
        closures = []
        for c in data.get("closures", []):
            if isinstance(c, str):  # vecchio formato: singola data
                closures.append(Closure(date.fromisoformat(c), date.fromisoformat(c)))
            else:
                closures.append(Closure(date.fromisoformat(c["start"]), date.fromisoformat(c["end"]),
                                        c.get("reason", "")))
        return cls(
            shop_name=data.get("shop_name", default.shop_name),
            address=data.get("address", default.address),
            phone=data.get("phone", default.phone),
            vat_number=data.get("vat_number", default.vat_number),
            services=[Service(**s) for s in data["services"]] if "services" in data else default.services,
            hours={int(k): [tuple(w) for w in v] for k, v in data["hours"].items()} if "hours" in data else default.hours,
            slot_step=int(data.get("slot_step", default.slot_step)),
            horizon_days=int(data.get("horizon_days", default.horizon_days)),
            closures=closures,
            national_holidays_open=list(data.get("national_holidays_open", [])),
            local_holidays=[LocalHoliday(**h) for h in data.get("local_holidays", [])],
            reminder_template=data.get("reminder_template") or DEFAULT_REMINDER_TEMPLATE,
            admin_password_hash=data.get("admin_password_hash", ""),
        )


class SettingsStore:
    def __init__(self, path):
        """path: percorso di settings.json oppure un oggetto di storage (LocalFile / GitHubFile)."""
        self.path = path if hasattr(path, "write") else LocalFile(path)
        self._lock = threading.RLock()

    def load(self) -> Settings:
        with self._lock:
            text = self.path.read()
            if text is None:
                return Settings()  # valori predefiniti finché il titolare non salva
            return Settings.from_json(json.loads(text))

    def save(self, settings: Settings) -> None:
        with self._lock:
            validate(settings)
            try:
                self.path.write(json.dumps(settings.to_json(), ensure_ascii=False, indent=2) + "\n",
                                "Aggiorna impostazioni")
            except StorageError as exc:
                raise SettingsError(f"Impossibile salvare le impostazioni: {exc}") from None


def _minutes(hhmm: str) -> int:
    if not re.fullmatch(r"\d{2}:\d{2}", hhmm or ""):
        raise SettingsError(f"Orario non valido: {hhmm!r} (usa il formato HH:MM).")
    t = datetime.strptime(hhmm, "%H:%M")
    return t.hour * 60 + t.minute


def validate(settings: Settings) -> None:
    if not settings.shop_name.strip():
        raise SettingsError("Il nome del negozio è obbligatorio.")
    if settings.vat_number and not re.fullmatch(r"\d{11}", settings.vat_number):
        raise SettingsError("La partita IVA deve avere 11 cifre.")
    names = set()
    for s in settings.services:
        if not s.name.strip() or len(s.name) > 40 or "|" in s.name:
            raise SettingsError("Ogni servizio deve avere un nome (max 40 caratteri, senza '|').")
        if s.name.lower() in names:
            raise SettingsError(f"Servizio duplicato: {s.name}.")
        names.add(s.name.lower())
        if not 5 <= s.duration <= 480 or s.duration % 5:
            raise SettingsError(f"Durata di «{s.name}» non valida (5-480 minuti, multipli di 5).")
        if s.price is not None and not 0 <= s.price <= 10000:
            raise SettingsError(f"Prezzo di «{s.name}» non valido.")
    if not settings.active_services:
        raise SettingsError("Deve esserci almeno un servizio attivo.")
    if not settings.reminder_template.strip() or len(settings.reminder_template) > 1000:
        raise SettingsError("Il testo del promemoria è obbligatorio (max 1000 caratteri).")
    try:
        settings.reminder_template.format(**REMINDER_PLACEHOLDERS)
    except KeyError as exc:
        raise SettingsError(f"Segnaposto sconosciuto nel promemoria: {{{exc.args[0]}}}. "
                            f"Disponibili: {', '.join('{' + k + '}' for k in REMINDER_PLACEHOLDERS)}.")
    except (ValueError, IndexError):
        raise SettingsError("Testo del promemoria non valido: controlla le parentesi graffe { }.")
    if settings.slot_step not in (5, 10, 15, 20, 30, 60):
        raise SettingsError("Intervallo tra gli slot non valido.")
    if not 1 <= settings.horizon_days <= 365:
        raise SettingsError("I giorni prenotabili in anticipo devono essere tra 1 e 365.")
    for c in settings.closures:
        if c.end < c.start:
            raise SettingsError("La data di fine chiusura deve essere uguale o successiva a quella di inizio.")
        if len(c.reason) > 60:
            raise SettingsError("Il motivo della chiusura può avere al massimo 60 caratteri.")
    for h in settings.local_holidays:
        if not h.name.strip() or len(h.name) > 40:
            raise SettingsError("Ogni festività locale deve avere un nome (max 40 caratteri).")
        try:
            date(2024, h.month, h.day)  # 2024 è bisestile: accetta anche il 29 febbraio
        except (ValueError, TypeError):
            raise SettingsError("Data della festività locale non valida.")
    for day, windows in settings.hours.items():
        if day not in range(7) or len(windows) > MAX_WINDOWS_PER_DAY:
            raise SettingsError("Configurazione orari non valida.")
        previous_end = -1
        for start, end in windows:
            a, b = _minutes(start), _minutes(end)
            if a >= b:
                raise SettingsError(f"{GIORNI[day].capitalize()}: l'apertura {start} deve precedere la chiusura {end}.")
            if a < previous_end:
                raise SettingsError(f"{GIORNI[day].capitalize()}: le fasce orarie si sovrappongono.")
            previous_end = b
