"""Promemoria WhatsApp per gli appuntamenti del giorno successivo.

Legge agenda.md (la stessa usata dalla web app) e invia un messaggio a ogni cliente
con un appuntamento attivo domani. Pensato per essere eseguito una volta al giorno
(Utilità di pianificazione di Windows / cron); gli stessi invii si possono fare a mano
dal Pannello Admin (sezione Promemoria). È idempotente: un registro
(promemoria_inviati.json) evita di mandare due volte lo stesso promemoria.

Il testo del messaggio si modifica dal Pannello Admin (segnaposto {quando}, {ora}, {nome}, ...).

Uso:
    python promemoria_whatsapp.py                 # appuntamenti di domani
    python promemoria_whatsapp.py --prova         # mostra i messaggi senza inviarli
    python promemoria_whatsapp.py --data 2026-11-03

Configurazione in promemoria.env (accanto a questo file) o variabili d'ambiente:
    WHATSAPP_PROVIDER=meta | twilio          (vuoto = modalità prova)

    # WhatsApp Cloud API (Meta) - richiede un template approvato
    META_TOKEN=...            token di accesso permanente
    META_PHONE_NUMBER_ID=...  ID del numero mittente
    META_TEMPLATE=promemoria_appuntamento
    META_TEMPLATE_LANG=it
    META_TEMPLATE_PARAMS=ora  variabili del template in ordine ({{1}}, {{2}}, ...)
    META_API_VERSION=v21.0

    # Twilio
    TWILIO_ACCOUNT_SID=...
    TWILIO_AUTH_TOKEN=...
    TWILIO_FROM=+14155238886  numero WhatsApp abilitato
    TWILIO_CONTENT_SID=...    facoltativo: template approvato (HX...), variabili come META_TEMPLATE_PARAMS
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from agenda import MESI, Agenda, Booking, BookingError, normalize_phone  # noqa: E402
from settings import GIORNI, Settings, SettingsStore  # noqa: E402
from storage import StorageError, open_data_file  # noqa: E402

LOG_FILE = BASE_DIR / "promemoria.log"
SENT_FILE = BASE_DIR / "promemoria_inviati.json"
ENV_FILE = BASE_DIR / "promemoria.env"

log = logging.getLogger("promemoria")


class SendError(Exception):
    pass


# --------------------------------------------------------------------------
# Configurazione
# --------------------------------------------------------------------------

def load_env_file(path: Path = ENV_FILE) -> None:
    """Carica KEY=VALUE da un file, senza sovrascrivere le variabili già impostate."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _default_agenda_path() -> Path:
    for folder in [BASE_DIR, *BASE_DIR.parents[:3]]:
        if (folder / "agenda.md").exists():
            return folder / "agenda.md"
    return BASE_DIR / "agenda.md"


def provider_name() -> str:
    """Fornitore configurato ('meta' / 'twilio'), oppure '' se non c'è: in quel caso nessun invio automatico."""
    name = os.environ.get("WHATSAPP_PROVIDER", "").strip().lower()
    return name if name in PROVIDERS else ""


# --------------------------------------------------------------------------
# Messaggio
# --------------------------------------------------------------------------

def to_whatsapp_number(phone: str) -> str:
    """'+39 3331234567' -> '393331234567' (formato internazionale senza '+')."""
    return normalize_phone(phone).replace("+", "").replace(" ", "")


def when_label(day: date, today: date) -> str:
    if day == today:
        return "oggi"
    if day == today + timedelta(days=1):
        return "domani"
    return f"{GIORNI[day.weekday()]} {day.day} {MESI[day.month - 1]}"


def message_params(b: Booking, today: date) -> dict[str, str]:
    return {
        "quando": when_label(b.day, today),
        "ora": f"{b.start:%H.%M}",
        "giorno": f"{GIORNI[b.day.weekday()]} {b.day.day} {MESI[b.day.month - 1]}",
        "data": f"{b.day:%d/%m/%Y}",
        "nome": b.name.split(" ")[0] if b.name else "",
        "nome_completo": b.name,
        "servizio": b.service,
        "codice": b.code,
    }


def message_text(b: Booking, template: str, today: date) -> str:
    return template.format(**message_params(b, today))


def whatsapp_link_to(phone: str, text: str) -> str | None:
    """Link wa.me con il messaggio già scritto: apre WhatsApp sul telefono/PC del titolare."""
    try:
        to = to_whatsapp_number(phone)
    except BookingError:
        return None
    return f"https://wa.me/{to}?text={urllib.parse.quote(text)}"


def whatsapp_link(b: Booking, text: str) -> str | None:
    return whatsapp_link_to(b.phone, text)


# --------------------------------------------------------------------------
# Invio tramite API
# --------------------------------------------------------------------------

def _post(url: str, data: bytes, headers: dict[str, str]) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise SendError(f"HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise SendError(f"connessione non riuscita: {exc.reason}") from None


def _template_values(params: dict[str, str]) -> list[str]:
    keys = [k.strip() for k in os.environ.get("META_TEMPLATE_PARAMS", "ora").split(",") if k.strip()]
    return [params.get(k, "") for k in keys]


def send_meta(to: str, text: str, params: dict[str, str]) -> str:
    """WhatsApp Cloud API. Fuori dalla finestra di 24 ore Meta accetta solo template approvati:
    il template deve contenere lo stesso testo, con le variabili indicate in META_TEMPLATE_PARAMS."""
    token, number_id = os.environ.get("META_TOKEN"), os.environ.get("META_PHONE_NUMBER_ID")
    if not token or not number_id:
        raise SendError("META_TOKEN e META_PHONE_NUMBER_ID sono obbligatori")
    version = os.environ.get("META_API_VERSION", "v21.0")
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": os.environ.get("META_TEMPLATE", "promemoria_appuntamento"),
            "language": {"code": os.environ.get("META_TEMPLATE_LANG", "it")},
            "components": [{"type": "body", "parameters": [
                {"type": "text", "text": v} for v in _template_values(params)
            ]}],
        },
    }
    resp = _post(f"https://graph.facebook.com/{version}/{number_id}/messages",
                 json.dumps(payload).encode("utf-8"),
                 {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    return (resp.get("messages") or [{}])[0].get("id", "")


def send_twilio(to: str, text: str, params: dict[str, str]) -> str:
    sid, auth, sender = (os.environ.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"))
    if not (sid and auth and sender):
        raise SendError("TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN e TWILIO_FROM sono obbligatori")
    form = {"From": f"whatsapp:{sender}", "To": f"whatsapp:+{to}"}
    content_sid = os.environ.get("TWILIO_CONTENT_SID")
    if content_sid:  # template approvato
        form["ContentSid"] = content_sid
        form["ContentVariables"] = json.dumps({str(i + 1): v for i, v in enumerate(_template_values(params))})
    else:
        form["Body"] = text
    basic = base64.b64encode(f"{sid}:{auth}".encode()).decode()
    resp = _post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                 urllib.parse.urlencode(form).encode("utf-8"),
                 {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"})
    return resp.get("sid", "")


PROVIDERS = {"meta": send_meta, "twilio": send_twilio}


# --------------------------------------------------------------------------
# Registro invii
# --------------------------------------------------------------------------

def reminder_key(b: Booking) -> str:
    # include data e ora: se l'appuntamento viene spostato, il nuovo promemoria parte comunque
    return f"{b.code or b.name}|{b.day.isoformat()}|{b.start:%H:%M}"


_sent_store = None


def _sent_file():
    """Registro invii: file locale oppure, con GITHUB_TOKEN/GITHUB_REPO, lo stesso repository dei dati.
    Così pannello online e script sul PC condividono il registro e non mandano doppioni."""
    global _sent_store
    if _sent_store is None or getattr(_sent_store, "local_path", None) != SENT_FILE:
        _sent_store = open_data_file(SENT_FILE, "promemoria_inviati.json")
        _sent_store.local_path = SENT_FILE
    return _sent_store


def load_sent() -> dict:
    text = _sent_file().read()
    if text:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            log.warning("Registro promemoria illeggibile: verrà ricreato")
    return {}


def save_sent(sent: dict, keep_days: int = 60) -> None:
    cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
    sent = {k: v for k, v in sent.items() if k.split("|")[1] >= cutoff}
    try:
        _sent_file().write(json.dumps(sent, ensure_ascii=False, indent=2), "Registro promemoria")
    except StorageError as exc:
        raise SendError(f"registro promemoria non salvato: {exc}") from None


def record_sent(b: Booking, to: str, via: str, message_id: str = "") -> None:
    sent = load_sent()
    sent[reminder_key(b)] = {"to": to, "at": datetime.now().isoformat(timespec="seconds"),
                             "via": via, "id": message_id}
    save_sent(sent)


def send_reminder(b: Booking, settings: Settings, today: date, via: str = "automatico") -> str:
    """Invia il promemoria tramite il fornitore configurato e lo registra. Restituisce il numero usato."""
    send = PROVIDERS.get(provider_name())
    if send is None:
        raise SendError("nessun fornitore WhatsApp configurato (WHATSAPP_PROVIDER)")
    if not b.phone:
        raise SendError("appuntamento senza numero di telefono")
    try:
        to = to_whatsapp_number(b.phone)
    except BookingError:
        raise SendError(f"numero di telefono non valido: {b.phone!r}") from None
    params = message_params(b, today)
    message_id = send(to, settings.reminder_template.format(**params), params)
    record_sent(b, to, via, message_id)
    return to


# --------------------------------------------------------------------------
# Esecuzione giornaliera
# --------------------------------------------------------------------------

def open_agenda() -> Agenda:
    # con GITHUB_TOKEN/GITHUB_REPO legge l'agenda pubblicata online (stesso archivio della web app)
    settings_file = open_data_file(os.environ.get("BARBER_SETTINGS_PATH", BASE_DIR / "settings.json"), "settings.json")
    agenda_file = open_data_file(os.environ.get("BARBER_AGENDA_PATH", _default_agenda_path()), "agenda.md")
    return Agenda(agenda_file, SettingsStore(settings_file))


def run(target: date, dry_run: bool, today: date | None = None) -> int:
    today = today or date.today()
    agenda = open_agenda()
    settings = agenda.settings.load()
    if not dry_run and not provider_name():
        log.warning("WHATSAPP_PROVIDER non configurato (meta/twilio): eseguo in modalità prova")
        dry_run = True

    bookings = [b for b in agenda.all() if b.active and b.day == target]
    log.info("Agenda %s: %d appuntamenti attivi per %s", agenda.path, len(bookings), target.isoformat())
    sent = load_sent()
    ok = skipped = failed = 0
    for b in bookings:
        who = f"{b.start:%H:%M} {b.name} ({b.code or 'senza codice'})"
        if reminder_key(b) in sent:
            log.info("Già inviato: %s", who)
            skipped += 1
            continue
        try:
            to = to_whatsapp_number(b.phone) if b.phone else None
        except BookingError:
            to = None
        if not to:
            log.warning("Telefono mancante o non valido per %s: promemoria non inviato", who)
            skipped += 1
            continue
        if dry_run:
            log.info("[PROVA] %s -> +%s\n%s\n", who, to, message_text(b, settings.reminder_template, today))
            ok += 1
            continue
        try:
            send_reminder(b, settings, today)
        except SendError as exc:
            log.error("Invio fallito a %s (+%s): %s", who, to, exc)
            failed += 1
            continue
        log.info("Inviato a %s (+%s)", who, to)
        ok += 1
    log.info("Fine: %d %s, %d saltati, %d errori", ok, "simulati" if dry_run else "inviati", skipped, failed)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Invia i promemoria WhatsApp per gli appuntamenti di domani.")
    parser.add_argument("--data", help="giorno degli appuntamenti (AAAA-MM-GG); predefinito: domani")
    parser.add_argument("--prova", action="store_true", help="mostra i messaggi senza inviarli")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(LOG_FILE, encoding="utf-8")])
    load_env_file(ENV_FILE)
    try:
        target = date.fromisoformat(args.data) if args.data else date.today() + timedelta(days=1)
    except ValueError:
        parser.error("--data deve essere nel formato AAAA-MM-GG")
    return run(target, args.prova)


if __name__ == "__main__":
    sys.exit(main())
