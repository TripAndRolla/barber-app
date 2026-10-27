"""Pannello Admin: login, pianificazione settimanale, clienti, listino, impostazioni."""
from __future__ import annotations

import hmac
import os
import secrets
from datetime import date, timedelta
from functools import wraps

from flask import (Blueprint, abort, current_app, flash, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from agenda import Agenda, BookingError, parse_date, parse_time
import notifications
import promemoria_whatsapp as reminders
import ratelimit
from customers import build_customers, revenue
from settings import (DEFAULT_REMINDER_TEMPLATE, GIORNI, MAX_WINDOWS_PER_DAY, NATIONAL_HOLIDAYS,
                      REMINDER_PLACEHOLDERS, Closure, LocalHoliday, Service, SettingsError, SettingsStore,
                      national_holiday_dates)

bp = Blueprint("admin", __name__, url_prefix="/admin")

GRID_STEP_MIN = 5  # risoluzione della griglia settimanale


@bp.after_request
def no_cache(response):
    """Le pagine del pannello non vanno conservate dal browser: sempre aggiornate (anche col tasto
    Indietro) e nessun dato di clienti/incassi lasciato nella cache del dispositivo."""
    response.headers["Cache-Control"] = "no-store"
    return response


def get_agenda() -> Agenda:
    return current_app.extensions["agenda"]


def get_store() -> SettingsStore:
    return current_app.extensions["settings"]


# --------------------------------------------------------------------------
# Autenticazione
# --------------------------------------------------------------------------

def _check_password(password: str) -> bool:
    env_pw = os.environ.get("BARBER_ADMIN_PASSWORD")
    if env_pw:
        return hmac.compare_digest(password.encode(), env_pw.encode())
    stored = get_store().load().admin_password_hash
    return bool(stored) and check_password_hash(stored, password)


def _needs_setup() -> bool:
    return not os.environ.get("BARBER_ADMIN_PASSWORD") and not get_store().load().admin_password_hash


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if session.get("admin") is not True:
            return redirect(url_for("admin.login", next=request.full_path))
        return view(*args, **kwargs)
    return wrapper


def _login_session() -> None:
    csrf = session.get("_csrf")
    session.clear()  # evita session fixation
    if csrf:
        session["_csrf"] = csrf
    session["admin"] = True


@bp.route("/login", methods=["GET", "POST"])
def login():
    if _needs_setup():
        return redirect(url_for("admin.setup"))
    error = None
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if ratelimit.too_many_attempts("admin", ip):
            error = "Troppi tentativi falliti. Riprova tra qualche minuto."
        elif _check_password(request.form.get("password", "")):
            _login_session()
            next_url = request.args.get("next", "")
            return redirect(next_url if next_url.startswith("/admin") else url_for("admin.week"))
        else:
            ratelimit.record_failure("admin", ip)
            error = "Password non corretta."
    return render_template("admin/login.html", error=error)


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    if not _needs_setup():
        return redirect(url_for("admin.login"))
    if request.remote_addr not in ("127.0.0.1", "::1"):
        abort(403, "La password del Pannello Admin non è ancora impostata. Online va definita con la "
                   "variabile d'ambiente BARBER_ADMIN_PASSWORD (oppure al primo accesso dal computer del negozio).")
    error = None
    if request.method == "POST":
        pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
        settings = get_store().load()
        if len(pw) < 8:
            error = "La password deve avere almeno 8 caratteri."
        elif pw != pw2:
            error = "Le due password non coincidono."
        else:
            settings.admin_password_hash = generate_password_hash(pw)
            try:
                get_store().save(settings)
            except SettingsError as exc:
                error = str(exc)
            else:
                _login_session()
                return redirect(url_for("admin.week"))
    return render_template("admin/setup.html", error=error)


@bp.post("/logout")
def logout():
    session.pop("admin", None)
    return redirect(url_for("index"))


@bp.get("")
@admin_required
def home():
    return redirect(url_for("admin.week"))


# --------------------------------------------------------------------------
# 1. Pianificazione settimanale
# --------------------------------------------------------------------------

def _minutes(t) -> int:
    return t.hour * 60 + t.minute


def _week_grid(week_start: date, bookings, settings):
    days = [week_start + timedelta(days=i) for i in range(7)]
    by_day = {d: [b for b in bookings if b.day == d and b.active] for d in days}

    # intervallo orario mostrato: tutte le fasce della settimana + eventuali appuntamenti fuori orario
    starts, ends = [], []
    for d in days:
        for a, b in settings.hours.get(d.weekday(), []):
            starts.append(int(a[:2]) * 60 + int(a[3:]))
            ends.append(int(b[:2]) * 60 + int(b[3:]))
        for b in by_day[d]:
            starts.append(_minutes(b.start))
            ends.append(_minutes(b.end))
    grid_start = (min(starts, default=9 * 60) // 60) * 60
    grid_end = -(-max(ends, default=18 * 60) // 60) * 60

    def row(m: int) -> int:
        return (m - grid_start) // GRID_STEP_MIN + 1

    columns = []
    for d in days:
        windows = []
        for a, b in settings.windows_for(d):
            ma, mb = int(a[:2]) * 60 + int(a[3:]), int(b[:2]) * 60 + int(b[3:])
            windows.append({"row_start": row(ma), "row_end": row(mb), "start_min": ma, "end_min": mb})
        items = [{"b": b, "row_start": row(_minutes(b.start)), "row_end": max(row(_minutes(b.end)), row(_minutes(b.start)) + 1),
                  "fits": b.fits(settings)} for b in by_day[d]]
        columns.append({
            "day": d, "name": GIORNI[d.weekday()], "closed": settings.closed_reason(d),
            "windows": windows, "appts": items,
            "revenue": sum(b.amount(settings) for b in by_day[d]),
        })
    hours = [{"label": f"{m // 60:02d}:00", "row": row(m)} for m in range(grid_start, grid_end, 60)]
    return {"columns": columns, "hours": hours, "rows": row(grid_end) - 1,
            "grid_start": grid_start, "step": GRID_STEP_MIN}


@bp.get("/settimana")
@admin_required
def week():
    agenda, settings = get_agenda(), get_store().load()
    today = agenda.now().date()
    try:
        ref = date.fromisoformat(request.args.get("data", "")) if request.args.get("data") else today
    except ValueError:
        ref = today
    week_start = ref - timedelta(days=ref.weekday())
    bookings = agenda.all()
    grid = _week_grid(week_start, bookings, settings)
    week_bookings = [it["b"] for col in grid["columns"] for it in col["appts"]]
    cancelled = [b for b in bookings if not b.active and week_start <= b.day < week_start + timedelta(days=7)]
    now = agenda.now()
    out_of_hours = [b for b in bookings if b.active and b.starts_at > now and not b.fits(settings)]
    prefill = {
        "date": request.args.get("giorno") or (today if week_start <= today < week_start + timedelta(days=7)
                                               else week_start).isoformat(),
        "time": request.args.get("ora", ""),
        "name": request.args.get("nome", ""),
        "phone": request.args.get("telefono", ""),
        "email": request.args.get("email", ""),
        "service": request.args.get("servizio", ""),
    }
    return render_template(
        "admin/week.html", grid=grid, week_start=week_start, week_end=week_start + timedelta(days=6),
        prev_week=(week_start - timedelta(days=7)).isoformat(), next_week=(week_start + timedelta(days=7)).isoformat(),
        this_week=today.isoformat(), count=len(week_bookings),
        week_revenue=sum(b.amount(settings) for b in week_bookings),
        cancelled=cancelled, out_of_hours=out_of_hours, now=now, prefill=prefill,
        customer_names=sorted({b.name for b in bookings if b.name}), agenda_path=agenda.path,
    )


@bp.post("/appuntamenti")
@admin_required
def add_booking():
    form = request.form
    day_raw = form.get("date", "")
    try:
        b = get_agenda().create(
            day=parse_date(day_raw), start=parse_time(form.get("time")), service=form.get("service", ""),
            name=form.get("name", ""), phone=form.get("phone", ""), email=form.get("email", ""),
            notes=form.get("notes", ""),
            by_owner=True, force=bool(form.get("force")),
        )
        flash(f"Appuntamento inserito: {b.name}, {b.day:%d/%m} alle {b.start:%H:%M} (codice {b.code}).", "success")
        return redirect(url_for("admin.week", data=b.day.isoformat()))
    except BookingError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.week", data=day_raw or None, giorno=day_raw or None, ora=form.get("time"),
                                nome=form.get("name"), telefono=form.get("phone"), email=form.get("email"),
                                servizio=form.get("service")) + "#nuovo")


@bp.post("/prenotazioni/<code>/cancella")
@admin_required
def cancel_booking(code: str):
    try:
        b = get_agenda().cancel(code)
        flash(f"Appuntamento di {b.name} del {b.day:%d/%m} alle {b.start:%H:%M} cancellato.", "success")
    except BookingError as exc:
        flash(str(exc), "error")
    back = request.form.get("next", "")
    return redirect(back if back.startswith("/admin") else url_for("admin.week"))


# --------------------------------------------------------------------------
# 2. Anagrafica clienti
# --------------------------------------------------------------------------

@bp.get("/clienti")
@admin_required
def customers():
    agenda, settings = get_agenda(), get_store().load()
    now = agenda.now()
    bookings = agenda.all()
    items = build_customers(bookings, settings, now)
    q = request.args.get("q", "").strip().lower()
    if q:
        items = [c for c in items if q in c.name.lower() or q in c.email.lower()
                 or q.replace(" ", "") in c.phone.replace(" ", "")]
    order = request.args.get("ordina", "nome")
    if order == "spesa":
        items.sort(key=lambda c: (-c.spend_total, c.name.lower()))
    elif order == "anno":
        items.sort(key=lambda c: (-c.spend_year, c.name.lower()))
    elif order == "recenti":
        items.sort(key=lambda c: c.bookings[0].starts_at, reverse=True)
    else:
        items.sort(key=lambda c: c.name.lower())
    return render_template(
        "admin/customers.html", customers=items, q=request.args.get("q", ""), order=order, year=now.year,
        revenue_year=revenue(bookings, settings, now, date(now.year, 1, 1)),
        revenue_total=revenue(bookings, settings, now, date.min),
    )


@bp.get("/clienti/<cid>")
@admin_required
def customer_detail(cid: str):
    agenda, settings = get_agenda(), get_store().load()
    now = agenda.now()
    customer = next((c for c in build_customers(agenda.all(), settings, now) if c.id == cid), None)
    if customer is None:
        abort(404, "Cliente non trovato.")
    by_year: dict[int, float] = {}
    for b in customer.bookings:
        if b.active and b.starts_at <= now:
            by_year[b.day.year] = by_year.get(b.day.year, 0) + b.amount(settings)
    return render_template("admin/customer.html", c=customer, now=now, settings=settings,
                           by_year=sorted(by_year.items(), reverse=True))


# --------------------------------------------------------------------------
# 3. Listino
# --------------------------------------------------------------------------

def _parse_int(raw: str, label: str) -> int:
    try:
        return int((raw or "").strip())
    except ValueError:
        raise SettingsError(f"{label}: inserisci un numero intero.")


def _parse_price(raw: str) -> float | None:
    raw = (raw or "").strip().replace("€", "").replace(",", ".").strip()
    if not raw:
        return None
    try:
        return round(float(raw), 2)
    except ValueError:
        raise SettingsError(f"Prezzo non valido: {raw!r}.")


def _services_from_form(form) -> list[Service]:
    services = []
    for i in range(_parse_int(form.get("service_count", "0"), "Servizi")):
        p = f"svc-{i}-"
        name = form.get(p + "name", "").strip()
        if not name or form.get(p + "delete"):
            continue
        services.append(Service(name=name,
                                duration=_parse_int(form.get(p + "duration"), f"Durata di «{name}»"),
                                price=_parse_price(form.get(p + "price")),
                                active=bool(form.get(p + "active"))))
    return services


def _services_view(services) -> list[dict]:
    return [{"name": s.name, "duration": s.duration,
             "price": "" if s.price is None else f"{s.price:.2f}".replace(".", ","), "active": s.active}
            for s in services]


@bp.route("/listino", methods=["GET", "POST"])
@admin_required
def price_list():
    store = get_store()
    if request.method == "POST":
        settings = store.load()
        try:
            settings.services = _services_from_form(request.form)
            store.save(settings)
            flash("Listino aggiornato. I nuovi prezzi valgono per le prenotazioni da ora in poi.", "success")
            return redirect(url_for("admin.price_list"))
        except SettingsError as exc:
            try:
                count = int(request.form.get("service_count", "0"))
            except ValueError:
                count = 0
            rows = [{"name": request.form.get(f"svc-{i}-name", ""), "duration": request.form.get(f"svc-{i}-duration", ""),
                     "price": request.form.get(f"svc-{i}-price", ""), "active": bool(request.form.get(f"svc-{i}-active"))}
                    for i in range(count) if request.form.get(f"svc-{i}-name", "").strip()
                    and not request.form.get(f"svc-{i}-delete")]
            return render_template("admin/pricelist.html", services=rows, error=str(exc)), 422
    return render_template("admin/pricelist.html", services=_services_view(store.load().services))


# --------------------------------------------------------------------------
# 4. Impostazioni, orari e chiusure
# --------------------------------------------------------------------------

def _hours_view_from_settings(s) -> dict:
    return {d: {"open": bool(s.hours.get(d)),
                "windows": (list(s.hours.get(d, [])) + [("", "")] * MAX_WINDOWS_PER_DAY)[:MAX_WINDOWS_PER_DAY]}
            for d in range(7)}


def _hours_view_from_form(form) -> dict:
    return {d: {"open": bool(form.get(f"day-{d}-open")),
                "windows": [(form.get(f"day-{d}-w{w}-start", ""), form.get(f"day-{d}-w{w}-end", ""))
                            for w in range(MAX_WINDOWS_PER_DAY)]}
            for d in range(7)}


def _hours_from_form(form) -> dict[int, list[tuple[str, str]]]:
    hours = {}
    for d in range(7):
        windows = []
        if form.get(f"day-{d}-open"):
            for w in range(MAX_WINDOWS_PER_DAY):
                start = form.get(f"day-{d}-w{w}-start", "").strip()
                end = form.get(f"day-{d}-w{w}-end", "").strip()
                if start or end:
                    windows.append((start, end))
            if not windows:
                raise SettingsError(f"{GIORNI[d].capitalize()}: indica almeno una fascia oraria o segna il giorno come chiuso.")
        hours[d] = windows
    return hours


def _render_settings(error=None, view=None, status=200):
    settings = get_store().load()
    view = view or {
        "hours": _hours_view_from_settings(settings), "slot_step": settings.slot_step,
        "horizon_days": settings.horizon_days, "shop_name": settings.shop_name,
        "address": settings.address, "phone": settings.phone, "vat_number": settings.vat_number,
    }
    today = get_agenda().now().date()
    closures = sorted(enumerate(settings.closures), key=lambda ic: ic[1].start)
    # prossima ricorrenza di ogni festività nazionale (quest'anno o, se passata, il prossimo)
    upcoming = {}
    for year in (today.year + 1, today.year):
        for key, _, d in national_holiday_dates(year):
            if d >= today or key not in upcoming:
                upcoming[key] = d
    national = [{"key": key, "name": name, "next": upcoming.get(key),
                 "closed": key not in settings.national_holidays_open}
                for key, name, _ in NATIONAL_HOLIDAYS]
    local = sorted(enumerate(settings.local_holidays), key=lambda ih: (ih[1].month, ih[1].day))
    return render_template(
        "admin/settings.html", view=view, giorni=GIORNI, error=error,
        closures=[(i, c, c.end < today) for i, c in closures],
        national=national, local_holidays=local,
        using_env_pw=bool(os.environ.get("BARBER_ADMIN_PASSWORD")),
    ), status


@bp.route("/impostazioni", methods=["GET", "POST"])
@admin_required
def settings_page():
    if request.method == "POST":
        form = request.form
        store = get_store()
        settings = store.load()
        try:
            settings.hours = _hours_from_form(form)
            settings.slot_step = _parse_int(form.get("slot_step"), "Intervallo slot")
            settings.horizon_days = _parse_int(form.get("horizon_days"), "Giorni prenotabili")
            settings.shop_name = form.get("shop_name", "").strip()
            settings.address = form.get("address", "").strip()
            settings.phone = form.get("phone", "").strip()
            settings.vat_number = form.get("vat_number", "").replace(" ", "").strip()
            store.save(settings)
            flash("Orari e impostazioni salvati. Valgono da subito per le prenotazioni online.", "success")
            return redirect(url_for("admin.settings_page"))
        except SettingsError as exc:
            view = {"hours": _hours_view_from_form(form), "slot_step": form.get("slot_step", ""),
                    "horizon_days": form.get("horizon_days", ""), "shop_name": form.get("shop_name", ""),
                    "address": form.get("address", ""), "phone": form.get("phone", ""),
                    "vat_number": form.get("vat_number", "")}
            return _render_settings(str(exc), view, 422)
    return _render_settings()


@bp.post("/chiusure")
@admin_required
def add_closure():
    form = request.form
    store = get_store()
    settings = store.load()
    try:
        start = date.fromisoformat(form.get("start", ""))
        end = date.fromisoformat(form.get("end") or form.get("start", ""))
    except ValueError:
        flash("Indica una data di inizio valida.", "error")
        return redirect(url_for("admin.settings_page") + "#chiusure")
    closure = Closure(start, end, form.get("reason", "").strip())
    settings.closures.append(closure)
    try:
        store.save(settings)
    except SettingsError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.settings_page") + "#chiusure")
    affected = [b for b in get_agenda().all() if b.active and closure.contains(b.day)]
    msg = f"Chiusura aggiunta ({closure.label}). Le prenotazioni online sono bloccate in quei giorni."
    if affected:
        msg += f" Attenzione: ci sono già {len(affected)} appuntamenti in quel periodo, controllali nella pianificazione."
    flash(msg, "success")
    return redirect(url_for("admin.settings_page") + "#chiusure")


@bp.post("/chiusure/<int:index>/elimina")
@admin_required
def delete_closure(index: int):
    store = get_store()
    settings = store.load()
    if 0 <= index < len(settings.closures):
        removed = settings.closures.pop(index)
        store.save(settings)
        flash(f"Chiusura {removed.label} rimossa.", "success")
    return redirect(url_for("admin.settings_page") + "#chiusure")


@bp.post("/festivita")
@admin_required
def national_holidays():
    store = get_store()
    settings = store.load()
    closed = set(request.form.getlist("closed"))
    settings.national_holidays_open = [key for key, _, _ in NATIONAL_HOLIDAYS if key not in closed]
    store.save(settings)
    flash("Festività nazionali aggiornate.", "success")
    return redirect(url_for("admin.settings_page") + "#festivita")


@bp.post("/festivita-locali")
@admin_required
def add_local_holiday():
    store = get_store()
    settings = store.load()
    try:
        d = date.fromisoformat(request.form.get("date", ""))
    except ValueError:
        flash("Indica la data della festività.", "error")
        return redirect(url_for("admin.settings_page") + "#festivita")
    holiday = LocalHoliday(d.month, d.day, request.form.get("name", "").strip())
    if any((h.month, h.day) == (holiday.month, holiday.day) for h in settings.local_holidays):
        flash(f"Esiste già una festività locale il {holiday.label}.", "error")
        return redirect(url_for("admin.settings_page") + "#festivita")
    settings.local_holidays.append(holiday)
    try:
        store.save(settings)
    except SettingsError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.settings_page") + "#festivita")
    today = get_agenda().now().date()
    affected = [b for b in get_agenda().all()
                if b.active and b.day >= today and (b.day.month, b.day.day) == (holiday.month, holiday.day)]
    msg = f"Festività locale aggiunta: {holiday.name} ({holiday.label}, ogni anno)."
    if affected:
        msg += f" Attenzione: ci sono già {len(affected)} appuntamenti in quella data."
    flash(msg, "success")
    return redirect(url_for("admin.settings_page") + "#festivita")


@bp.post("/festivita-locali/<int:index>/elimina")
@admin_required
def delete_local_holiday(index: int):
    store = get_store()
    settings = store.load()
    if 0 <= index < len(settings.local_holidays):
        removed = settings.local_holidays.pop(index)
        store.save(settings)
        flash(f"Festività locale «{removed.name}» rimossa.", "success")
    return redirect(url_for("admin.settings_page") + "#festivita")


@bp.post("/password")
@admin_required
def change_password():
    form = request.form
    store = get_store()
    if os.environ.get("BARBER_ADMIN_PASSWORD"):
        flash("La password è impostata tramite la variabile d'ambiente BARBER_ADMIN_PASSWORD.", "error")
    elif not _check_password(form.get("current", "")):
        flash("Password attuale non corretta.", "error")
    elif len(form.get("new", "")) < 8:
        flash("La nuova password deve avere almeno 8 caratteri.", "error")
    elif form.get("new") != form.get("new2"):
        flash("Le due nuove password non coincidono.", "error")
    else:
        settings = store.load()
        settings.admin_password_hash = generate_password_hash(form["new"])
        store.save(settings)
        flash("Password aggiornata.", "success")
    return redirect(url_for("admin.settings_page") + "#account")


# --------------------------------------------------------------------------
# 5. Promemoria WhatsApp (invio manuale)
# --------------------------------------------------------------------------

def _reminder_day() -> date:
    tomorrow = get_agenda().now().date() + timedelta(days=1)
    try:
        return date.fromisoformat(request.values.get("data", "")) if request.values.get("data") else tomorrow
    except ValueError:
        return tomorrow


def _reminder_rows(day: date) -> list[dict]:
    """Appuntamenti attivi del giorno con testo, link WhatsApp e stato di invio."""
    agenda, settings = get_agenda(), get_store().load()
    today = agenda.now().date()
    sent = reminders.load_sent()
    rows = []
    for b in agenda.all():
        if not (b.active and b.day == day):
            continue
        text = reminders.message_text(b, settings.reminder_template, today)
        key = reminders.reminder_key(b)
        rows.append({"b": b, "key": key, "text": text, "sent": sent.get(key),
                     "link": reminders.whatsapp_link(b, text) if b.phone else None})
    return rows


@bp.get("/promemoria")
@admin_required
def reminders_page():
    settings = get_store().load()
    today = get_agenda().now().date()
    day = _reminder_day()
    rows = _reminder_rows(day)
    sample = rows[0]["b"] if rows else None
    if sample is None:  # anteprima con dati di esempio
        from agenda import Booking
        sample = Booking("K7M2QX", today + timedelta(days=1), parse_time("17:30"), parse_time("18:15"),
                         "Mario Rossi", "+39 3331234567", "Taglio")
    return render_template(
        "admin/reminders.html", rows=rows, day=day, today=today, tomorrow=today + timedelta(days=1),
        provider=reminders.provider_name(),
        preview=reminders.message_text(sample, settings.reminder_template, today),
        template=settings.reminder_template, placeholders=REMINDER_PLACEHOLDERS,
        pending=sum(1 for r in rows if not r["sent"] and r["link"]),
    )


def _find_active(ident: str, day: date):
    """Appuntamento attivo del giorno per codice o per chiave del registro (righe senza codice)."""
    return next((b for b in get_agenda().all() if b.active and b.day == day and ident
                 and (b.code == ident or reminders.reminder_key(b) == ident)), None)


@bp.get("/promemoria/guidato")
@admin_required
def guided_reminders():
    """Invio massivo manuale: un cliente alla volta, WhatsApp si apre con il messaggio già scritto."""
    day = _reminder_day()
    skipped = request.args.getlist("salta")
    rows = [r for r in _reminder_rows(day) if r["link"]]
    queue = [r for r in rows if not r["sent"] and r["key"] not in skipped]
    return render_template(
        "admin/reminders_guided.html", day=day, current=queue[0] if queue else None,
        queue=queue, total=len(rows), done=sum(1 for r in rows if r["sent"]),
        skipped=[r for r in rows if not r["sent"] and r["key"] in skipped],
        without_phone=sum(1 for r in _reminder_rows(day) if not r["link"]),
        skip_next=skipped + ([queue[0]["key"]] if queue else []),
        skipped_keys=skipped,
    )


@bp.post("/promemoria/invia")
@admin_required
def send_reminders():
    agenda, settings = get_agenda(), get_store().load()
    today, day = agenda.now().date(), _reminder_day()
    codes = request.form.getlist("code")
    if request.form.get("tutti"):
        sent = reminders.load_sent()
        targets = [b for b in agenda.all() if b.active and b.day == day and b.phone
                   and reminders.reminder_key(b) not in sent]
    else:
        targets = [b for b in (_find_active(c, day) for c in codes) if b]
    ok, errors = 0, []
    for b in targets:
        try:
            reminders.send_reminder(b, settings, today, via="manuale")
            ok += 1
        except reminders.SendError as exc:
            errors.append(f"{b.name}: {exc}")
    if ok:
        flash(f"Promemoria inviati: {ok}.", "success")
    if errors:
        flash("Non inviati — " + "; ".join(errors[:3]) + (" …" if len(errors) > 3 else ""), "error")
    if not targets:
        flash("Nessun promemoria da inviare.", "error")
    return redirect(url_for("admin.reminders_page", data=day.isoformat()))


@bp.post("/promemoria/segna")
@admin_required
def mark_reminder():
    day = _reminder_day()
    b = _find_active(request.form.get("code", ""), day)
    if b is None:
        flash("Appuntamento non trovato.", "error")
    elif request.form.get("annulla"):
        sent = reminders.load_sent()
        sent.pop(reminders.reminder_key(b), None)
        reminders.save_sent(sent)
        flash(f"Promemoria di {b.name} segnato come da inviare.", "success")
    else:
        try:
            to = reminders.to_whatsapp_number(b.phone)
        except BookingError:
            to = ""
        reminders.record_sent(b, to, "WhatsApp manuale")
        if not request.form.get("ajax"):
            flash(f"Promemoria di {b.name} segnato come inviato.", "success")
    if request.form.get("ajax"):  # chiamata dall'invio guidato: niente pagina, solo esito
        return ("", 204) if b else ("", 404)
    back = request.form.get("next", "")
    return redirect(back if back.startswith("/admin/") else url_for("admin.reminders_page", data=day.isoformat()))


@bp.post("/promemoria/testo")
@admin_required
def reminder_template():
    store = get_store()
    settings = store.load()
    settings.reminder_template = (DEFAULT_REMINDER_TEMPLATE if request.form.get("ripristina")
                                  else request.form.get("template", "").replace("\r\n", "\n").strip())
    try:
        store.save(settings)
        flash("Testo del promemoria salvato.", "success")
    except SettingsError as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin.reminders_page", data=request.form.get("data") or None) + "#testo")


# --------------------------------------------------------------------------
# 6. Avvisi (prenotazioni, modifiche e cancellazioni dei clienti)
# --------------------------------------------------------------------------

def get_notifications() -> notifications.NotificationStore:
    return current_app.extensions["notifications"]


@bp.app_context_processor
def inject_alert_count():
    if session.get("admin") is not True or "notifications" not in current_app.extensions:
        return {"admin_unread": 0}
    return {"admin_unread": get_notifications().unread_count()}


@bp.get("/avvisi")
@admin_required
def alerts():
    store, settings = get_notifications(), get_store().load()
    items = store.all()
    unread_ids = {e["id"] for e in items if not e.get("read")}
    for e in items:
        e["title"], e["text"] = notifications.message(e)
    response = render_template(
        "admin/alerts.html", items=items[:100], unread_ids=unread_ids, settings=settings,
        events=notifications.EVENTS,
        suggested_topic=settings.notify_ntfy_topic or f"barber-stella-{secrets.token_hex(4)}",
    )
    if unread_ids:
        store.mark_all_read()  # visti: la campanella si azzera, nella pagina restano evidenziati
    return response


@bp.post("/avvisi/impostazioni")
@admin_required
def alert_settings():
    store = get_store()
    settings = store.load()
    form = request.form
    settings.notify_events = [k for k in notifications.EVENTS if form.get(f"evento-{k}")]
    settings.notify_ntfy_topic = form.get("ntfy_topic", "").strip()
    settings.notify_callmebot_phone = form.get("callmebot_phone", "").replace(" ", "").strip()
    settings.notify_callmebot_apikey = form.get("callmebot_apikey", "").strip()
    try:
        store.save(settings)
        flash("Impostazioni degli avvisi salvate.", "success")
    except SettingsError as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin.alerts") + "#telefono")


@bp.post("/avvisi/prova")
@admin_required
def alert_test():
    settings = get_store().load()
    if not (settings.notify_ntfy_topic or (settings.notify_callmebot_phone and settings.notify_callmebot_apikey)):
        flash("Configura prima almeno un canale (ntfy o WhatsApp) e salva.", "error")
        return redirect(url_for("admin.alerts") + "#telefono")
    event = {"type": "nuova", "name": "Prova avvisi", "phone": "", "new": "funziona!",
             "service": settings.shop_name, "code": "PROVA"}
    errors = notifications.push(settings, event, url_for("admin.alerts", _external=True))
    if errors:
        flash("Invio di prova non riuscito: " + "; ".join(errors), "error")
    else:
        flash("Notifica di prova inviata: controlla il telefono.", "success")
    return redirect(url_for("admin.alerts") + "#telefono")
