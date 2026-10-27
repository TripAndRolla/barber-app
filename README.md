# Prenotazioni online — Barber Shop Stefano Stella

Web app Flask per prenotare, modificare e annullare appuntamenti, con Pannello Admin riservato al titolare e promemoria WhatsApp automatici.

## Avvio

```powershell
cd webapp
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python app.py
```

Apri <http://127.0.0.1:5000>. Per renderla raggiungibile da altri dispositivi della rete locale: `$env:HOST="0.0.0.0"; .venv\Scripts\python app.py`.

## Clienti

1. Scelgono servizio, giorno e uno degli orari liberi, poi inseriscono nome e cognome, **telefono** (obbligatorio, salvato come `+39 numero`) ed **e-mail** (facoltativa).
2. Ricevono un **codice prenotazione** (es. `K7M2QX`).
3. Da *La mia prenotazione* inseriscono il codice per **modificare** (servizio, giorno, orario) o **annullare**.

Gli orari proposti non si sovrappongono ad altri appuntamenti, non sono nel passato, non cadono in giorni di chiusura o festivi e non finiscono mai oltre la chiusura di una fascia (nessun servizio a cavallo della pausa pranzo).

## Area clienti (`/area-clienti`)

Il cliente accede con il **numero di telefono oppure con il codice di una sua prenotazione** (nessuna password da ricordare) e vede tutte le prenotazioni fatte con quel numero: prossimi appuntamenti con "Modifica", "Annulla" e codice copiabile, storico, e "Nuova prenotazione" con nome e telefono già compilati. Può restare collegato 30 giorni. Dopo 5 tentativi errati l'accesso è bloccato per 5 minuti.

## Avvisi al titolare

Quando un cliente prenota, modifica o annulla (dall'area clienti o dal link con il codice), l'avviso compare nel **Pannello Admin → Avvisi** (campanella con il numero di non letti; storico salvato in `notifiche.json`, online nel repository dati). Le azioni fatte dal titolare non generano avvisi. Dalla stessa pagina si sceglie quali eventi mandare anche sul telefono, tramite:

- **ntfy** (gratuito, nessuna registrazione): installa l'app, iscriviti al canale indicato nella pagina, salva e premi "Invia notifica di prova";
- **WhatsApp con CallMeBot** (gratuito): numero del titolare + apikey ottenuta seguendo le istruzioni su callmebot.com.

## Pannello Admin (`/admin`)

Si apre dal link **Pannello Admin** nel footer e si accede con la **sola password** del titolare: senza password nessuno può entrare (tutte le pagine `/admin` rimandano al login). La password si sceglie al primo accesso, consentito solo dal computer del negozio (localhost); in alternativa si imposta con `BARBER_ADMIN_PASSWORD`. Dopo 5 tentativi errati l'accesso è bloccato per 5 minuti.

1. **Pianificazione**: griglia settimanale (elenco per giorno su smartphone) con navigazione tra le settimane, incasso previsto, chiusure e festività evidenziate. Clic su una fascia libera per precompilare l'**inserimento manuale** (telefono facoltativo, opzione per inserire fuori orario); cancellazione dal riquadro di ogni appuntamento.
2. **Clienti**: anagrafica ricavata dalle prenotazioni (riconoscimento per telefono, poi e-mail), con ricerca, ordinamento, visite, ultima/prossima visita, **spesa dell'anno e storica**; scheda cliente con storico completo e spesa per anno. La spesa conta solo appuntamenti già svolti e non cancellati.
3. **Listino**: prezzi e durate dei servizi, aggiunta/eliminazione, visibilità per i clienti. Ogni prenotazione registra il prezzo del momento, quindi gli aumenti non alterano lo storico.
4. **Orari e chiusure**: due fasce per giorno, chiusure straordinarie e ferie (dal/al con motivo), regole di prenotazione, dati del negozio, cambio password.
   - **Festività nazionali** calcolate automaticamente ogni anno: Capodanno, Epifania, Pasqua, Pasquetta, 25 aprile, 1° maggio, 2 giugno, Ferragosto, 4 ottobre (San Francesco, festivo dal 2026), Ognissanti, Immacolata, Natale, Santo Stefano. Tutte chiuse di default; si può togliere la spunta per lavorare in una di esse.
   - **Festività locali** ricorrenti (es. santo patrono): giorno e mese, si ripetono ogni anno.
   - Le prossime chiusure e festività sono mostrate ai clienti in home.

Le impostazioni sono salvate in `settings.json`.

## Sincronizzazione con agenda.md

`agenda.md` è l'unica fonte dati per le prenotazioni: viene riletto a ogni operazione e riscritto in modo atomico, ordinato per data e ora. Colonne: `Codice | Data | Ora inizio | Ora fine | Cliente | Telefono | Email | Servizio | Prezzo | Stato | Note`. Le cancellazioni restano nel file con stato `cancellata`. I file nel vecchio formato (colonna unica `Contatto`) vengono letti senza problemi. Righe aggiunte a mano (anche senza codice o ora di fine) vengono rispettate e bloccano lo slot.

Per impostazione predefinita l'app usa il primo `agenda.md` trovato risalendo dalla sua cartella (cioè quello alla radice del progetto). Si può forzare con `BARBER_AGENDA_PATH`.

| Variabile | Uso |
|---|---|
| `BARBER_AGENDA_PATH` | percorso di `agenda.md` |
| `BARBER_SETTINGS_PATH` | percorso di `settings.json` |
| `BARBER_ADMIN_PASSWORD` | password del Pannello Admin (sostituisce quella salvata) |
| `BARBER_SECRET_KEY` | chiave delle sessioni (altrimenti generata in `.secret_key`) |
| `HOST`, `PORT` | indirizzo e porta del server |

## Promemoria WhatsApp

Il promemoria si può inviare **in automatico** (script giornaliero, sotto) oppure **a mano dal Pannello Admin → Promemoria**: per ogni appuntamento del giorno scelto (predefinito: domani) c'è "Apri WhatsApp", che apre la chat del cliente con il messaggio già scritto e funziona anche senza API, più "Segna come inviato"; con un fornitore configurato compaiono anche "Invia ora" e "Invia a tutti". Nella stessa pagina si modifica il **testo del messaggio**, con anteprima; segnaposto disponibili: `{quando}` (domani/oggi/data), `{ora}` (17.30), `{giorno}`, `{data}`, `{nome}`, `{nome_completo}`, `{servizio}`, `{codice}`.

`promemoria_whatsapp.py` legge `agenda.md` e invia un messaggio a ogni cliente con un appuntamento **attivo per il giorno dopo** (salta cancellati e righe senza telefono). Un registro (`promemoria_inviati.json`) evita doppioni anche se lo script gira più volte; se un invio fallisce viene ritentato alla prossima esecuzione. Log in `promemoria.log`.

```powershell
.venv\Scripts\python promemoria_whatsapp.py --prova          # mostra i messaggi, non invia nulla
.venv\Scripts\python promemoria_whatsapp.py                  # invia per domani
.venv\Scripts\python promemoria_whatsapp.py --data 2026-11-03
```

**Configurazione**: copia `promemoria.env.example` in `promemoria.env` e scegli il fornitore.

- **WhatsApp Cloud API (Meta)**: serve un account WhatsApp Business su Meta for Developers (numero mittente, token permanente) e un **template approvato** in italiano con 5 variabili (nome, giorno, ora, servizio, codice). WhatsApp non permette messaggi liberi a clienti che non ti hanno scritto nelle ultime 24 ore.
- **Twilio**: SID, token e numero WhatsApp abilitato; con `TWILIO_CONTENT_SID` usa un template approvato, altrimenti invia testo libero (valido solo nel sandbox o nella finestra di 24 ore).

Senza fornitore configurato lo script resta in modalità prova.

**Esecuzione automatica ogni giorno alle 18:00** (attività "Promemoria WhatsApp Barber", già creata su questo PC). Per ricrearla da PowerShell:

```powershell
$dir = "C:\Users\david\Desktop\Claude Code\SubAgents\.claude\webapp"
Register-ScheduledTask -TaskName "Promemoria WhatsApp Barber" `
  -Action (New-ScheduledTaskAction -Execute "$dir\promemoria.bat" -WorkingDirectory $dir) `
  -Trigger (New-ScheduledTaskTrigger -Daily -At 18:00) `
  -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable)
```

Se il PC è spento alle 18:00 l'invio parte appena viene riacceso.

## Grafica

Stile monocromatico minimal (grigio caldo, schede bianche arrotondate, pulsanti neri a pillola, font Urbanist), con tema scuro automatico. Per mettere una **foto del negozio** nella testata della home basta copiarla in `static/img/hero.jpg` (o `.webp`/`.png`): viene usata automaticamente con una sfumatura scura per il testo.

## Pubblicazione gratuita su Render

File inclusi: `requirements.txt` (Flask + Gunicorn), `gunicorn.conf.py`, `Procfile`, `render.yaml` (Blueprint, piano **free**, regione Francoforte, fuso orario Europe/Rome, controllo di salute su `/healthz`).

**Perché serve un repository per i dati.** Sul piano gratuito di Render il disco non è permanente: si azzera a ogni riavvio, deploy o "risveglio". Per non perdere le prenotazioni, online `agenda.md` e `settings.json` vengono salvati in un **repository GitHub privato** (ogni modifica è un commit, quindi c'è anche lo storico completo). Senza le variabili `GITHUB_*` l'app usa i file locali come in negozio.

1. **Repository del codice**: crea su GitHub un repository (anche privato), ad es. `barber-app`, e carica il contenuto di questa cartella:
   ```powershell
   cd "C:\Users\david\Desktop\Claude Code\SubAgents\.claude\webapp"
   git init; git add .; git commit -m "Web app prenotazioni"
   git branch -M main
   git remote add origin https://github.com/TUO-UTENTE/barber-app.git
   git push -u origin main
   ```
   (`.gitignore` esclude ambiente virtuale, password, chiavi e registri.)
2. **Repository dei dati**: crea un secondo repository **privato**, ad es. `barber-dati`, spuntando "Add a README". Deve essere separato dal codice, altrimenti ogni prenotazione farebbe ripartire il deploy. Facoltativo: carica lì l'`agenda.md` attuale.
3. **Token GitHub**: Settings → Developer settings → Fine-grained tokens → Generate. Accesso solo al repository `barber-dati`, permesso **Contents: Read and write**. Annota la scadenza: alla scadenza va rigenerato e aggiornato su Render.
4. **Render**: registrati su <https://render.com> con GitHub → **New + → Blueprint** → scegli `barber-app`. Render legge `render.yaml` e chiede:
   - `BARBER_ADMIN_PASSWORD`: la password del Pannello Admin;
   - `GITHUB_TOKEN`: il token del punto 3;
   - `GITHUB_REPO`: `TUO-UTENTE/barber-dati`.
5. Dopo il primo deploy il sito è su `https://barber-stefano-stella.onrender.com` (il nome esatto è nella dashboard): apribile da qualsiasi smartphone, con HTTPS.

**Evitare la sospensione**: il piano gratuito si sospende dopo 15 minuti senza visite. Crea un monitor gratuito su <https://uptimerobot.com> (HTTP(s), intervallo 5 minuti) oppure su <https://cron-job.org> (ogni 10 minuti) verso `https://…onrender.com/ping`, che risponde `OK` senza leggere i dati. Un servizio sempre attivo usa circa 744 ore al mese, entro le 750 ore gratuite di Render (una sola app gratuita attiva per account).

**Limiti del piano gratuito**: senza monitor, dopo 15 minuti senza visite il servizio si sospende e la prima apertura successiva richiede circa un minuto; il salvataggio di una prenotazione impiega circa un secondo (commit su GitHub). Le prenotazioni contengono dati personali (nome, telefono): il repository dei dati deve restare **privato**.

**Promemoria WhatsApp con l'app online**: aggiungi a `promemoria.env` sul PC del negozio `GITHUB_TOKEN`, `GITHUB_REPO` (gli stessi di Render) e `PUBLIC_URL=https://...onrender.com`: lo script leggerà le prenotazioni fatte online e i messaggi conterranno il link per modificarle.

## Test

```powershell
.venv\Scripts\python -m pytest
```
