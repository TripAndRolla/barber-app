"""Area clienti: accesso con telefono + codice di una prenotazione, elenco delle proprie prenotazioni.

Nessuna password da ricordare: il codice prenotazione (mostrato e copiabile dopo ogni prenotazione)
dimostra che il numero è del cliente; da lì vede tutte le prenotazioni fatte con lo stesso telefono.
"""
from __future__ import annotations

from datetime import timedelta
from functools import wraps

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for

import ratelimit
from agenda import Agenda, normalize_code
from customers import phone_key

bp = Blueprint("customer", __name__, url_prefix="/area-clienti")

SESSION_DAYS = 30


def get_agenda() -> Agenda:
    return current_app.extensions["agenda"]


def current_customer() -> str | None:
    """Telefono (normalizzato) del cliente collegato, oppure None."""
    return session.get("customer")


def owns(booking) -> bool:
    return bool(current_customer() and booking and booking.phone and phone_key(booking.phone) == current_customer())


def customer_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_customer():
            return redirect(url_for("customer.login", next=request.full_path))
        return view(*args, **kwargs)
    return wrapper


@bp.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store"  # dati personali: niente copie nella cache del browser
    return response


@bp.route("/accedi", methods=["GET", "POST"])
def login():
    if current_customer():
        return redirect(url_for("customer.area"))
    error = None
    form = request.form
    if request.method == "POST":
        ip = request.remote_addr or "?"
        key = phone_key(form.get("phone", ""))
        booking = get_agenda().get(normalize_code(form.get("code", "")))
        if ratelimit.too_many_attempts("cliente", ip):
            error = "Troppi tentativi non riusciti. Riprova tra qualche minuto."
        elif booking and booking.phone and key and phone_key(booking.phone) == key:
            admin = session.get("admin")
            csrf = session.get("_csrf")
            session.clear()  # nuova sessione all'accesso
            if csrf:
                session["_csrf"] = csrf
            if admin:
                session["admin"] = admin
            session["customer"] = key
            session["customer_name"] = booking.name
            session.permanent = bool(form.get("ricordami"))
            next_url = request.args.get("next", "")
            return redirect(next_url if next_url.startswith("/area-clienti") else url_for("customer.area"))
        else:
            ratelimit.record_failure("cliente", ip)
            error = "Telefono o codice prenotazione non corretti."
    return render_template("customer/login.html", error=error, form=form)


@bp.post("/esci")
def logout():
    for k in ("customer", "customer_name"):
        session.pop(k, None)
    flash("Sei uscito dall'area clienti.", "success")
    return redirect(url_for("index"))


@bp.get("")
@customer_required
def area():
    agenda = get_agenda()
    now = agenda.now()
    mine = [b for b in agenda.all() if b.phone and phone_key(b.phone) == current_customer()]
    upcoming = sorted((b for b in mine if b.active and b.starts_at > now), key=lambda b: b.starts_at)
    history = sorted((b for b in mine if not (b.active and b.starts_at > now)), key=lambda b: b.starts_at, reverse=True)
    profile = max(mine, key=lambda b: b.starts_at) if mine else None
    if profile:
        session["customer_name"] = profile.name
    return render_template("customer/area.html", upcoming=upcoming, history=history[:30], profile=profile,
                           visits=sum(1 for b in mine if b.active and b.starts_at <= now))


def init_app(app) -> None:
    app.register_blueprint(bp)
    app.permanent_session_lifetime = timedelta(days=SESSION_DAYS)

    @app.context_processor
    def inject_customer():
        return {"customer_logged": bool(current_customer()), "customer_name": session.get("customer_name", "")}
