// Caricamento orari liberi, conferme e piccoli aiuti del pannello titolare.
(function () {
  "use strict";

  function initBookingForm(form) {
    var box = form.querySelector("[data-slots]");
    if (!box) return;
    var selected = box.dataset.selected || "";
    var exclude = box.dataset.exclude || "";
    var requestId = 0;

    function message(text) {
      box.innerHTML = "";
      var p = document.createElement("p");
      p.className = "muted";
      p.textContent = text;
      box.appendChild(p);
    }

    function load() {
      var service = form.querySelector('input[name="service"]:checked');
      var date = form.querySelector('input[name="date"]').value;
      if (!date) return message("Seleziona prima un giorno per vedere gli orari liberi.");
      if (!service) return message("Seleziona prima un servizio.");
      var current = form.querySelector('input[name="time"]:checked');
      if (current) selected = current.value;

      var id = ++requestId;
      message("Caricamento orari…");
      var params = new URLSearchParams({ date: date, service: service.value });
      if (exclude) params.set("exclude", exclude);
      fetch("/api/slots?" + params.toString(), { headers: { Accept: "application/json" } })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (id !== requestId) return; // risposta superata da una richiesta più recente
          if (!data.slots || !data.slots.length) return message(data.message || "Nessun orario disponibile.");
          box.innerHTML = "";
          var grid = document.createElement("div");
          grid.className = "slot-grid";
          data.slots.forEach(function (t) {
            var label = document.createElement("label");
            label.className = "slot";
            var input = document.createElement("input");
            input.type = "radio";
            input.name = "time";
            input.value = t;
            input.required = true;
            if (t === selected) input.checked = true;
            var span = document.createElement("span");
            span.textContent = t;
            label.appendChild(input);
            label.appendChild(span);
            grid.appendChild(label);
          });
          box.appendChild(grid);
        })
        .catch(function () {
          if (id === requestId) message("Impossibile caricare gli orari. Riprova.");
        });
    }

    form.addEventListener("change", function (e) {
      if (e.target.name === "service" || e.target.name === "date") load();
    });
    form.addEventListener("submit", function (e) {
      if (!form.querySelector('input[name="time"]:checked')) {
        e.preventDefault();
        box.classList.add("slots--error");
        if (!box.querySelector(".slot-grid")) load();
        box.scrollIntoView({ behavior: "smooth", block: "center" });
        setTimeout(function () { box.classList.remove("slots--error"); }, 1500);
      }
    });
    load();
  }

  function initSettingsForm(form) {
    var counter = form.querySelector("[data-service-count]");
    var addBtn = form.querySelector("[data-add-service]");
    var tbody = form.querySelector("[data-services] tbody");
    if (addBtn && counter && tbody) {
      addBtn.addEventListener("click", function () {
        var template = tbody.querySelector(".row--new");
        var i = parseInt(counter.value, 10);
        var row = template.cloneNode(true);
        row.querySelectorAll("input").forEach(function (input) {
          input.name = input.name.replace(/^svc-\d+-/, "svc-" + i + "-");
          if (input.type === "checkbox") input.checked = true;
          else input.value = "";
        });
        tbody.appendChild(row);
        counter.value = i + 1;
        row.querySelector("input").focus();
      });
    }
    form.querySelectorAll("[data-day-row]").forEach(function (row) {
      var toggle = row.querySelector("[data-day-toggle]");
      function sync() { row.classList.toggle("hours__row--closed", !toggle.checked); }
      toggle.addEventListener("change", sync);
      sync();
    });
  }

  // Pianificazione: clic su una fascia libera -> precompila giorno e ora del modulo
  function initWeek(week) {
    var form = document.querySelector("[data-admin-booking]");
    if (!form) return;
    week.addEventListener("click", function (e) {
      var cell = e.target.closest(".week__open");
      if (!cell) return;
      var start = parseInt(cell.dataset.startMin, 10);
      var end = parseInt(cell.dataset.endMin, 10);
      var rect = cell.getBoundingClientRect();
      var minute = start + Math.floor(((e.clientY - rect.top) / rect.height) * (end - start) / 15) * 15;
      minute = Math.max(start, Math.min(minute, end - 15));
      var hh = String(Math.floor(minute / 60)).padStart(2, "0");
      var mm = String(minute % 60).padStart(2, "0");
      form.querySelector('[name="date"]').value = cell.dataset.day;
      form.querySelector('[name="time"]').value = hh + ":" + mm;
      form.scrollIntoView({ behavior: "smooth", block: "center" });
      form.querySelector('[name="name"]').focus({ preventScroll: true });
    });
  }

  // Pianificazione su smartphone: un giorno alla volta, scelto dalla barra dei giorni
  function initWeeklist(list) {
    var tabs = list.querySelectorAll("[data-day-tab]");
    var panels = list.querySelectorAll("[data-day-panel]");
    if (!tabs.length) return;
    function show(day) {
      tabs.forEach(function (t) {
        var on = t.dataset.dayTab === day;
        t.classList.toggle("is-on", on);
        t.setAttribute("aria-selected", on ? "true" : "false");
      });
      panels.forEach(function (p) { p.hidden = p.dataset.dayPanel !== day; });
    }
    var wanted = new URLSearchParams(location.search).get("data");
    var start = list.querySelector('[data-day-tab="' + wanted + '"]') ||
                list.querySelector(".daybar__day.is-today") || tabs[0];
    list.classList.add("weeklist--tabs");
    show(start.dataset.dayTab);
    tabs.forEach(function (t) {
      t.addEventListener("click", function () { show(t.dataset.dayTab); });
    });

    var form = document.querySelector("[data-admin-booking]");
    list.querySelectorAll("[data-add-day]").forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        if (!form) return;
        e.preventDefault();
        form.querySelector('[name="date"]').value = btn.dataset.addDay;
        form.scrollIntoView({ behavior: "smooth", block: "start" });
        form.querySelector('[name="time"]').focus({ preventScroll: true });
      });
    });
  }

  // Invio guidato: "Apri WhatsApp" segna il promemoria come inviato e prepara il cliente successivo
  function initGuided(box) {
    var open = box.querySelector("[data-guided-open]");
    var form = box.querySelector("[data-guided-mark]");
    if (!open || !form) return;
    var nextUrl = form.querySelector('[name="next"]').value;
    var marked = false;
    function goNext() { if (marked) location.href = nextUrl; }
    open.addEventListener("click", function () {
      // il link si apre normalmente (WhatsApp); intanto registriamo l'invio
      var data = new FormData(form);
      data.append("ajax", "1");
      fetch(form.action, { method: "POST", body: data, credentials: "same-origin", keepalive: true })
        .then(function (r) {
          if (!r.ok) return;
          marked = true;
          open.textContent = "Inviato ✓ — carico il prossimo…";
          // su computer la pagina resta visibile: si passa al prossimo dopo un attimo;
          // su telefono si passa al prossimo quando si torna dall'app WhatsApp
          if (document.visibilityState === "visible") setTimeout(goNext, 1500);
        });
    });
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") goNext();
    });
  }

  // Pulsante "Copia" del codice prenotazione
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve, reject) {  // browser vecchi o pagina non HTTPS
      var area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      document.body.removeChild(area);
      if (ok) resolve(); else reject();
    });
  }
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    var label = btn.querySelector(".copy-btn__label");
    var timer;
    btn.addEventListener("click", function () {
      copyText(btn.dataset.copy).then(function () {
        btn.classList.add("is-copied");
        label.textContent = "Copiato!";
      }, function () {
        label.textContent = "Tieni premuto il codice per copiarlo";
      });
      clearTimeout(timer);
      timer = setTimeout(function () { btn.classList.remove("is-copied"); label.textContent = "Copia"; }, 2000);
    });
  });

  // Popup di conferma prenotazione: si apre da solo; si chiude col pulsante, con Esc o toccando fuori
  document.querySelectorAll("dialog[data-autoshow]").forEach(function (dialog) {
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    dialog.addEventListener("click", function (e) {
      if (e.target === dialog) dialog.close();
    });
  });

  document.querySelectorAll("[data-guided]").forEach(initGuided);
  document.querySelectorAll("[data-week]").forEach(initWeek);
  document.querySelectorAll("[data-weeklist]").forEach(initWeeklist);
  document.querySelectorAll("[data-booking-form]").forEach(initBookingForm);
  document.querySelectorAll("[data-settings-form]").forEach(initSettingsForm);
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.dataset.confirm)) e.preventDefault();
    });
  });
})();
