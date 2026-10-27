"""Avvisi al proprietario quando un cliente prenota, modifica o annulla.

Gli avvisi restano sempre nel Pannello Admin (notifiche.json, locale o su GitHub) e, se configurato,
arrivano anche sul telefono del proprietario:
- ntfy (https://ntfy.sh): app gratuita, notifiche push senza registrazione;
- WhatsApp tramite CallMeBot (https://www.callmebot.com): messaggi gratuiti a se stessi.
"""
from __future__ import annotations

import base64
import json
import logging
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from agenda import Booking, format_day_it
from settings import Settings
from storage import StorageError

log = logging.getLogger(__name__)

MAX_KEPT = 300
EVENTS = {
    "nuova": ("🆕", "Nuova prenotazione"),
    "modifica": ("✏️", "Prenotazione modificata"),
    "cancellazione": ("❌", "Prenotazione annullata"),
}


def _when(b: Booking) -> str:
    return f"{format_day_it(b.day)} alle {b.start:%H:%M}"


def build_event(kind: str, booking: Booking, old: Booking | None = None, via: str = "") -> dict:
    return {
        "id": secrets.token_hex(4),
        "at": datetime.now().isoformat(timespec="seconds"),
        "type": kind,
        "code": booking.code,
        "name": booking.name,
        "phone": booking.phone,
        "service": booking.service,
        "day": booking.day.isoformat(),
        "new": _when(booking),
        "old": _when(old) if old and (old.day, old.start, old.service) != (booking.day, booking.start, booking.service) else "",
        "old_service": old.service if old and old.service != booking.service else "",
        "via": via,
        "read": False,
    }


def message(event: dict) -> tuple[str, str]:
    """(titolo, testo) dell'avviso."""
    icon, title = EVENTS.get(event["type"], ("🔔", "Avviso"))
    lines = [f"{event['name']}" + (f" ({event['phone']})" if event.get("phone") else "")]
    if event["type"] == "modifica" and event.get("old"):
        lines.append(f"Prima: {event['old']}" + (f" · {event['old_service']}" if event.get("old_service") else ""))
        lines.append(f"Ora: {event['new']} · {event['service']}")
    else:
        lines.append(f"{event['new']} · {event['service']}")
    lines.append(f"Codice {event['code']}")
    return f"{icon} {title}", "\n".join(lines)


class NotificationStore:
    def __init__(self, file):
        self.file = file
        self._lock = threading.RLock()

    def all(self) -> list[dict]:
        with self._lock:
            text = self.file.read()
            try:
                return json.loads(text) if text else []
            except json.JSONDecodeError:
                return []

    def unread_count(self) -> int:
        try:
            return sum(1 for e in self.all() if not e.get("read"))
        except StorageError:
            return 0

    def add(self, event: dict) -> None:
        with self._lock:
            items = [event] + self.all()
            self.file.write(json.dumps(items[:MAX_KEPT], ensure_ascii=False, indent=1),
                            f"Avviso: {event['type']} {event.get('code', '')}")

    def mark_all_read(self) -> None:
        with self._lock:
            items = self.all()
            if any(not e.get("read") for e in items):
                for e in items:
                    e["read"] = True
                self.file.write(json.dumps(items, ensure_ascii=False, indent=1), "Avvisi letti")


# --------------------------------------------------------------------------
# Invio al telefono del proprietario
# --------------------------------------------------------------------------

def _request(url: str, data: bytes | None = None, headers: dict | None = None) -> None:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def send_ntfy(topic: str, title: str, text: str, click_url: str = "") -> None:
    # il titolo contiene emoji e accenti: ntfy accetta l'intestazione codificata RFC 2047
    headers = {"Title": "=?UTF-8?B?" + base64.b64encode(title.encode("utf-8")).decode("ascii") + "?=",
               "Tags": "scissors", "Priority": "high"}
    if click_url:
        headers["Click"] = click_url
    _request(f"https://ntfy.sh/{urllib.parse.quote(topic)}", text.encode("utf-8"), headers)


def send_callmebot(phone: str, apikey: str, title: str, text: str) -> None:
    query = urllib.parse.urlencode({"phone": phone, "text": f"{title}\n{text}", "apikey": apikey})
    _request(f"https://api.callmebot.com/whatsapp.php?{query}")


def push(settings: Settings, event: dict, click_url: str = "") -> list[str]:
    """Invia l'avviso ai canali configurati. Restituisce gli errori (lista vuota se tutto ok)."""
    title, text = message(event)
    errors = []
    if settings.notify_ntfy_topic:
        try:
            send_ntfy(settings.notify_ntfy_topic, title, text, click_url)
        except (urllib.error.URLError, OSError) as exc:
            errors.append(f"ntfy: {exc}")
    if settings.notify_callmebot_phone and settings.notify_callmebot_apikey:
        try:
            send_callmebot(settings.notify_callmebot_phone, settings.notify_callmebot_apikey, title, text)
        except (urllib.error.URLError, OSError) as exc:
            errors.append(f"WhatsApp: {exc}")
    for e in errors:
        log.warning("Avviso non consegnato (%s)", e)
    return errors


def notify(store: NotificationStore, settings: Settings, event: dict, click_url: str = "",
           background: bool = True) -> None:
    """Registra l'avviso nel pannello e lo invia al telefono (in background, per non rallentare il cliente)."""
    try:
        store.add(event)
    except StorageError as exc:
        log.error("Avviso non salvato: %s", exc)
    if event["type"] not in settings.notify_events:
        return
    if background:
        threading.Thread(target=push, args=(settings, event, click_url), daemon=True).start()
    else:
        push(settings, event, click_url)
