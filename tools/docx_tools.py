"""DOCX-Werkzeuge: lesen, im Aenderungsmodus bearbeiten, neu erstellen, als PDF.

Track Changes laeuft ueber docx-revisions (echtes OOXML <w:ins>/<w:del>).
Konvertierungen laufen ueber LibreOffice/pandoc mit hartem Timeout.
"""

import io
import os
import json
import shutil
import tempfile
import subprocess
import logging
import zipfile

log = logging.getLogger("toolbox.docx")

CONVERT_TIMEOUT = int(os.environ.get("TOOLBOX_CONVERT_TIMEOUT", "60"))
MAX_UNCOMPRESSED = int(os.environ.get("TOOLBOX_MAX_UNCOMPRESSED", str(200 * 1024 * 1024)))
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class ToolError(Exception):
    """Fehler, den das Modell lesen und korrigieren kann."""


def _check_zip_safety(data: bytes, label: str = "Datei") -> None:
    """DOCX/XLSX sind ZIP-Container. Schutz gegen Zip-Bomben und Pfad-Traversal."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            total = 0
            for info in z.infolist():
                name = info.filename
                if name.startswith("/") or ".." in name.split("/"):
                    raise ToolError(f"{label}: unsicherer Pfad im Archiv ({name})")
                total += info.file_size
                if total > MAX_UNCOMPRESSED:
                    raise ToolError(
                        f"{label}: entpackt groesser als {MAX_UNCOMPRESSED // (1024 * 1024)} MB"
                    )
    except zipfile.BadZipFile:
        raise ToolError(f"{label} ist kein gueltiges Office-Dokument (ZIP-Container).")


def _run(cmd: list, workdir: str, label: str) -> None:
    """Externes Werkzeug mit hartem Timeout; killt die Prozessgruppe."""
    try:
        p = subprocess.run(
            cmd,
            cwd=workdir,
            capture_output=True,
            timeout=CONVERT_TIMEOUT,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"{label}: Zeitlimit von {CONVERT_TIMEOUT}s ueberschritten")
    if p.returncode != 0:
        err = (p.stderr or b"").decode("utf-8", "replace")[:300]
        raise ToolError(f"{label} fehlgeschlagen: {err or 'unbekannter Fehler'}")


def _soffice(workdir: str, target: str, label: str) -> None:
    """LibreOffice-Konvertierung im isolierten Benutzerprofil."""
    profile = os.path.join(workdir, "lo-profile")
    _run(
        [
            "soffice",
            "--headless",
            "--norestore",
            f"-env:UserInstallation=file://{profile}",
            "--convert-to",
            target,
            "--outdir",
            workdir,
            *[f for f in os.listdir(workdir) if f.endswith((".docx", ".doc", ".odt"))],
        ],
        workdir,
        label,
    )


def _read_docx(data: bytes) -> dict:
    """Liest Absaetze und vorhandene Revisionen."""
    from docx import Document
    from docx_revisions import RevisionDocument

    _check_zip_safety(data, "DOCX")
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as fh:
        fh.write(data)
        path = fh.name
    try:
        doc = Document(path)
        paragraphs = [p.text for p in doc.paragraphs]
        tracked = []
        try:
            rdoc = RevisionDocument(path)
            for ch in rdoc.track_changes:
                tracked.append(
                    {
                        "type": type(ch).__name__,
                        "author": ch.author,
                        "date": str(ch.date) if ch.date else None,
                        "text": ch.text[:200],
                    }
                )
        except Exception as exc:
            log.warning("Revisions konnten nicht gelesen werden: %s", exc)
        return {
            "paragraphs": len(paragraphs),
            "chars": sum(len(p) for p in paragraphs),
            "text": "\n".join(paragraphs),
            "tracked_changes": tracked,
            "tables": len(doc.tables),
        }
    finally:
        os.unlink(path)


def _edit_tracked(data: bytes, replacements: list, author: str) -> tuple[bytes, list]:
    """Wendet Textersetzungen als nachverfolgte Aenderungen an."""
    from docx_revisions import RevisionDocument

    _check_zip_safety(data, "DOCX")
    if not replacements:
        raise ToolError("Keine Ersetzungen uebergeben.")
    if len(replacements) > 200:
        raise ToolError("Mehr als 200 Ersetzungen pro Aufruf sind nicht erlaubt.")

    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as fh:
        fh.write(data)
        path = fh.name
    out_path = path.replace(".docx", "-out.docx")
    try:
        rdoc = RevisionDocument(path)
        report = []
        for item in replacements:
            old = (item.get("old") or "").strip()
            new = item.get("new") or ""
            if not old:
                report.append({"old": old, "new": new, "count": 0, "status": "leer"})
                continue
            count = rdoc.find_and_replace_tracked(old, new, author=author)
            report.append(
                {
                    "old": old,
                    "new": new,
                    "count": count,
                    "status": "ersetzt" if count else "nicht gefunden",
                }
            )
        rdoc.save(out_path)
        with open(out_path, "rb") as fh:
            result = fh.read()
        return result, report
    finally:
        for p in (path, out_path):
            try:
                os.unlink(p)
            except OSError:
                pass


def _create_docx(markdown: str, reference: bytes | None) -> bytes:
    """Erstellt ein DOCX aus Markdown via pandoc."""
    workdir = tempfile.mkdtemp(prefix="docx-create-")
    try:
        md_path = os.path.join(workdir, "input.md")
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(markdown)

        cmd = ["pandoc", "input.md", "-o", "output.docx", "--from", "markdown"]
        if reference is not None:
            _check_zip_safety(reference, "Referenz-DOCX")
            ref_path = os.path.join(workdir, "reference.docx")
            with open(ref_path, "wb") as fh:
                fh.write(reference)
            cmd += ["--reference-doc", "reference.docx"]

        _run(cmd, workdir, "DOCX-Erstellung")
        out = os.path.join(workdir, "output.docx")
        if not os.path.exists(out):
            raise ToolError("DOCX wurde nicht erzeugt.")
        with open(out, "rb") as fh:
            return fh.read()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _docx_to_pdf(data: bytes, accept_revisions: bool) -> bytes:
    """DOCX -> PDF. Optional vorher alle Revisionen annehmen."""
    _check_zip_safety(data, "DOCX")
    workdir = tempfile.mkdtemp(prefix="docx-pdf-")
    try:
        src = os.path.join(workdir, "input.docx")
        with open(src, "wb") as fh:
            fh.write(data)

        if accept_revisions:
            from docx_revisions import RevisionDocument

            rdoc = RevisionDocument(src)
            rdoc.accept_all()
            rdoc.save(src)

        _soffice(workdir, "pdf", "PDF-Konvertierung")
        out = os.path.join(workdir, "input.pdf")
        if not os.path.exists(out):
            raise ToolError("PDF wurde nicht erzeugt.")
        with open(out, "rb") as fh:
            return fh.read()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _list_revisions(data: bytes) -> dict:
    """Listet Revisionen mit Autor, Datum und Typ."""
    from docx_revisions import RevisionDocument

    _check_zip_safety(data, "DOCX")
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as fh:
        fh.write(data)
        path = fh.name
    try:
        rdoc = RevisionDocument(path)
        items = []
        for ch in rdoc.track_changes:
            items.append(
                {
                    "type": "Einfuegung" if type(ch).__name__ == "TrackedInsertion" else "Loeschung",
                    "author": ch.author or "",
                    "date": str(ch.date) if ch.date else "",
                    "text": ch.text,
                }
            )
        return {"count": len(items), "revisions": items}
    finally:
        os.unlink(path)


def _result_filename(name: str, default_ext: str = ".docx") -> str:
    """Sauberer Dateiname ohne Pfadanteile, mit passender Endung."""
    base = os.path.basename(name or "dokument")
    if not base.lower().endswith(default_ext):
        stem, ext = os.path.splitext(base)
        base = (stem or base) + default_ext
    return base
