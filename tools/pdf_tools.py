"""PDF-Werkzeuge: Text lesen, Seiten als Bild rendern (Vision), Markdown -> PDF.

Rendern laeuft ueber PyMuPDF (pymupdf/fitz) im Toolbox-Container.
Markdown -> PDF laeuft ueber pandoc mit LaTeX-freier Engine (via LibreOffice-HTML-Pfad
oder wkhtmltopdf-freie Variante: pandoc -> docx -> PDF).
"""

import io
import os
import shutil
import tempfile
import subprocess
import logging

log = logging.getLogger("toolbox.pdf")

CONVERT_TIMEOUT = int(os.environ.get("TOOLBOX_CONVERT_TIMEOUT", "60"))
MAX_PAGES_PER_CALL = int(os.environ.get("TOOLBOX_MAX_PDF_PAGES", "4"))
DEFAULT_DPI = int(os.environ.get("TOOLBOX_PDF_DPI", "120"))
# Obergrenze fuer gerenderte Bilddaten (Basis fuer Vision-Tokens)
MAX_IMAGE_BYTES = int(os.environ.get("TOOLBOX_MAX_IMAGE_BYTES", str(6 * 1024 * 1024)))


class ToolError(Exception):
    """Fehler, den das Modell lesen und korrigieren kann."""


def _open_pdf(data: bytes):
    """PDF oeffnen und Metadaten liefern."""
    import pymupdf

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ToolError(f"PDF konnte nicht geoeffnet werden: {exc}")
    if doc.needs_pass:
        doc.close()
        raise ToolError("PDF ist passwortgeschuetzt.")
    return doc


def _read_pdf(data: bytes, pages: str | None) -> dict:
    """Liest Text und Qualitaetshinweise pro Seite."""
    doc = _open_pdf(data)
    try:
        total = doc.page_count
        indices = _parse_pages(pages, total)
        out = []
        for i in indices:
            page = doc[i]
            text = page.get_text("text") or ""
            images = len(page.get_images(full=True))
            out.append(
                {
                    "page": i + 1,
                    "chars": len(text.strip()),
                    "images": images,
                    "width": round(page.rect.width, 1),
                    "height": round(page.rect.height, 1),
                    "text": text[:8000],
                    # Heuristik: kaum Text, aber Bilder -> wahrscheinlich Scan
                    "warnung": (
                        "Seite enthaelt kaum Text, aber Bilder (= vermutlich Scan). "
                        "Mit pdf_render ansehen."
                        if len(text.strip()) < 40 and images > 0
                        else None
                    ),
                }
            )
        return {"total_pages": total, "pages": out}
    finally:
        doc.close()


def _parse_pages(spec: str | None, total: int) -> list[int]:
    """'1', '1-3', '1,3,5' -> Liste 0-basierter Indizes (geklemmt)."""
    if not spec or not spec.strip():
        return [0] if total > 0 else []
    spec = spec.strip()
    idx: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                start, end = int(a), int(b)
            except ValueError:
                raise ToolError(f"Ungueltige Seitenangabe: {part}")
            for n in range(start, end + 1):
                if 1 <= n <= total:
                    idx.append(n - 1)
        else:
            try:
                n = int(part)
            except ValueError:
                raise ToolError(f"Ungueltige Seitenangabe: {part}")
            if 1 <= n <= total:
                idx.append(n - 1)
    if not idx:
        raise ToolError(f"Keine gueltige Seite in '{spec}' (Dokument hat {total}).")
    # Duplikate entfernen, Reihenfolge erhalten
    seen = set()
    uniq = [i for i in idx if not (i in seen or seen.add(i))]
    if len(uniq) > MAX_PAGES_PER_CALL:
        raise ToolError(
            f"Maximal {MAX_PAGES_PER_CALL} Seiten pro Aufruf "
            f"({len(uniq)} angefragt). Weitere Aufrufe fuer den Rest."
        )
    return uniq


def _render_pages(data: bytes, pages: str | None, dpi: int) -> list[tuple[int, bytes]]:
    """Rendert Seiten als PNG. Gibt [(seitennummer, png_bytes)] zurueck."""
    doc = _open_pdf(data)
    try:
        total = doc.page_count
        indices = _parse_pages(pages, total)
        dpi = max(50, min(dpi, 300))
        results = []
        for i in indices:
            page = doc[i]
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            png = pix.tobytes("png")
            if len(png) > MAX_IMAGE_BYTES:
                # Aufloesung schrittweise senken statt abzubrechen
                for lower in (100, 80, 60):
                    pix = page.get_pixmap(dpi=min(lower, dpi), alpha=False)
                    png = pix.tobytes("png")
                    if len(png) <= MAX_IMAGE_BYTES:
                        break
                else:
                    raise ToolError(
                        f"Seite {i + 1} ist auch bei niedriger Aufloesung zu gross "
                        f"({len(png) // 1024} KB)."
                    )
            results.append((i + 1, png))
        return results
    finally:
        doc.close()


def _md_to_pdf(markdown: str, title: str | None) -> bytes:
    """Markdown -> DOCX (pandoc) -> PDF (LibreOffice). Kein LaTeX noetig."""
    workdir = tempfile.mkdtemp(prefix="md-pdf-")
    try:
        md_path = os.path.join(workdir, "input.md")
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(markdown)

        cmd = ["pandoc", "input.md", "-o", "input.docx", "--from", "markdown"]
        if title:
            cmd += ["--metadata", f"title={title}"]
        try:
            p = subprocess.run(
                cmd, cwd=workdir, capture_output=True, timeout=CONVERT_TIMEOUT, start_new_session=True
            )
        except subprocess.TimeoutExpired:
            raise ToolError(f"Markdown-Konvertierung: Zeitlimit von {CONVERT_TIMEOUT}s")
        if p.returncode != 0:
            raise ToolError(
                "Markdown -> DOCX fehlgeschlagen: "
                + (p.stderr or b"").decode("utf-8", "replace")[:300]
            )

        profile = os.path.join(workdir, "lo-profile")
        try:
            p = subprocess.run(
                [
                    "soffice",
                    "--headless",
                    "--norestore",
                    f"-env:UserInstallation=file://{profile}",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    workdir,
                    "input.docx",
                ],
                cwd=workdir,
                capture_output=True,
                timeout=CONVERT_TIMEOUT,
                start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            raise ToolError(f"PDF-Konvertierung: Zeitlimit von {CONVERT_TIMEOUT}s")
        if p.returncode != 0:
            raise ToolError(
                "DOCX -> PDF fehlgeschlagen: "
                + (p.stderr or b"").decode("utf-8", "replace")[:300]
            )

        out = os.path.join(workdir, "input.pdf")
        if not os.path.exists(out):
            raise ToolError("PDF wurde nicht erzeugt.")
        with open(out, "rb") as fh:
            return fh.read()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
