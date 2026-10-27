"""Dove vengono salvati agenda.md e settings.json.

- LocalFile: file su disco (uso in negozio / sviluppo).
- GitHubFile: file in un repository GitHub privato, letto e scritto tramite API.
  Serve sugli hosting gratuiti (es. Render free) dove il disco si azzera a ogni riavvio.

open_data_file() sceglie automaticamente: se GITHUB_TOKEN e GITHUB_REPO sono impostati usa GitHub,
altrimenti il file locale.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class StorageError(Exception):
    pass


class StorageConflict(StorageError):
    """Il file remoto è cambiato dopo l'ultima lettura (es. modifica manuale su GitHub)."""


class LocalFile:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)

    def read(self) -> str | None:
        if not self.path.exists():
            return None
        return self.path.read_text(encoding="utf-8")

    def write(self, content: str, message: str = "") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.stem}-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def __str__(self) -> str:
        return str(self.path)


class GitHubFile:
    """File in un repository GitHub (Contents API). Ogni scrittura è un commit.

    Le letture sono messe in cache per `cache_seconds` per non superare i limiti dell'API;
    le scritture usano lo SHA dell'ultima versione letta, così una modifica concorrente
    non viene sovrascritta per errore (StorageConflict).
    """
    API = "https://api.github.com"

    def __init__(self, repo: str, path: str, token: str, branch: str = "main", cache_seconds: float = 15):
        self.repo, self.file_path, self.token, self.branch = repo, path.lstrip("/"), token, branch
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._content: str | None = None
        self._sha: str | None = None
        self._fetched_at = 0.0

    def _request(self, method: str, data: dict | None = None):
        url = f"{self.API}/repos/{self.repo}/contents/{urllib.parse.quote(self.file_path)}"
        if method == "GET":
            url += "?" + urllib.parse.urlencode({"ref": self.branch})
        req = urllib.request.Request(
            url, method=method,
            data=json.dumps(data).encode("utf-8") if data is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "barber-booking-app",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            return exc.code, {"message": body}
        except urllib.error.URLError as exc:
            raise StorageError(f"GitHub non raggiungibile: {exc.reason}") from None

    def _fetch(self) -> None:
        status, data = self._request("GET")
        if status == 404:
            self._content, self._sha = None, None
        elif status == 200:
            self._content = base64.b64decode(data["content"]).decode("utf-8")
            self._sha = data["sha"]
        else:
            raise StorageError(f"Lettura di {self} non riuscita (HTTP {status}): {data.get('message', '')}")
        self._fetched_at = time.monotonic()

    def read(self) -> str | None:
        with self._lock:
            if not self._fetched_at or time.monotonic() - self._fetched_at > self.cache_seconds:
                self._fetch()
            return self._content

    def write(self, content: str, message: str = "") -> None:
        with self._lock:
            if not self._fetched_at:
                self._fetch()
            payload = {"message": message or f"Aggiorna {self.file_path}", "branch": self.branch,
                       "content": base64.b64encode(content.encode("utf-8")).decode("ascii")}
            if self._sha:
                payload["sha"] = self._sha
            status, data = self._request("PUT", payload)
            if status in (409, 422):
                self._fetched_at = 0.0  # forza una nuova lettura alla prossima richiesta
                raise StorageConflict(f"{self} è stato modificato altrove: riprova.")
            if status not in (200, 201):
                raise StorageError(f"Salvataggio di {self} non riuscito (HTTP {status}): {data.get('message', '')}")
            self._content, self._sha = content, data["content"]["sha"]
            self._fetched_at = time.monotonic()

    def __str__(self) -> str:
        return f"github:{self.repo}/{self.file_path} ({self.branch})"


def open_data_file(local_path: str | os.PathLike, remote_name: str):
    """GitHubFile se GITHUB_TOKEN e GITHUB_REPO sono configurati, altrimenti LocalFile."""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPO")
    if token and repo:
        folder = os.environ.get("GITHUB_DATA_DIR", "").strip("/")
        return GitHubFile(repo, f"{folder}/{remote_name}" if folder else remote_name, token,
                          branch=os.environ.get("GITHUB_BRANCH", "main"))
    return LocalFile(local_path)
