@echo off
rem Avvia l'invio dei promemoria WhatsApp (usato dall'Utilita' di pianificazione di Windows).
cd /d "%~dp0"
".venv\Scripts\python.exe" promemoria_whatsapp.py %*
