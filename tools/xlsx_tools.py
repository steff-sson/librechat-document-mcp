"""XLSX-Werkzeuge: lesen, schreiben, Zellen aendern.

openpyxl erhaelt Formatierungen, Formeln und Spaltenbreiten beim Aendern.
CSV wird ebenfalls gelesen (fuer Datenimport).
"""

import io
import os
import csv
import json
import tempfile
import logging

log = logging.getLogger("toolbox.xlsx")

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_ROWS = int(os.environ.get("TOOLBOX_MAX_XLSX_ROWS", "2000"))
MAX_CELLS_WRITE = int(os.environ.get("TOOLBOX_MAX_XLSX_CELLS", "5000"))


class ToolError(Exception):
    """Fehler, den das Modell lesen und korrigieren kann."""


def _check_zip(data: bytes, label: str = "Datei") -> None:
    import zipfile

    max_bytes = 200 * 1024 * 1024
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            total = 0
            for info in z.infolist():
                if info.filename.startswith("/") or ".." in info.filename.split("/"):
                    raise ToolError(f"{label}: unsicherer Pfad im Archiv")
                total += info.file_size
                if total > max_bytes:
                    raise ToolError(f"{label}: entpackt groesser als 200 MB")
    except zipfile.BadZipFile:
        raise ToolError(f"{label} ist kein gueltiges Office-Dokument.")


def _cell_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        # 1.0 -> "1", sonst 3.14
        return str(int(value)) if value.is_integer() else str(value)
    return str(value)


def _read_xlsx(data: bytes, sheet: str | None, max_rows: int) -> dict:
    """Liest ein Blatt als Tabelle plus Metadaten."""
    from openpyxl import load_workbook

    _check_zip(data, "XLSX")
    wb = load_workbook(io.BytesIO(data), data_only=True)
    try:
        names = wb.sheetnames
        if sheet and sheet not in names:
            raise ToolError(
                f"Blatt '{sheet}' existiert nicht. Verfuegbar: {', '.join(names)}"
            )
        ws = wb[sheet] if sheet else wb[names[0]]
        rows = []
        truncated = False
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= max_rows:
                truncated = True
                break
            # Leerzeilen am Ende nicht mitzaehlen
            if all(c is None or c == "" for c in row):
                continue
            rows.append([_cell_str(c) for c in row])
        return {
            "sheet": ws.title,
            "sheets": names,
            "rows": len(rows),
            "cols": max((len(r) for r in rows), default=0),
            "truncated": truncated,
            "data": rows,
        }
    finally:
        wb.close()


def _read_csv(data: bytes) -> dict:
    """Liest CSV tolerant (Semikolon oder Komma)."""
    text = data.decode("utf-8", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    rows = [r for r in rows if any(c.strip() for c in r)]
    return {
        "sheet": "csv",
        "sheets": ["csv"],
        "rows": len(rows),
        "cols": max((len(r) for r in rows), default=0),
        "truncated": len(rows) > MAX_ROWS,
        "data": rows[:MAX_ROWS],
        "delimiter": dialect.delimiter,
    }


def _write_xlsx(rows: list, sheet: str, header_bold: bool) -> bytes:
    """Erzeugt eine neue Arbeitsmappe aus einer Liste von Zeilen."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    if not rows:
        raise ToolError("Keine Daten uebergeben.")
    if len(rows) * max((len(r) for r in rows), default=0) > MAX_CELLS_WRITE:
        raise ToolError(f"Mehr als {MAX_CELLS_WRITE} Zellen pro Aufruf sind nicht erlaubt.")

    wb = Workbook()
    ws = wb.active
    ws.title = (sheet or "Tabelle")[:31]
    for r in rows:
        ws.append(list(r))
    if header_bold and ws.max_row >= 1:
        for cell in ws[1]:
            cell.font = Font(bold=True)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _update_xlsx(data: bytes, updates: list, sheet: str | None) -> tuple[bytes, list]:
    """Aendert Zellen und erhaelt Formatierung/Formeln anderer Zellen.

    updates: [{"cell": "B2", "value": "..."}]  (A1-Notation)
    """
    from openpyxl import load_workbook
    from openpyxl.utils import column_index_from_string
    import re

    _check_zip(data, "XLSX")
    if not updates:
        raise ToolError("Keine Aenderungen uebergeben.")
    if len(updates) > MAX_CELLS_WRITE:
        raise ToolError(f"Mehr als {MAX_CELLS_WRITE} Aenderungen pro Aufruf.")

    # data_only=False, damit Formeln erhalten bleiben
    wb = load_workbook(io.BytesIO(data), data_only=False)
    try:
        names = wb.sheetnames
        if sheet and sheet not in names:
            raise ToolError(f"Blatt '{sheet}' existiert nicht. Verfuegbar: {', '.join(names)}")
        ws = wb[sheet] if sheet else wb[names[0]]

        report = []
        for u in updates:
            ref = str(u.get("cell") or "").strip().upper()
            if not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}", ref):
                report.append({"cell": ref, "status": "ungueltige Zellreferenz"})
                continue
            value = u.get("value")
            # Zahlen als Zahl schreiben, nicht als String
            if isinstance(value, str) and re.fullmatch(r"-?\d+(\.\d+)?", value.strip()):
                v = float(value) if "." in value else int(value)
            elif value in ("", None):
                v = None
            else:
                v = value
            old = ws[ref].value
            ws[ref] = v
            report.append(
                {
                    "cell": ref,
                    "alt": _cell_str(old),
                    "neu": _cell_str(v),
                    "status": "geaendert",
                }
            )

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue(), report
    finally:
        wb.close()


def _result_name(name: str, default_ext: str = ".xlsx") -> str:
    base = os.path.basename(name or "tabelle")
    if not base.lower().endswith(default_ext):
        base = os.path.splitext(base)[0] + default_ext
    return base
