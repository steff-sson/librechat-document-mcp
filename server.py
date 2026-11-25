"""LibreChat Toolbox — lokale Werkzeuge für Dokumentverarbeitung.

Kein Dateisystem-Zugriff auf LibreChat. Kein DB-Zugriff. Kein eval.
Ein- und Ausgabe ausschließlich über LibreChats öffentliche Datei-API.
"""

import os
import io
import json
import time
import uuid
import logging
import re
from contextvars import ContextVar

import jwt
import requests
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from tools import docx_tools, pdf_tools, xlsx_tools, textstats_tools
from mcp.server.fastmcp import Image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("toolbox")

LIBRECHAT_URL = os.environ.get("LIBRECHAT_URL", "http://api:3080")
JWT_SECRET = os.environ.get("JWT_SECRET", "")
TOKEN_TTL_SECONDS = int(os.environ.get("TOOLBOX_TOKEN_TTL", "120"))
HTTP_TIMEOUT = int(os.environ.get("TOOLBOX_HTTP_TIMEOUT", "60"))
MAX_BYTES = 20 * 1024 * 1024
UPLOAD_ENDPOINT = os.environ.get("TOOLBOX_UPLOAD_ENDPOINT", "agents")

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

if not JWT_SECRET:
    log.error("JWT_SECRET fehlt — Datei-Rueckgabe wird fehlschlagen")

# LibreChats checkBan weist Clients ohne Browser-UA als "non_browser" ab und
# sperrt den Nutzer nach mehreren Versuchen.
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

current_headers: ContextVar[dict] = ContextVar("current_headers", default={})

mcp = FastMCP(
    os.environ.get("TOOLBOX_NAME", "LibreChat Document MCP"),
    stateless_http=False,
    json_response=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=["toolbox:8080", "toolbox", "localhost:8080", "127.0.0.1:8080"],
        allowed_origins=["*"],
    ),
)


# --------------------------------------------------------------------------
# HTTP-Bruecke zu LibreChat
# --------------------------------------------------------------------------

_HTTP = requests.Session()
_HTTP.headers["User-Agent"] = BROWSER_UA


def _user_id() -> str:
    uid = current_headers.get().get("x-user-id")
    if not uid:
        raise RuntimeError(
            "X-User-Id fehlt. LibreChat muss den Header setzen "
            "({{LIBRECHAT_USER_ID}} in mcpServers.headers)."
        )
    return uid


def _mint(user_id: str) -> str:
    """Kurzlebiges JWT, wie LibreChat es selbst signiert (HS256, {id})."""
    now = int(time.time())
    return jwt.encode({"id": user_id, "iat": now, "exp": now + TOKEN_TTL_SECONDS}, JWT_SECRET, algorithm="HS256")


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {_mint(_user_id())}"}


def _resolve_file_id(file_ref: str) -> str:
    """Akzeptiert eine file_id (UUID) ODER einen Dateinamen.

    Der Agent kennt oft nur den Dateinamen aus dem Chat. Statt ihn ein zweites
    Werkzeug aufrufen zu lassen, loesen wir das hier auf: die Toolbox holt die
    Dateiliste des Nutzers und sucht den Namen.
    """
    ref = (file_ref or "").strip()
    if not ref:
        raise RuntimeError("Keine Datei angegeben (file_id oder Dateiname).")

    # UUID-Form -> direkt verwenden
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", ref, re.I):
        return ref

    # Sonst: als Dateiname suchen
    r = _HTTP.get(f"{LIBRECHAT_URL}/api/files/", headers=_auth_headers(), timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(
            f"Datei '{ref}' ist keine gueltige Datei-ID und die Dateiliste "
            f"ist nicht abrufbar ({r.status_code})."
        )
    files = r.json() or []
    needle = ref.lower()
    hits = [f for f in files if needle == (f.get("filename") or "").lower()]
    if not hits:
        hits = [f for f in files if needle in (f.get("filename") or "").lower()]
    if not hits:
        names = ", ".join(sorted({(f.get("filename") or "?") for f in files})[:15])
        raise RuntimeError(
            f"Keine Datei mit dem Namen '{ref}' gefunden. "
            f"Verfuegbar waeren z. B.: {names}"
        )
    hits.sort(key=lambda f: str(f.get("createdAt") or ""), reverse=True)
    return hits[0]["file_id"]


def _download(file_id: str) -> tuple[bytes, str]:
    """Datei ueber die oeffentliche API holen. Eigentuemercheck macht LibreChat."""
    uid = _user_id()
    fid = _resolve_file_id(file_id)
    url = f"{LIBRECHAT_URL}/api/files/download/{uid}/{fid}"
    r = _HTTP.get(url, headers=_auth_headers(), timeout=HTTP_TIMEOUT, stream=True)
    if r.status_code == 403:
        raise RuntimeError(
            "Zugriff auf die Datei verweigert. Sie gehoert einem anderen Nutzer "
            "oder der Nutzer ist gesperrt."
        )
    r.raise_for_status()
    buf = bytearray()
    for chunk in r.iter_content(65536):
        buf.extend(chunk)
        if len(buf) > MAX_BYTES:
            raise RuntimeError(f"Datei groesser als {MAX_BYTES // (1024 * 1024)} MB")
    cd = r.headers.get("Content-Disposition", "")
    name = "datei"
    if "filename=" in cd:
        name = cd.split("filename=")[-1].strip('"; ').split("''")[-1]
    return bytes(buf), name


def _upload(content: bytes, filename: str, mimetype: str) -> dict:
    """Datei ueber die oeffentliche Upload-API anlegen; Record entsteht in LibreChat.

    Pflichtfelder im Multipart-Body: file_id (UUID), endpoint, message_file.
    """
    url = f"{LIBRECHAT_URL}/api/files/"
    files = {"file": (filename, io.BytesIO(content), mimetype)}
    data = {
        "file_id": str(uuid.uuid4()),
        "endpoint": UPLOAD_ENDPOINT,
        "message_file": "true",
    }
    r = _HTTP.post(url, headers=_auth_headers(), files=files, data=data, timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"Upload fehlgeschlagen ({r.status_code}): {r.text[:200]}")
    return r.json()


def _result_file(content: bytes, filename: str, mimetype: str, extra: dict | None = None) -> str:
    """Datei an LibreChat uebergeben und dem Modell den Download-Link nennen."""
    res = _upload(content, filename, mimetype)
    fid = res.get("file_id") or res.get("_id")
    if not fid:
        raise RuntimeError("Upload lieferte keine file_id: " + json.dumps(res)[:200])
    payload = {
        "filename": filename,
        "bytes": len(content),
        "file_id": fid,
        # LibreChat erkennt im Markdown nur Links mit DREI Segmenten nach dem
        # Muster files|outputs/<userId>/<fileId>/<filename>:
        #   parts.pop() -> filename, parts.pop() -> fileId
        # Fehlt der Dateiname, wird die User-Id als Datei-Id gelesen -> 404
        # ("Datei wurde moeglicherweise geloescht").
        "download_url": f"files/{_user_id()}/{fid}/{filename}",
        "markdown": f"[{filename}](files/{_user_id()}/{fid}/{filename})",
        "hinweis": (
            "Dem Nutzer den Markdown-Link geben, den Inhalt nicht erfinden. "
            "Der Link wird im Chat automatisch zu einem Download."
        ),
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------
# Diagnose
# --------------------------------------------------------------------------


@mcp.tool()
def echo_user() -> str:
    """Gibt die von LibreChat uebergebene User-ID zurueck (Diagnose)."""
    h = current_headers.get()
    return json.dumps(
        {"user_id": h.get("x-user-id"), "email": h.get("x-user-email")}, ensure_ascii=False
    )


@mcp.tool()
def file_info(file_id: str) -> str:
    """Zeigt Groesse und Anfang einer Datei (Diagnose). file_id = UUID oder Dateiname."""
    content, name = _download(file_id)
    return json.dumps(
        {
            "filename": name,
            "bytes": len(content),
            "head": content[:300].decode("utf-8", errors="replace"),
        },
        ensure_ascii=False,
    )


@mcp.tool()
def file_find(filename: str = "", limit: int = 10) -> str:
    """Findet die file_id zu einem Dateinamen (oder listet die letzten Dateien).

    Nutze das, wenn du den Dateinamen kennst (z.B. aus dem Chat-Anhang),
    aber die file_id fuer ein anderes Werkzeug brauchst.
    Bei mehreren Treffern wird die neueste Datei zuerst genannt.
    """
    url = f"{LIBRECHAT_URL}/api/files/"
    r = _HTTP.get(url, headers=_auth_headers(), timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"Dateiliste nicht abrufbar ({r.status_code})")
    files = r.json() or []

    needle = (filename or "").strip().lower()
    hits = [f for f in files if needle in (f.get("filename") or "").lower()] if needle else list(files)

    hits.sort(key=lambda f: str(f.get("createdAt") or ""), reverse=True)
    out = [
        {
            "file_id": f.get("file_id"),
            "filename": f.get("filename"),
            "type": f.get("type"),
            "bytes": f.get("bytes"),
            "created": f.get("createdAt"),
        }
        for f in hits[: max(1, min(limit, 25))]
    ]
    payload = {"treffer": len(hits), "dateien": out}
    if needle and not hits:
        payload["hinweis"] = (
            "Kein Treffer. Mit leerem filename alle Dateien auflisten und "
            "den Namen vergleichen."
        )
    return json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------
# DOCX
# --------------------------------------------------------------------------


@mcp.tool()
def docx_read(file_id: str, include_text: bool = True) -> str:
    """Liest ein Word-Dokument: Absaetze, Tabellen und vorhandene Revisionen.

    file_id = UUID oder Dateiname aus dem Chat."""
    content, _ = _download(file_id)
    info = docx_tools._read_docx(content)
    payload = {
        "paragraphs": info["paragraphs"],
        "chars": info["chars"],
        "tables": info["tables"],
        "tracked_changes": info["tracked_changes"],
    }
    if include_text:
        payload["text"] = info["text"][:20000]
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool()
def docx_edit_tracked(file_id: str, replacements: list, author: str = "") -> str:
    """Aendert ein Word-Dokument im AENDERUNGSMODUS (Track Changes).

    file_id = UUID oder Dateiname aus dem Chat.

    replacements: Liste von {"old": "<exakter Text>", "new": "<Ersatz>"}.
    Ergebnis ist eine neue Datei mit echten Word-Revisionen (annehmbar/ablehnbar).
    Wichtig: "old" muss exakt im Dokument stehen, inklusive Leerzeichen und Umlauten.

    author: Name, der an jeder Revision steht. Ohne Angabe greift die
    Umgebungsvariable TOOLBOX_REVISION_AUTHOR, sonst "LibreChat".
    """
    author = author or os.environ.get("TOOLBOX_REVISION_AUTHOR", "") or "LibreChat"
    content, name = _download(file_id)
    new_bytes, report = docx_tools._edit_tracked(content, replacements, author)
    applied = sum(r["count"] for r in report)
    out = docx_tools._result_filename(name)
    extra = {"ersetzungen": report, "angewendet": applied}
    if applied == 0:
        extra["warnung"] = (
            "Keine Ersetzung gefunden. Text exakt pruefen "
            "(Leerzeichen, Bindestriche, Umlaute, Zeilenumbrueche)."
        )
    return _result_file(new_bytes, out, DOCX_MIME, extra)


@mcp.tool()
def docx_list_revisions(file_id: str) -> str:
    """Listet alle Revisionsmarkierungen auf. file_id = UUID oder Dateiname."""
    content, _ = _download(file_id)
    return json.dumps(docx_tools._list_revisions(content), ensure_ascii=False)


@mcp.tool()
def docx_create(markdown: str, filename: str = "dokument.docx") -> str:
    """Erstellt ein neues Word-Dokument aus Markdown."""
    data = docx_tools._create_docx(markdown, None)
    return _result_file(data, docx_tools._result_filename(filename), DOCX_MIME)


@mcp.tool()
def docx_to_pdf(file_id: str, accept_revisions: bool = True) -> str:
    """Konvertiert ein Word-Dokument in ein PDF. file_id = UUID oder Dateiname.

    accept_revisions=True nimmt vorher alle Aenderungen an (sauberes PDF).
    False laesst die Revisionsmarken sichtbar.
    """
    content, name = _download(file_id)
    pdf = docx_tools._docx_to_pdf(content, accept_revisions)
    out = docx_tools._result_filename(name).rsplit(".", 1)[0] + ".pdf"
    return _result_file(pdf, out, "application/pdf")


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------


@mcp.tool()
def pdf_read(file_id: str, pages: str | None = None) -> str:
    """Liest Text aus einem PDF. file_id = UUID oder Dateiname.

    pages z.B. "1", "1-3", "1,3,5".

    Meldet pro Seite Zeichenzahl, Bildanzahl und einen Hinweis, wenn die Seite
    vermutlich ein Scan ist (dann pdf_render nutzen).
    """
    content, _ = _download(file_id)
    return json.dumps(pdf_tools._read_pdf(content, pages), ensure_ascii=False)


@mcp.tool()
async def pdf_render(file_id: str, pages: str | None = None, dpi: int = 120):
    """Rendert PDF-Seiten als BILD. Du siehst die Seite dann wirklich.

    file_id = UUID oder Dateiname aus dem Chat. Maximal 4 Seiten pro Aufruf.

    Fuer Aufgaben, die das Aussehen betreffen: Layout, Farben, Grafiken,
    Design-Bewertung, Tabellen in Scans. Maximal 4 Seiten pro Aufruf.
    """
    content, _ = _download(file_id)
    rendered = pdf_tools._render_pages(content, pages, dpi)
    out = [Image(data=png, format="png") for _num, png in rendered]
    labels = ", ".join(str(num) for num, _ in rendered)
    out.insert(0, f"Gerenderte Seiten: {labels} (dpi {dpi}).")
    return out


@mcp.tool()
def md_to_pdf(markdown: str, filename: str = "dokument.pdf", title: str | None = None) -> str:
    """Erstellt ein PDF aus Markdown (Ueberschriften, Listen, Tabellen, Fett)."""
    pdf = pdf_tools._md_to_pdf(markdown, title)
    name = pdf_tools_result_name(filename)
    return _result_file(pdf, name, "application/pdf")


def pdf_tools_result_name(name: str) -> str:
    base = os.path.basename(name or "dokument.pdf")
    if not base.lower().endswith(".pdf"):
        base = os.path.splitext(base)[0] + ".pdf"
    return base


# --------------------------------------------------------------------------
# XLSX / CSV
# --------------------------------------------------------------------------


@mcp.tool()
def xlsx_read(file_id: str, sheet: str | None = None, max_rows: int = 500) -> str:
    """Liest eine Excel-Tabelle oder CSV als Zeilen-Array. file_id = UUID oder Dateiname.

    sheet: Blattname (optional, sonst das erste).
    Die erste Zeile ist typischerweise die Kopfzeile.
    """
    content, name = _download(file_id)
    limit = max(1, min(max_rows, 2000))
    if name.lower().endswith(".csv") or file_id.lower().endswith(".csv"):
        info = xlsx_tools._read_csv(content)
    else:
        info = xlsx_tools._read_xlsx(content, sheet, limit)
    return json.dumps(info, ensure_ascii=False)


@mcp.tool()
def xlsx_write(rows: list, filename: str = "tabelle.xlsx", sheet: str = "Tabelle", header_bold: bool = True) -> str:
    """Erstellt eine neue Excel-Tabelle aus einem Zeilen-Array.

    rows: [["Kopf1","Kopf2"],["Wert1","Wert2"], ...]
    """
    data = xlsx_tools._write_xlsx(rows, sheet, header_bold)
    return _result_file(data, xlsx_tools._result_name(filename), XLSX_MIME)


@mcp.tool()
def xlsx_update(file_id: str, updates: list, sheet: str | None = None) -> str:
    """Aendert einzelne Zellen und erhaelt Formatierung sowie Formeln.

    file_id = UUID oder Dateiname.

    updates: [{"cell": "B2", "value": "neuer Wert"}]
    Zellreferenzen in A1-Notation. Zahlen werden als Zahl geschrieben.
    """
    content, name = _download(file_id)
    new_bytes, report = xlsx_tools._update_xlsx(content, updates, sheet)
    changed = sum(1 for r in report if r.get("status") == "geaendert")
    extra = {"aenderungen": report, "geaendert": changed}
    if changed == 0:
        extra["warnung"] = "Keine Zelle geaendert. Zellreferenzen und Blattname pruefen."
    return _result_file(new_bytes, xlsx_tools._result_name(name), XLSX_MIME, extra)


# --------------------------------------------------------------------------
# Textstatistik
# --------------------------------------------------------------------------


@mcp.tool()
def text_stats(text: str, kind: str = "frei") -> str:
    """Zaehlt Zeichen, Woerter, Saetze deterministisch – statt im Kopf zu schaetzen.

    Fuer JEDE Laengenangabe an den Nutzer zuerst dieses Werkzeug nutzen.
    Niemals Zeichen selbst zaehlen.

    kind: frei | ueberschrift | teaser | instagram | bluesky | linkedin
      - ueberschrift: prueft max. 45 Zeichen
      - teaser:       prueft max. 250 Zeichen
      - instagram:    prueft max. 2.200 Zeichen
      - bluesky:      prueft max. 300 GRAPHEME (nicht Zeichen)
      - linkedin:     prueft max. 3.000 Zeichen

    Liefert ausserdem Silben, Lesezeit und eine Lesbarkeitsstufe.
    Bei kind != "frei" steht unter laengenpruefung, wie viel Platz noch bleibt.
    """
    return json.dumps(textstats_tools.analyze(text, kind), ensure_ascii=False)


@mcp.tool()
def file_text_stats(file_id: str, kind: str = "frei") -> str:
    """Zaehlt Zeichen und Woerter in einer Textdatei oder einem PDF.

    file_id: UUID oder Dateiname. Fuer PDFs wird der Textlayer gelesen.
    """
    content, name = _download(file_id)
    if name.lower().endswith(".pdf") or content[:4] == b"%PDF":
        info = pdf_tools._read_pdf(content, None)
        text = "\n".join(p.get("text", "") for p in info["pages"])
    else:
        text = content.decode("utf-8", errors="replace")
    result = textstats_tools.analyze(text, kind)
    result["quelle"] = name
    return json.dumps(result, ensure_ascii=False)


# --------------------------------------------------------------------------
# Header-Bruecke
# --------------------------------------------------------------------------


class HeaderBridge:
    """ASGI-Middleware: reicht die HTTP-Header pro Request in die contextvar."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            hdrs = {}
            for k, v in scope.get("headers") or []:
                try:
                    hdrs[k.decode("latin-1").lower()] = v.decode("latin-1")
                except Exception:
                    continue
            token = current_headers.set(hdrs)
            try:
                await self.app(scope, receive, send)
            finally:
                current_headers.reset(token)
        else:
            await self.app(scope, receive, send)


def build_app():
    app = mcp.streamable_http_app()

    from starlette.routing import Route
    from starlette.responses import PlainTextResponse

    async def health(request):
        return PlainTextResponse("ok")

    app.router.routes.insert(0, Route("/health", health))
    return HeaderBridge(app)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    log.info("Toolbox startet auf :%s (LibreChat=%s)", port, LIBRECHAT_URL)
    uvicorn.run(build_app(), host="0.0.0.0", port=port, log_level="warning")
