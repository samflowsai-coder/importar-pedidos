import os
import sys

# Sob pythonw.exe (Tarefa Agendada do Windows, sem console) o Python deixa
# sys.stdout / sys.stderr como None. O logging do uvicorn escreve em
# sys.stderr no startup -> None.write() lanca AttributeError e o processo
# MORRE imediatamente (a tarefa fica "Ready", nao "Running", e o watchdog
# entra em loop de restart). Rodar com python.exe (console) funciona porque
# ai os streams sao validos. Damos handles validos redirecionando para um
# arquivo -- que ainda serve de log do servidor headless.
if sys.stdout is None or sys.stderr is None:
    _log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(_log_dir, exist_ok=True)
    _console_log = open(
        os.path.join(_log_dir, "server-console.log"),
        "a",
        buffering=1,
        encoding="utf-8",
        errors="replace",
    )
    if sys.stdout is None:
        sys.stdout = _console_log
    if sys.stderr is None:
        sys.stderr = _console_log

from dotenv import load_dotenv

load_dotenv()

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.web.server:app",
        host=os.getenv("PORTAL_HOST", "127.0.0.1"),
        port=int(os.getenv("PORTAL_PORT", "3636")),
        reload=os.getenv("PORTAL_RELOAD", "false").lower() == "true",
    )
