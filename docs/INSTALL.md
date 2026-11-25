# Installation und Betrieb

Diese Anleitung setzt eine laufende LibreChat-Installation mit Docker Compose
voraus. Die Toolbox ist ein zusätzlicher Container ohne eigenen Port nach außen.

## Voraussetzungen

| | |
|---|---|
| LibreChat | laufend, `librechat.yaml` bearbeitbar |
| Docker Compose | Version mit `include:`-Unterstützung (2.20+) oder eine eigene Compose-Datei |
| Plattenplatz | ca. 1,5 GB für das Image (LibreOffice ist groß) |
| Arbeitsspeicher | ca. 200 MB im Betrieb, 2 GB Limit empfohlen |

Kein `/dev/kvm` nötig – anders als beim offiziellen Code-Interpreter.

## Schritt 1: Image bauen

```bash
cd /pfad/zu/LibreChat
git clone https://github.com/steff-sson/librechat-document-mcp.git

# In die Compose-Datei einbinden (siehe Schritt 2), dann:
docker compose build toolbox
```

Der erste Build dauert einige Minuten: LibreOffice, pandoc und die
Python-Pakete werden installiert.

**Prüfen, ob der Container läuft:**

```bash
docker compose up -d toolbox
docker ps --filter name=toolbox --format '{{.Names}} {{.Status}}'
# erwartet: toolbox Up ... (healthy)
```

Wird `(unhealthy)` angezeigt, in die Logs schauen:

```bash
docker logs toolbox --tail 30
```

## Schritt 2: Compose ergänzen

```yaml
networks:
  net_tools:
    internal: true

services:
  api:
    networks:
      - net_www        # vorhandene Netze beibehalten
      - net_ai
      - net_tools      # NEU

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
    read_only: true
    tmpfs:
      - /tmp:size=1g,mode=1777
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    mem_limit: 2g
    pids_limit: 256
    depends_on:
      - api
```

> **`JWT_SECRET`** muss denselben Wert haben wie in der LibreChat-`.env`. Das
> ist bereits über die Compose-Variableninterpolation gelöst, wenn beide
> Dienste aus derselben Compose-Datei starten.

> **Nur `api` und `toolbox` in `net_tools`.** Wer dort weitere Container
> einhängt, ermöglicht das Fälschen des `X-User-Id`-Headers.

## Schritt 3: LibreChat konfigurieren

In `config/librechat.yaml`:

```yaml
mcpSettings:
  allowedDomains:
    - toolbox

mcpServers:
  toolbox:
    type: streamable-http
    url: "http://toolbox:8080/mcp"
    timeout: 120000
    initTimeout: 30000
    headers:
      X-User-Id: "{{LIBRECHAT_USER_ID}}"
      X-User-Email: "{{LIBRECHAT_USER_EMAIL}}"
```

### Der `allowedDomains`-Stolperstein

Wenn `allowedDomains` **gesetzt** ist, sind **ausschließlich** die gelisteten
Ziele erreichbar. Fehlt `toolbox`, schlägt die Verbindung fehl:

```
Failed to inspect server "toolbox": Domain "http://toolbox:8080" is not allowed
```

Zwei Wege:

- **A)** `toolbox` zur Liste hinzufügen (empfohlen, wenn die Whitelist gewollt ist)
- **B)** `allowedDomains` ganz entfernen und stattdessen `allowedAddresses: ["toolbox:8080"]` nutzen – dann bleibt das öffentliche Internet erreichbar und nur dieser eine interne Dienst wird freigeschaltet

## Schritt 4: LibreChat neu starten

```bash
docker compose restart api
```

**Prüfen, ob der Server erkannt wurde:**

```bash
docker logs LibreChat --since 2m | grep -i toolbox
```

Erwartet werden Zeilen wie:

```
[MCP][toolbox] URL: http://toolbox:8080/mcp
[MCP][toolbox] Server Instructions: configured
[MCP][toolbox] Initialized in: 150ms
```

### `Tools: undefined` ist meist harmlos

Im Startprotokoll steht oft `Tools: undefined`, obwohl die Werkzeuge vorhanden
sind. Das ist ein Anzeigedetail des Loggers. Gegentest mit dem MCP-SDK:

```bash
docker exec LibreChat node -e "
const {Client}=require('@modelcontextprotocol/sdk/client/index.js');
const {StreamableHTTPClientTransport}=require('@modelcontextprotocol/sdk/client/streamableHttp.js');
(async()=>{
  const t=new StreamableHTTPClientTransport(new URL('http://toolbox:8080/mcp'),{requestInit:{headers:{'X-User-Id':'diagnose'}}});
  const c=new Client({name:'p',version:'1'},{capabilities:{}});
  await c.connect(t);
  console.log((await c.listTools()).tools.map(x=>x.name).join(', '));
  process.exit(0);
})();
"
```

## Schritt 5: Agent ausstatten

1. Agent öffnen → **Bearbeiten**
2. Bereich **Tools** → **Hinzufügen**
3. **Tool Library** → Filter *MCP servers* → `toolbox`
4. Werkzeuge auswählen, **Speichern**

### Nach dem Speichern: neuer Chat nötig

Die Tool-Liste wird im Agent **persistiert**. Wird der Agent später in der
Oberfläche gespeichert, schreibt die UI die Liste neu – Werkzeuge, die nur per
Datenbank ergänzt wurden, verschwinden dabei wieder. Nach jeder Änderung:

**Seite neu laden (F5) und einen neuen Chat beginnen.** In bestehenden Chats
bleibt der alte Werkzeugstand aktiv.

## Dateiname statt Datei-ID

Der Agent sieht im Chat nur den Dateinamen. Alle Werkzeuge akzeptieren deshalb
beides:

```
pdf_render(file_id="Programm.pdf", pages="1-2")
pdf_render(file_id="a1b2c3d4-...", pages="1-2")
```

Aufgelöst wird in der Toolbox: Ist der Wert keine UUID, wird die Dateiliste des
Nutzers geholt und der Name gesucht (exakt, dann als Teilstring, neueste
zuerst). Bei Tippfehlern nennt die Fehlermeldung verfügbare Namen.

**Das gehört in den Systemprompt des Agenten**, damit das Modell nicht rät:

```markdown
## Dateien

Ein Dateiname ist keine Datei-ID. Übergib den Namen aus dem Chat direkt an das
Werkzeug — die Toolbox löst ihn auf. Erfinde niemals eine ID und übernimm keine
ID aus einer Fehlermeldung.
```

## Download-Links

Damit ein erzeugter Download im Chat anklickbar ist, braucht das Linkziel
**genau drei Segmente**:

```
files/<userId>/<fileId>/<dateiname>
```

Der Grund steht im LibreChat-Client (`MarkdownComponents.tsx`): Er zerlegt den
Treffer von hinten.

```js
const parts = path.split('/');
const name    = parts.pop();   // Dateiname
const file_id = parts.pop();   // Datei-ID
```

| Link | Ergebnis |
|---|---|
| `/api/files/download/<uid>/<fid>` | ❌ `{"message":"No auth token"}` – ein Browser-Klick schickt keinen Header |
| `files/<uid>/<fid>` | ❌ „Datei wurde möglicherweise gelöscht" – der Parser liest die **User-ID** als Datei-ID |
| `files/<uid>/<fid>/<name>` | ✅ wird zu einem authentifizierten Download |

Die Werkzeuge liefern dieses Format bereits. Der Agent muss den `markdown`-Wert
ausgeben, nicht `download_url` roh.

## Längengrenzen

`text_stats` prüft redaktionelle Vorgaben. Voreinstellungen:

| `kind` | Vorgabe |
|---|---|
| `ueberschrift` | 45 Zeichen |
| `teaser` | 250 Zeichen |
| `instagram` | 2.200 Zeichen |
| `bluesky` | 300 Grapheme |
| `linkedin` | 3.000 Zeichen |

Überschreibbar per Umgebungsvariable am `toolbox`-Container:

```yaml
environment:
  - TOOLBOX_LIMIT_UEBERSCHRIFT=50
  - TOOLBOX_LIMIT_TEASER=300
  - TOOLBOX_LIMIT_INSTAGRAM=2200
  - TOOLBOX_LIMIT_BLUESKY=300
  - TOOLBOX_LIMIT_LINKEDIN=3000
```

## Weitere Umgebungsvariablen

| Variable | Vorgabe | Zweck |
|---|---|---|
| `TOOLBOX_NAME` | `LibreChat Document MCP` | Anzeigename des MCP-Servers |
| `TOOLBOX_REVISION_AUTHOR` | `LibreChat` | Autorname an Word-Revisionen |
| `TOOLBOX_TOKEN_TTL` | `120` | Gültigkeit des internen Tokens (Sekunden) |
| `TOOLBOX_HTTP_TIMEOUT` | `60` | Timeout für Datei-API-Aufrufe |
| `TOOLBOX_CONVERT_TIMEOUT` | `60` | Timeout für pandoc/LibreOffice |
| `TOOLBOX_MAX_PDF_PAGES` | `4` | Seiten pro `pdf_render`-Aufruf |
| `TOOLBOX_PDF_DPI` | `120` | Auflösung beim Rendern |

## Update

Die Toolbox ist kein Bestandteil von LibreChat. Ein `docker compose pull`
betrifft sie **nicht**.

```bash
cd /pfad/zu/LibreChat/librechat-document-mcp
git pull
cd ..
docker compose build toolbox
docker compose up -d toolbox
```

Nach einem LibreChat-Update ist nichts zu tun: Die genutzten Endpunkte
(`GET /api/files/download/...`, `POST /api/files/`) sind öffentliche API und
bleiben erhalten.

> **`JWT_SECRET` rotieren?** Danach `docker compose up -d toolbox` ausführen.
> Sonst schlägt jeder Dateizugriff fehl, ohne dass im LibreChat-Log ein
> offensichtlicher Fehler auftaucht.

## Laufzeit neu starten

`docker restart` reicht – es gibt keinen Zustand außerhalb des Containers:

```bash
docker compose restart toolbox
```

## Deinstallation

```bash
docker compose stop toolbox
docker compose rm -f toolbox
```

Dann den `toolbox`-Block aus der Compose-Datei und `librechat.yaml` entfernen,
`docker compose restart api`. Erzeugte Dateien bleiben als normale Uploads
erhalten und lassen sich wie gewohnt löschen.
