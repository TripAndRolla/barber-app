"""Configurazione Gunicorn (caricata automaticamente da `gunicorn app:app`)."""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"

# Un solo processo: agenda e impostazioni usano un lock in memoria per evitare
# prenotazioni doppie, quindi più processi potrebbero scrivere in contemporanea.
# I thread bastano ampiamente per il traffico di un negozio (e per i 512 MB del piano free).
workers = 1
worker_class = "gthread"
threads = 4

timeout = 60            # il salvataggio su GitHub può richiedere qualche secondo
graceful_timeout = 20
keepalive = 5

accesslog = "-"
errorlog = "-"
loglevel = "info"
forwarded_allow_ips = "*"  # Render termina l'HTTPS sul proprio proxy
