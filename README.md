# librechat-document-mcp

**Eine Redaktions-Toolbox für LibreChat.** MCP-Server, der dem Chat echte
Dateiverarbeitung gibt: Word im Änderungsmodus, Excel bearbeiten, PDF-Seiten
optisch auswerten, Zeichen zuverlässig zählen.

> LibreChat kann Dateien von Haus aus nur als Text auslesen oder an den
> Modellanbieter schicken. Wer damit redaktionell arbeitet – Pressemitteilungen
> überarbeiten, Tabellen pflegen, Layout prüfen – stößt schnell an eine Wand.
> Dieses Projekt schließt die Lücke mit **einem** Container.

---

## Das Problem

Ein Redaktionsteam arbeitet mit Dokumenten, die ein Chat-Modell nicht anfassen kann:

| Aufgabe | LibreChat allein | Mit dieser Toolbox |
|---|---|---|
| Word-Dokument lesen | nur Text, keine Struktur | Absätze, Tabellen, vorhandene Revisionen |
| **Word überarbeiten mit Änderungsmarkierungen** | nicht möglich | echte `<w:ins>`/`<w:del>`-Revisionen mit Autor und Datum |
| Excel-Zelle ändern | nicht möglich | Zellwert ändern, Formeln bleiben erhalten |
| **PDF optisch bewerten** (Farben, Layout, Grafiken) | nicht möglich | Seite wird als Bild an ein Vision-Modell gegeben |
| Markdown zu PDF | nicht möglich | pandoc + LibreOffice, kein LaTeX nötig |
| Zeichen zählen | Modell schätzt und verzählt sich | deterministisch gezählt |
| Datei zurückgeben | nicht möglich | Download-Link direkt im Chat |

---

## Warum kein Code-Interpreter

Der offizielle Weg wäre
[`LibreChat-AI/code-interpreter`](https://github.com/LibreChat-AI/code-interpreter).
Für viele Installationen scheidet er aus:

- **Sieben Container** plus Redis und MinIO
- Braucht `/dev/kvm` für die MicroVM-Isolation. Fehlt es – etwa weil der Server
  selbst eine VM ohne verschachtelte Virtualisierung ist – bleibt nur der
  NsJail-Modus mit geteiltem Host-Kernel, den der Hersteller ausdrücklich **nicht**
  für fremden Code empfiehlt.
- 8–15 GB zusätzlicher Plattenbedarf

Diese Toolbox löst dasselbe Problem mit **einem** Container und **fest definierten
Werkzeugen** statt beliebigem Code. Das ist weniger flexibel – und deutlich
einfacher abzusichern.

---

## Werkzeuge

### PDF und Vision

| Werkzeug | Zweck |
|---|---|
| `pdf_read` | Text pro Seite; warnt bei Seiten ohne Textlayer (Scans) |
| `pdf_render` | Seiten als **Bild** an das Modell – für Farben, Layout, Grafiken |
| `md_to_pdf` | Markdown zu PDF |

### Word

| Werkzeug | Zweck |
|---|---|
| `docx_read` | Absätze, Tabellen, vorhandene Revisionen |
| `docx_edit_tracked` | **Änderungsmodus**: erzeugt echte Word-Revisionen |
| `docx_list_revisions` | Revisionen auflisten (Typ, Autor, Datum, Text) |
| `docx_create` | Neues Dokument aus Markdown |
| `docx_to_pdf` | PDF, wahlweise mit angenommenen oder sichtbaren Revisionen |

### Excel

| Werkzeug | Zweck |
|---|---|
| `xlsx_read` | Blatt als Zeilen-Array, auch CSV |
| `xlsx_write` | Neue Arbeitsmappe |
| `xlsx_update` | Einzelne Zellen per A1-Referenz, **Formeln bleiben erhalten** |

### Textstatistik

| Werkzeug | Zweck |
|---|---|
| `text_stats` | Zeichen, Wörter, Sätze, Silben, Lesezeit – deterministisch |
| `file_text_stats` | dasselbe für Textdatei oder PDF |

Modelle zählen Zeichen unzuverlässig. Dieses Werkzeug rechnet. Der Parameter
`kind` prüft zusätzlich Längengrenzen: `ueberschrift`, `teaser`, `instagram`,
`bluesky` (Grapheme), `linkedin`. Die Voreinstellungen sind redaktionelle
Hausnummern und über Umgebungsvariablen änderbar – siehe
[`docs/INSTALL.md`](docs/INSTALL.md#längengrenzen).

### Diagnose

`echo_user`, `file_info`, `file_find`

---

## Quickstart

### 1. Repository holen

```bash
git clone https://github.com/steff-sson/librechat-document-mcp.git
```

### 2. In die LibreChat-Compose einbinden

Neben dem `LibreChat/`-Verzeichnis ablegen und ergänzen:

```yaml
networks:
  net_tools:
    internal: true        # kein Internet, nur api ↔ toolbox

services:
  api:
    networks:
      - net_www
      - net_ai
      - net_tools

  toolbox:
    container_name: toolbox
    build:
      context: ./librechat-document-mcp
    restart: unless-stopped
    networks:
      - net_tools
    environment:
      - LIBRECHAT_URL=http://api:3080
      - JWT_SECRET=${JWT_SECRET}
      # optional:
      # - TOOLBOX_NAME=Redaktions-Toolbox
      # - TOOLBOX_REVISION_AUTHOR=Redaktion
    read_only: true
    tmpfs:
      - /tmp:size=1g,mode=1777
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    mem_limit: 2g
    depends_on:
      - api
```

### 3. In `librechat.yaml` eintragen

```yaml
mcpSettings:
  allowedDomains:
    - toolbox          # Pflicht! allowedDomains ist eine strikte Whitelist

mcpServers:
  toolbox:
    type: streamable-http
    url: "http://toolbox:8080/mcp"
    timeout: 120000
    headers:
      X-User-Id: "{{LIBRECHAT_USER_ID}}"
      X-User-Email: "{{LIBRECHAT_USER_EMAIL}}"
```

> **Achtung:** Wenn `allowedDomains` gesetzt ist, sind **nur** die gelisteten
> Ziele erreichbar. Fehlt `toolbox`, verbindet der Server nicht.

### 4. Starten

```bash
docker compose build toolbox
docker compose up -d toolbox
docker compose restart api
```

### 5. Im Agent freischalten

Agent bearbeiten → **Tools** → **Hinzufügen** → **Tool Library** → Filter
*MCP servers* → `toolbox` wählen.

> **Wichtig:** Die Tool-Liste wird im Agent **gespeichert**. Nach dem Hinzufügen
> einen **neuen Chat** beginnen – in bestehenden Chats bleibt der alte Stand.

---

## Wie Dateien durchgereicht werden

Die Toolbox hat **keinen Dateisystem-Zugriff** und **keine Datenbankverbindung**.
Ein- und Ausgabe laufen ausschließlich über die öffentliche Datei-API von
LibreChat:

```
LibreChat ──MCP──▶ Toolbox ──GET  /api/files/download/<user>/<file_id>──▶ LibreChat
                            ──POST /api/files/ ────────────────────────▶ LibreChat
                                                             │
                                              Nutzer bekommt Download-Link
```

Die Toolbox signiert dafür pro Aufruf ein kurzlebiges Token (120 s) mit
`JWT_SECRET`.

**Warum `JWT_SECRET`?** Die Datei-API verlangt ein gültiges Token. Alternative
wäre ein Datenbankzugriff – also deutlich mehr Rechte für deutlich mehr Risiko.
Die Toolbox bekommt das Secret als Umgebungsvariable, sieht aber nicht die
`.env` mit den übrigen Zugangsdaten. Was das bedeutet und wie man es begrenzt,
steht in [`docs/SECURITY.md`](docs/SECURITY.md).

### Dateiname statt ID

Der Agent kennt aus dem Chat nur den Dateinamen. Alle Werkzeuge akzeptieren
deshalb **beides**:

```
pdf_render(file_id="Programm.pdf", pages="1-2")       ← Name genügt
pdf_render(file_id="a1b2c3d4-...", pages="1-2")       ← UUID geht auch
```

Ist es keine UUID, holt die Toolbox die Dateiliste und sucht den Namen (exakt,
dann als Teilstring, neueste zuerst). Bei Tippfehlern nennt die Fehlermeldung
verfügbare Dateinamen.

### Download-Links

Erzeugte Dateien kommen als Markdown-Link zurück, mit **drei** Pfadsegmenten:

```
files/<userId>/<fileId>/<dateiname>
```

Nur dieses Muster erkennt der LibreChat-Client und wandelt es in einen
authentifizierten Download um. Die zwei naheliegenden Fehlversuche sind in
[`docs/INSTALL.md`](docs/INSTALL.md#download-links) dokumentiert.

---

## Sicherheit

| Maßnahme | Umsetzung |
|---|---|
| Netz | eigenes Netz, `internal: true` – kein Internet |
| Dateisystem | keine Volumes, `read_only`, nur `/tmp` als tmpfs |
| Datenbank | keine Zugangsdaten im Container |
| Codeausführung | feste Werkzeuge, kein `eval` |
| Prozess | `cap_drop: ALL`, `no-new-privileges`, UID 10001, 2 GB Limit |
| Konvertierungen | harter 60-Sekunden-Timeout mit Prozess-Kill |
| Zip-Bomben | entpackte Größe begrenzt, Pfad-Traversal abgewiesen |

**Restrisiko:** Die Toolbox kennt `JWT_SECRET` und könnte theoretisch Tokens für
beliebige Nutzer erzeugen. Vertretbar, weil kein Netz, kein Dateisystem, keine
Datenbank und nur feste Werkzeuge erreichbar sind.

> **Nur `api` und `toolbox` dürfen im Netz `net_tools` hängen.** Jeder weitere
> Container dort könnte den `X-User-Id`-Header fälschen.

---

## Grenzen

- 4 PDF-Seiten pro `pdf_render`-Aufruf (jede Seite ≈ 1.300–1.800 Tokens)
- 20 MB pro Datei, 200 Ersetzungen pro `docx_edit_tracked`
- 200 MB entpackt bei Office-Dateien

---

## Antworten auf typische Fragen

**Warum braucht es `JWT_SECRET`?**
Weil die Toolbox Dateien über LibreChats API lesen und schreiben muss, und die
verlangt ein gültiges Token. Siehe [Sicherheit](#sicherheit).

**Kann ein Modell damit beliebigen Code ausführen?**
Nein. Es gibt keine `run_python`-Schnittstelle, nur feste Werkzeuge mit
definierten Parametern.

**Funktioniert es mit jedem Modell?**
Die Textwerkzeuge ja. `pdf_render` braucht ein Modell mit Vision-Fähigkeit. Ein
Modell muss PDFs **nicht** nativ verstehen – genau dafür gibt es das Rendern.

**Warum nicht einfach OCR?**
OCR liefert Text mit Struktur, aber keine Farben, kein Layout, keine Grafiken.
Für „bewerte die Gestaltung" reicht Text nicht. Beides hat seinen Platz.

**Läuft das nur mit LibreChat?**
Die Werkzeuge sind MCP-Tools; die Datei-Brücke nutzt LibreChats API. Für andere
MCP-Clients wäre nur der Ein-/Ausgabeteil anzupassen.

---

## Dokumentation

- [`docs/INSTALL.md`](docs/INSTALL.md) – Schritt für Schritt, Stolperfallen, Konfiguration
- [`docs/SECURITY.md`](docs/SECURITY.md) – Bedrohungsmodell und Restrisiko

---

## Lizenz

MIT – siehe [LICENSE](LICENSE).

Copyright (c) 2026 Stefan Weitzel
