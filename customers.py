"""Anagrafica clienti ricavata dalle prenotazioni in agenda.md.

Un cliente è identificato dal telefono (normalizzato) o, in mancanza, dall'e-mail;
le righe senza contatto vengono raggruppate per nome.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from agenda import Booking
from settings import Settings


def customer_key(b: Booking) -> str:
    digits = re.sub(r"\D", "", b.phone)
    if digits:
        if digits.startswith("0039"):
            digits = digits[4:]
        elif b.phone.strip().startswith("+39") or (digits.startswith("39") and len(digits) == 12):
            digits = digits[2:]
        return digits
    if b.email.strip():
        return b.email.strip().lower()
    return "nome:" + re.sub(r"\s+", " ", b.name.strip().lower())


def customer_id(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


@dataclass
class Customer:
    id: str
    name: str
    phone: str
    email: str
    bookings: list[Booking] = field(default_factory=list)  # dal più recente
    visits: int = 0            # appuntamenti già svolti (non cancellati)
    cancelled: int = 0
    spend_total: float = 0.0   # solo appuntamenti svolti
    spend_year: float = 0.0
    last_visit: Booking | None = None
    next_booking: Booking | None = None


def build_customers(bookings: list[Booking], settings: Settings, now: datetime) -> list[Customer]:
    groups: dict[str, list[Booking]] = {}
    for b in bookings:
        groups.setdefault(customer_key(b), []).append(b)

    customers = []
    for key, items in groups.items():
        items.sort(key=lambda b: b.starts_at, reverse=True)
        latest = items[0]  # nome e recapiti più recenti
        email = next((b.email for b in items if b.email), "")
        c = Customer(id=customer_id(key), name=latest.name, phone=latest.phone, email=email, bookings=items)
        for b in items:
            if not b.active:
                c.cancelled += 1
            elif b.starts_at <= now:
                c.visits += 1
                amount = b.amount(settings)
                c.spend_total += amount
                if b.day.year == now.year:
                    c.spend_year += amount
                if c.last_visit is None:
                    c.last_visit = b
            else:
                c.next_booking = b  # l'ultimo assegnato è il più vicino
        customers.append(c)
    return customers


def revenue(bookings: list[Booking], settings: Settings, now: datetime, since: date) -> float:
    return sum(b.amount(settings) for b in bookings if b.active and since <= b.day and b.starts_at <= now)
