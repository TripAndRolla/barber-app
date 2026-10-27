"""Account dei clienti (email + password) salvati in clienti.json, locale o nel repository dati.

Le password sono salvate solo come hash. Un account vede le prenotazioni fatte mentre era collegato
e, dopo aver dimostrato di possedere il proprio numero inserendo il codice di una prenotazione,
tutte quelle fatte con quel numero (phone_verified).
"""
from __future__ import annotations

import json
import re
import secrets
import threading
from datetime import datetime

from werkzeug.security import check_password_hash, generate_password_hash

from agenda import BookingError, normalize_phone
from customers import phone_key
from storage import StorageError

MIN_PASSWORD = 8
TEMP_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # senza caratteri ambigui (l/1, o/0)


class AccountError(Exception):
    """Errore mostrato al cliente o al titolare."""


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def check_password_strength(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise AccountError(f"La password deve avere almeno {MIN_PASSWORD} caratteri.")


class AccountStore:
    def __init__(self, file):
        self.file = file
        self._lock = threading.RLock()

    # -- lettura ----------------------------------------------------------
    def all(self) -> list[dict]:
        with self._lock:
            text = self.file.read()
            try:
                return json.loads(text) if text else []
            except json.JSONDecodeError:
                return []

    def get(self, account_id: str | None) -> dict | None:
        return next((a for a in self.all() if account_id and a["id"] == account_id), None)

    def by_email(self, email: str) -> dict | None:
        email = normalize_email(email)
        return next((a for a in self.all() if a["email"] == email), None)

    def for_phone(self, phone: str) -> list[dict]:
        key = phone_key(phone)
        return [a for a in self.all() if key and a.get("phone_key") == key]

    # -- scrittura --------------------------------------------------------
    def _save(self, accounts: list[dict], message: str) -> None:
        try:
            self.file.write(json.dumps(accounts, ensure_ascii=False, indent=1), message)
        except StorageError as exc:
            raise AccountError("Impossibile salvare in questo momento, riprova tra qualche istante.") from exc

    def _update(self, account_id: str, message: str, change) -> dict:
        with self._lock:
            accounts = self.all()
            account = next((a for a in accounts if a["id"] == account_id), None)
            if account is None:
                raise AccountError("Account non trovato.")
            change(account)
            self._save(accounts, message)
            return account

    def create(self, *, name: str, email: str, phone: str, password: str) -> dict:
        name = re.sub(r"\s+", " ", name or "").strip()
        email = normalize_email(email)
        if not 2 <= len(name) <= 60:
            raise AccountError("Inserisci nome e cognome (2-60 caratteri).")
        if len(email) > 80 or not re.fullmatch(r"[^@\s|]+@[^@\s|]+\.[^@\s|]+", email):
            raise AccountError("Inserisci un indirizzo e-mail valido.")
        try:
            phone = normalize_phone(phone)
        except BookingError as exc:
            raise AccountError(str(exc)) from None
        check_password_strength(password)
        with self._lock:
            accounts = self.all()
            if any(a["email"] == email for a in accounts):
                raise AccountError("Esiste già un account con questa e-mail: accedi oppure chiedi al negozio di reimpostare la password.")
            account = {
                "id": secrets.token_hex(6),
                "name": name,
                "email": email,
                "phone": phone,
                "phone_key": phone_key(phone),
                "phone_verified": False,
                "codes": [],
                "password_hash": generate_password_hash(password),
                "must_change_password": False,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "last_login": "",
            }
            accounts.append(account)
            self._save(accounts, "Nuovo account cliente")
        return account

    def authenticate(self, email: str, password: str) -> dict | None:
        account = self.by_email(email)
        if account and check_password_hash(account["password_hash"], password or ""):
            return account
        return None

    def touch_login(self, account_id: str) -> None:
        try:
            self._update(account_id, "Accesso cliente",
                         lambda a: a.update(last_login=datetime.now().isoformat(timespec="seconds")))
        except AccountError:
            pass  # l'orario dell'ultimo accesso non è essenziale

    def set_password(self, account_id: str, password: str) -> None:
        check_password_strength(password)
        self._update(account_id, "Password cliente aggiornata",
                     lambda a: a.update(password_hash=generate_password_hash(password), must_change_password=False))

    def reset_password(self, account_id: str) -> str:
        """Reset fatto dal titolare: password temporanea da comunicare al cliente, da cambiare al primo accesso."""
        temp = "".join(secrets.choice(TEMP_ALPHABET) for _ in range(10))
        self._update(account_id, "Reset password cliente",
                     lambda a: a.update(password_hash=generate_password_hash(temp), must_change_password=True))
        return temp

    def link_code(self, account_id: str, code: str, verifies_phone: bool = False) -> None:
        def change(a):
            if code and code not in a["codes"]:
                a["codes"].append(code)
            if verifies_phone:
                a["phone_verified"] = True
        self._update(account_id, f"Prenotazione {code} collegata all'account", change)

    def delete(self, account_id: str) -> None:
        with self._lock:
            accounts = [a for a in self.all() if a["id"] != account_id]
            self._save(accounts, "Account cliente eliminato")


def account_bookings(account: dict, bookings) -> list:
    """Prenotazioni visibili all'account: collegate esplicitamente o, se il numero è verificato, dello stesso numero."""
    codes = set(account.get("codes", []))
    key = account.get("phone_key") if account.get("phone_verified") else None
    return [b for b in bookings if (b.code and b.code in codes) or (key and b.phone and phone_key(b.phone) == key)]
