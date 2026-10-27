"""Area clienti: account con e-mail e password.

Il cliente si registra (nome, e-mail, telefono, password) e accede con e-mail e password.
Vede le prenotazioni fatte mentre era collegato e, dopo aver inserito il codice di una sua
prenotazione (che dimostra che il numero è suo), tutte quelle fatte con lo stesso telefono.
Password dimenticata: la reimposta il titolare dal Pannello Admin (password temporanea).
"""
from __future__ import annotations

from datetime import timedelta
from functools import wraps

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for

import ratelimit
from accounts import AccountError, AccountStore, account_bookings
from agenda import Agenda, normalize_code
from customers import phone_key

bp = Blueprint("customer", __name__, url_prefix="/area-clienti")

SESSION_DAYS = 30


def get_agenda() -> Agenda:
    return current_app.extensions["agenda"]


def get_accounts() -> AccountStore:
    return current_app.extensions["accounts"]


def current_account() -> dict | None:
    account_id = session.get("customer")
    if not account_id:
        return None
    account = get_accounts().get(account_id)
    if account is None:  # account eliminato dal titolare
        session.pop("customer", None)
    return account


def owns(booking) -> bool:
    account = current_account()
    return bool(account and booking and account_bookings(account, [booking]))


def customer_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        account = current_account()
        if account is None:
            return redirect(url_for("customer.login", next=request.full_path))
        if account.get("must_change_password") and request.endpoint not in ("customer.change_password", "customer.logout"):
            return redirect(url_for("customer.change_password"))
        return view(*args, **kwargs)
    return wrapper


@bp.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store"  # dati personali: niente copie nella cache del browser
    return response


def _start_session(account: dict, remember: bool) -> None:
    admin = session.get("admin")
    csrf = session.get("_csrf")
    session.clear()  # nuova sessione all'accesso
    if csrf:
        session["_csrf"] = csrf
    if admin:
        session["admin"] = admin
    session["customer"] = account["id"]
    session.permanent = remember
    get_accounts().touch_login(account["id"])


def _after_login():
    next_url = request.args.get("next", "")
    return redirect(next_url if next_url.startswith("/area-clienti") else url_for("customer.area"))


@bp.route("/accedi", methods=["GET", "POST"])
def login():
    if current_account():
        return redirect(url_for("customer.area"))
    error = None
    form = request.form
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if ratelimit.too_many_attempts("cliente", ip):
            error = "Troppi tentativi non riusciti. Riprova tra qualche minuto."
        else:
            account = get_accounts().authenticate(form.get("email", ""), form.get("password", ""))
            if account:
                _start_session(account, remember=bool(form.get("ricordami")))
                if account.get("must_change_password"):
                    return redirect(url_for("customer.change_password"))
                return _after_login()
            ratelimit.record_failure("cliente", ip)
            error = "E-mail o password non corretti."
    return render_template("customer/login.html", error=error, form=form)


@bp.route("/registrati", methods=["GET", "POST"])
def register():
    if current_account():
        return redirect(url_for("customer.area"))
    error = None
    form = request.form
    if request.method == "POST":
        try:
            if form.get("password", "") != form.get("password2", ""):
                raise AccountError("Le due password non coincidono.")
            account = get_accounts().create(name=form.get("name", ""), email=form.get("email", ""),
                                            phone=form.get("phone", ""), password=form.get("password", ""))
            # facoltativo: codice di una prenotazione già fatta, per vedere subito lo storico
            if form.get("code", "").strip():
                _link_booking(account, form["code"], quiet=True)
            _start_session(account, remember=True)
            flash("Benvenuto! Il tuo account è stato creato.", "success")
            return _after_login()
        except AccountError as exc:
            error = str(exc)
    return render_template("customer/register.html", error=error, form=form)


def _link_booking(account: dict, raw_code: str, quiet: bool = False) -> bool:
    """Collega una prenotazione tramite il suo codice. Se il telefono coincide con quello dell'account,
    il numero è verificato e da lì l'account vede tutte le prenotazioni di quel numero."""
    booking = get_agenda().get(normalize_code(raw_code))
    if booking is None:
        if not quiet:
            flash("Nessuna prenotazione trovata con questo codice.", "error")
        return False
    same_phone = bool(booking.phone and phone_key(booking.phone) == account["phone_key"])
    get_accounts().link_code(account["id"], booking.code, verifies_phone=same_phone)
    if not quiet:
        flash("Prenotazione collegata al tuo account." + (" Ora vedi tutte le prenotazioni del tuo numero." if same_phone else ""),
              "success")
    return True


@bp.post("/collega")
@customer_required
def link():
    ip = request.remote_addr or "?"
    if ratelimit.too_many_attempts("collega", ip):
        flash("Troppi tentativi non riusciti. Riprova tra qualche minuto.", "error")
    elif not _link_booking(current_account(), request.form.get("code", "")):
        ratelimit.record_failure("collega", ip)
    return redirect(url_for("customer.area"))


@bp.route("/password", methods=["GET", "POST"])
@customer_required
def change_password():
    account = current_account()
    forced = bool(account.get("must_change_password"))
    error = None
    if request.method == "POST":
        form = request.form
        try:
            if not forced and not get_accounts().authenticate(account["email"], form.get("current", "")):
                raise AccountError("La password attuale non è corretta.")
            if form.get("new", "") != form.get("new2", ""):
                raise AccountError("Le due nuove password non coincidono.")
            get_accounts().set_password(account["id"], form.get("new", ""))
            flash("Password aggiornata.", "success")
            return redirect(url_for("customer.area"))
        except AccountError as exc:
            error = str(exc)
    return render_template("customer/password.html", error=error, forced=forced)


@bp.post("/esci")
def logout():
    session.pop("customer", None)
    flash("Sei uscito dall'area clienti.", "success")
    return redirect(url_for("index"))


@bp.get("")
@customer_required
def area():
    account = current_account()
    agenda = get_agenda()
    now = agenda.now()
    mine = account_bookings(account, agenda.all())
    upcoming = sorted((b for b in mine if b.active and b.starts_at > now), key=lambda b: b.starts_at)
    history = sorted((b for b in mine if not (b.active and b.starts_at > now)), key=lambda b: b.starts_at, reverse=True)
    return render_template("customer/area.html", account=account, upcoming=upcoming, history=history[:30],
                           visits=sum(1 for b in mine if b.active and b.starts_at <= now))


def link_new_booking(booking) -> None:
    """Prenotazione fatta mentre il cliente è collegato: entra nel suo account."""
    account = current_account()
    if account:
        get_accounts().link_code(account["id"], booking.code)


def init_app(app) -> None:
    app.register_blueprint(bp)
    app.permanent_session_lifetime = timedelta(days=SESSION_DAYS)

    @app.context_processor
    def inject_customer():
        if not session.get("customer"):
            return {"customer_logged": False, "customer_name": ""}
        account = current_account()
        return {"customer_logged": bool(account), "customer_name": account["name"] if account else ""}
