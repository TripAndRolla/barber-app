"""Web app di prenotazione — Barber Shop Stefano Stella.

Avvio:  python app.py   (poi apri http://127.0.0.1:5000)
"""
from __future__ import annotations

import hmac
import os
import secrets
from datetime import timedelta
from pathlib import Path

from flask import (Flask, abort, current_app, flash, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

from agenda import Agenda, BookingError, format_day_it, normalize_code, parse_date, parse_time
import customer_area
import notifications
from accounts import AccountStore
from settings import SettingsStore, format_price
from storage import open_data_file

BASE_DIR = Path(__file__).resolve().parent


def _default_agenda_path() -> Path:
    """agenda.md del progetto: la prima trovata risalendo dalla cartella dell'app."""
    for folder in [BASE_DIR, *BASE_DIR.parents[:3]]:
        candidate = folder / "agenda.md"
        if candidate.exists():
            return candidate
    return BASE_DIR / "agenda.md"


def _secret_key() -> str:
    if os.environ.get("BARBER_SECRET_KEY"):
        return os.environ["BARBER_SECRET_KEY"]
    key_file = BASE_DIR / ".secret_key"
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32), encoding="utf-8")
    return key_file.read_text(encoding="utf-8").strip()


import promemoria_whatsapp  # noqa: E402

promemoria_whatsapp.load_env_file()  # configurazione WhatsApp (promemoria.env), se presente

ON_RENDER = os.environ.get("RENDER") == "true"  # variabile impostata automaticamente da Render

app = Flask(__name__)
app.config["SECRET_KEY"] = _secret_key()
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
if ON_RENDER or os.environ.get("BEHIND_PROXY") == "1":
    # dietro il proxy HTTPS: IP reale del cliente (blocco tentativi di login) e cookie solo su HTTPS
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config["SESSION_COOKIE_SECURE"] = True

if ON_RENDER and not (os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPO")):
    app.logger.warning("GITHUB_TOKEN/GITHUB_REPO non impostati: su Render i dati verranno persi al riavvio!")

_settings_store = SettingsStore(open_data_file(
    os.environ.get("BARBER_SETTINGS_PATH", BASE_DIR / "settings.json"), "settings.json"))
app.extensions["settings"] = _settings_store
app.extensions["agenda"] = Agenda(open_data_file(
    os.environ.get("BARBER_AGENDA_PATH", _default_agenda_path()), "agenda.md"), _settings_store)
app.extensions["notifications"] = notifications.NotificationStore(open_data_file(
    os.environ.get("BARBER_NOTIFICATIONS_PATH", BASE_DIR / "notifiche.json"), "notifiche.json"))
app.extensions["accounts"] = AccountStore(open_data_file(
    os.environ.get("BARBER_ACCOUNTS_PATH", BASE_DIR / "clienti.json"), "clienti.json"))


def get_agenda() -> Agenda:
    return current_app.extensions["agenda"]


def get_settings_store() -> SettingsStore:
    return current_app.extensions["settings"]


# --------------------------------------------------------------------------
# CSRF e helper per i template
# --------------------------------------------------------------------------

def csrf_token() -> str:
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


@app.before_request
def csrf_protect():
    if request.method == "POST":
        sent, expected = request.form.get("_csrf", ""), session.get("_csrf", "")
        if not expected or not hmac.compare_digest(sent, expected):
            abort(400, "Sessione scaduta: ricarica la pagina e riprova.")


@app.context_processor
def inject_globals():
    return {
        "csrf_token": csrf_token,
        "shop": get_settings_store().load(),
        "is_admin": session.get("admin") is True,
        "format_day": format_day_it,
        "today": get_agenda().now().date(),
        "hero_image": _hero_image(),
        "storage_warning": ON_RENDER and not (os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPO")),
    }


def _hero_image() -> str | None:
    """Foto del negozio per la home: basta copiarla in static/img/hero.(jpg|jpeg|png|webp)."""
    for ext in ("webp", "jpg", "jpeg", "png"):
        if (BASE_DIR / "static" / "img" / f"hero.{ext}").exists():
            return url_for("static", filename=f"img/hero.{ext}")
    return None


def _monogram(name: str) -> str:
    """Iniziali per il logo: "Barber Shop Stefano Stella" -> "SS" (ultime due parole)."""
    words = [w for w in (name or "").split() if w[:1].isalpha()]
    return "".join(w[0] for w in words[-2:]).upper() or "B"


app.jinja_env.filters["euro"] = lambda v: format_price(v) or "—"
app.jinja_env.filters["monogram"] = _monogram


def alert_owner(kind: str, booking, old=None) -> None:
    """Avvisa il proprietario di una prenotazione/modifica/cancellazione fatta dal cliente."""
    if kind == "nuova":
        via = "sito"
    else:
        via = "area clienti" if customer_area.owns(booking) else "link con codice"
    event = notifications.build_event(kind, booking, old, via)
    notifications.notify(current_app.extensions["notifications"], get_settings_store().load(), event,
                         click_url=url_for("admin.alerts", _external=True),
                         background=not current_app.testing)


def _date_bounds() -> tuple[str, str]:
    today = get_agenda().now().date()
    return today.isoformat(), (today + timedelta(days=get_settings_store().load().horizon_days)).isoformat()


# --------------------------------------------------------------------------
# Area clienti
# --------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    """Controllo di salute per Render (non tocca i file dati)."""
    return {"status": "ok"}


@app.get("/ping")
def ping():
    """Per UptimeRobot / cron-job.org: una visita ogni 5-10 minuti evita la sospensione del piano free.
    Risposta minima, senza leggere agenda o impostazioni (nessuna chiamata a GitHub)."""
    return "OK", 200, {"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store"}


@app.get("/")
def index():
    min_d, max_d = _date_bounds()
    form = {}
    account = customer_area.current_account()
    if account:  # cliente collegato: dati già compilati
        form = {"name": account["name"], "phone": account["phone"].replace("+39 ", ""), "email": account["email"]}
    return render_template("index.html", form=form, min_date=min_d, max_date=max_d)


@app.get("/api/slots")
def api_slots():
    try:
        day = parse_date(request.args.get("date", ""))
        slots = get_agenda().available_slots(day, request.args.get("service", ""), request.args.get("exclude"))
    except BookingError as exc:
        return jsonify({"slots": [], "message": str(exc)}), 400
    settings = get_settings_store().load()
    message = ""
    reason = settings.closed_reason(day)
    if reason:
        message = f"Il negozio è chiuso in questo giorno: {reason}."
    elif not settings.windows_for(day):
        message = "Il negozio è chiuso in questo giorno."
    elif not slots:
        message = "Nessun orario disponibile in questa data."
    return jsonify({"slots": [f"{t:%H:%M}" for t in slots], "message": message,
                    "day_label": format_day_it(day)})


@app.post("/prenota")
def book():
    form = request.form
    try:
        booking = get_agenda().create(
            day=parse_date(form.get("date")),
            start=parse_time(form.get("time")),
            service=form.get("service", ""),
            name=form.get("name", ""),
            phone=form.get("phone", ""),
            email=form.get("email", ""),
            notes=form.get("notes", ""),
        )
    except BookingError as exc:
        min_d, max_d = _date_bounds()
        return render_template("index.html", form=form, error=str(exc), min_date=min_d, max_date=max_d), 422
    customer_area.link_new_booking(booking)
    alert_owner("nuova", booking)
    flash("created", "event")
    return redirect(url_for("manage", code=booking.code))


@app.route("/gestisci", methods=["GET", "POST"])
def lookup():
    if request.method == "POST":
        code = normalize_code(request.form.get("code", ""))
        if get_agenda().get(code):
            return redirect(url_for("manage", code=code))
        return render_template("lookup.html", error="Nessuna prenotazione trovata con questo codice.",
                               code=request.form.get("code", "")), 404
    return render_template("lookup.html")


@app.get("/gestisci/<code>")
def manage(code: str):
    booking = get_agenda().get(code)
    if booking is None:
        return render_template("lookup.html", error="Nessuna prenotazione trovata con questo codice.",
                               code=code), 404
    min_d, max_d = _date_bounds()
    editable = booking.active and booking.starts_at > get_agenda().now()
    return render_template("manage.html", booking=booking, editable=editable,
                           min_date=min_d, max_date=max_d)


@app.post("/gestisci/<code>/modifica")
def modify(code: str):
    old = get_agenda().get(code)
    try:
        booking = get_agenda().update(code, day=parse_date(request.form.get("date")),
                                      start=parse_time(request.form.get("time")),
                                      service=request.form.get("service", ""))
        alert_owner("modifica", booking, old)
        flash("modified", "event")
    except BookingError as exc:
        flash(str(exc), "error")
    return redirect(url_for("manage", code=normalize_code(code)))


@app.post("/gestisci/<code>/cancella")
def cancel(code: str):
    from_area = request.form.get("next", "").startswith("/area-clienti")  # annullata dall'area clienti
    try:
        booking = get_agenda().cancel(code)
        alert_owner("cancellazione", booking)
        flash("Appuntamento annullato." if from_area else "cancelled", "success" if from_area else "event")
    except BookingError as exc:
        flash(str(exc), "error")
    if from_area:
        return redirect(request.form["next"])
    return redirect(url_for("manage", code=normalize_code(code)))


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
def http_error(err):
    return render_template("error.html", error=err), err.code


from admin import bp as admin_bp  # noqa: E402

app.register_blueprint(admin_bp)
customer_area.init_app(app)


if __name__ == "__main__":
    print(f"Agenda: {app.extensions['agenda'].path}")
    print(f"Impostazioni: {_settings_store.path}")
    print("Pannello Admin: http://127.0.0.1:{}/admin".format(os.environ.get("PORT", "5000")))
    # con PORT impostata (hosting) ascolta su tutte le interfacce; in negozio solo su questo PC
    default_host = "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"
    app.run(host=os.environ.get("HOST", default_host), port=int(os.environ.get("PORT", "5000")),
            debug=os.environ.get("FLASK_DEBUG") == "1")
