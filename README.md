# navaja

MCP server for personal [CENDOJ](https://www.poderjudicial.es/search/indexAN.jsp)
case-law research.

Ask for case law, get candidates with metadata and the site's own automatic
summaries, and read the full text of the resolutions you pick.

## What navaja is

`navaja-mcp` exposes four tools for an MCP client:

- `buscar_sentencias` — search by free text and the site's advanced filters.
  No captcha is needed.
- `listar_localizaciones` — the site's own location vocabulary, ready to pass
  back into a search. No captcha is needed.
- `ver_texto_completo` — fetch the full text of a resolution.
- `estado_servidor` — runtime configuration snapshot.

Search, metadata and the automatic summaries come straight from the public
results page. Only the full-text step can trigger the site's
`Control Descargas masivas` captcha.

## Search filters

`buscar_sentencias` takes free text plus the filters the site's own advanced
form offers. Each one was verified against the live endpoint, by measurement
rather than by reading the front-end.

| Argument | Site field | Accepted values |
| --- | --- | --- |
| `texto` | `TEXT` | free text; optional when another criterion is present |
| `fecha_desde` / `fecha_hasta` | `FECHARESOLUCIONDESDE` / `FECHARESOLUCIONHASTA` | `YYYY-MM-DD` or `DD/MM/AAAA` |
| `jurisdiccion` | `JURISDICCION` | `CIVIL`, `PENAL`, `CONTENCIOSO`, `SOCIAL`, `MILITAR` |
| `tipo_resolucion` | `TIPORESOLUCION` | `SENTENCIA`, `AUTO` |
| `roj` / `ecli` | `ROJ` / `ECLI` | exact identifier |
| `num_resolucion` / `num_recurso` | `NUMERORESOLUCION` / `NUMERORECURSO` | as printed in the resolution |
| `ponente` | `PONENTE` | magistrate's name |
| `voces` | `VOCES` | subject vocabulary, e.g. `TRÁFICO DE DROGAS` |
| `localizacion` | `VALUESCOMUNIDAD` | see below |
| `coleccion` | `databasematch` | `AN` (every jurisdiction), `TS` (Tribunal Supremo only) |
| `orden` | `sort` | `reciente`, `antiguo` |
| `campos_extra` | anything else | raw form fields, applied last |

Filters combine with AND. The site's own term matching is AND too and has no
relevance ranking, so results come back ordered by resolution date, and recall
within one query depends on how you word it. The site caps a query at 200
records, which is why a broad query returns the same ceiling for every wording.

### Subject matter

The site classifies every resolution, and the classification travels in the
summary as `RESUMEN: <label>`. `materia` exposes it as its own field. It is
`null` when the site did not classify the resolution: both
`DELITO SIN ESPECIFICAR` and `MATERIAS NO ESPECIFICADAS` mean that.

There is **no server-side subject filter**, and that is measured rather than
assumed:

- `MATERIAS` is a field in the site's own form, but the endpoint ignores it.
  Three values, including a valid one, returned the baseline unchanged.
- Free-text operators do nothing useful: `"tráfico de drogas"` in quotes equals
  the unquoted query, `+tráfico +drogas` is worse, and `AND` / `Y` change
  nothing.
- `voces` is honoured but is a different thesaurus. It does not include every
  resolution whose label says `TRÁFICO DE DROGAS`, so it misses relevant ones.

So to answer "the last N about X in Y": put the place in `localizacion`, put a
subject wording in `texto`, ask for a page of 10 to 50, and **classify the
results by `materia`**. A query for `"tráfico de drogas"` also returns
resolutions about drink-driving or extranjería that merely mention the phrase,
and the label is what tells them apart.

### Location

`localizacion` takes a list. Each entry is either the site's own form, with the
level in the suffix, or a bare place name, which means a comunidad autónoma:

```python
buscar_sentencias(texto="tráfico de drogas", localizacion=["MELILLA(C)"])
buscar_sentencias(localizacion=["Barcelona(P)", "Melilla(S)"])
buscar_sentencias(localizacion=["Melilla"])   # the same as "MELILLA(C)"
```

The levels are `(C)` comunidad autónoma, `(P)` provincia and `(S)` sede.
Entries are OR-ed with each other and AND-ed with the other filters. Names are
the site's uppercase vocabulary; the site publishes it at

```
POST /search/jurisprudencia.action
  action=getComunidades&field=COMUNIDAD|PROVINCIA|SEDE&publicinterface=true
```

which returns pipe-separated `KEY&LABEL` pairs (`MELILLA&MELILLA`,
`PAÍS VASCO&PAÍS VASCO`).

`listar_localizaciones` exposes that vocabulary as a tool, already formatted as
tokens you can pass straight back:

```python
listar_localizaciones(nivel="COMUNIDAD")                        # "MELILLA(C)", ...
listar_localizaciones(nivel="PROVINCIA", comunidad="MELILLA")   # "MELILLA(P)"
listar_localizaciones(nivel="SEDE", comunidad="MELILLA", provincia="MELILLA")
```

The tokens always carry their level suffix, so a provincia token cannot be
mistaken for a comunidad. A missing parent raises rather than returning an empty
list.

The value is the label the site displays, not an internal id: a search with
Melilla selected sends `MELILLA(C) | `. The codes the front-end keeps in its own
checkbox values (`ALL@ALL@MELILLA`) are ignored by the server, so a client that
sends those gets unfiltered results and no error.

### Pagination and the 200-record ceiling

`records_por_pagina` is one of 10, 20, 30 or 50, and `pagina` is a real page
number: navaja converts it to the site's record offset, which counts records,
not pages. A page whose window would pass record 200 is refused rather than
requested, because past the ceiling the site silently clamps the offset and
returns the whole result set again — 200 records that look like a normal page.

Getting this wrong is not hypothetical. The previous release forwarded the page
number as the offset, so page 2 repeated nine of page 1's ten results.

`total_capped` reports when `total` sits on that ceiling, so the count is a
ceiling rather than a count. When it is true, union several subject wordings or
narrow with a date range instead of paging: past the ceiling the site clamps the
offset and returns the same records again, so there is nothing further to reach.

### When the site does not answer

A search that matches nothing is an answer: it returns an empty result page.
Three other outcomes are errors, told apart by the response body because the
shapes are the same size as legitimate ones:

- the site rejects the request as invalid → `SearchRequestError`
- the site serves its `Control de grandes paginaciones` challenge →
  `SearchGatedError`
- the site returns more records than the page asked for → `SearchError`

navaja does not solve that challenge, and it never reports a refused search as
an empty result set.

### Not modelled

`ID_NORMA`, `SUBTIPORESOLUCION`, `INSTITUCION`, `SECCION`, `SECCIONAUTO`,
`SECCIONSOLOPLENO`, `TIPOORGANOPUB` and the `TIPOINTERES_*` flags are reachable
through `campos_extra`. `ID_NORMA` needs an id space navaja does not know how to
address: a non-matching id comes back as a legitimate zero-hit page, not as an
error.

## How full-text retrieval works

When you ask for the full text of a resolution, navaja requests the document
from CENDOJ. If the site responds with its captcha page, navaja shows the
captcha image through a **long-lived local HTTP form** bound to a private
network interface and waits for a human to type the answer.

The form URL has three states:

- While a challenge is pending, it serves the captcha form.
- While no challenge is pending, it serves an idle page that says so and
  refreshes itself every 5 seconds; a tab left open picks up the next
  challenge without the human reloading.
- If the URL answers nothing at all, no navaja process is running.

The listener stays bound for the whole navaja process rather than only during
a challenge. That is a deliberate trade-off: a slightly longer-lived local
listener in exchange for a URL that always answers something useful. It still
binds only the validated private interface (`0.0.0.0`, `::` and empty hosts
are refused), and the token is required on every path. A wrong token returns
a 404 in both the form and idle states.

The `estado_servidor` tool reports whether the listener is bound
(`captcha_listening`) and the real, usable captcha form URL as
`captcha_url`. The URL is part of the API surface on purpose: a caller can
obtain it from `estado_servidor` before running any blocking fetch, hand it
to the human, and the human opens it once and leaves it open. While no
challenge is pending the idle page refreshes itself every 5 seconds and
turns into the captcha form automatically when a challenge arrives. The token
in the URL gates access to the HTTP form rather than being hidden from the
caller; anyone with local access can already read it from the persisted token
file.

`ver_texto_completo` returns structured failures with an `error_code`:
`captcha_timeout` when the human does not answer in time, `captcha_busy`
when a challenge is already pending on the listener, `captcha_rejected` when
the site refuses the answer, `full_text_error` for other fetch failures, and
`invalid_url` when the URL cannot be parsed. Both `captcha_timeout` and
`captcha_busy` include `captcha_url` in the payload so the caller can open
the form without reading stderr. The default `espera_segundos` is `120`,
deliberately well below this deployment's MCP client request timeout (`330`
seconds in the default Pi configuration), so the server has time to return a
structured error before the client kills the call.

There is **no automatic captcha solver** in navaja. The captcha image is never
sent to a vision model, an OCR service, or any third party. A human reads the
image and submits the answer through the local form.

The challenge is session-sticky: once the session has solved it, subsequent
full-text requests in the same session usually do not ask again. In practice
this means roughly **one solve per research session**, not one per resolution.

### PDF persistence

When the final response is a PDF, navaja automatically saves the file to disk
as well as returning the extracted text. This applies to both
`ver_texto_completo` and `navaja-doc`; there is no per-call opt-in.

The destination directory resolves in this order:

1. `NAVAJA_PDF_DIR`, if set.
2. `$XDG_DATA_HOME/navaja/pdfs` when `XDG_DATA_HOME` is set.
3. `~/.local/share/navaja/pdfs` otherwise.

navaja creates the directory on demand.

The filename is taken from the `name=` parameter the server sends in the
`Content-Type` header, for example `name="STS_3679_2026.pdf"`. That value is
sanitized to a safe basename before it reaches the filesystem. If the header
has no usable name, or the name would be unsafe or too long, navaja falls back
to a deterministic name built from the document URL:
`<reference>_<optimize>.pdf`. The `pdf_save_reason` field in the result tells
which rule was used (`server_sent_name`, `missing_name`, `unsafe_name`,
`overlong_name`, `identical_bytes`, ...).

If the response is not a PDF, no file is written and `pdf_save_reason` is
`not_pdf`. A write failure (permissions, full disk, bad `NAVAJA_PDF_DIR`)
never fails the fetch itself: `ok` stays `True`, the full text is returned,
and `pdf_save_error` explains what happened.

For MCP clients, the `ver_texto_completo` result always contains three extra
keys:

* `pdf_path` — the saved file path, or `None`.
* `pdf_save_reason` — why the file has that name, or why nothing was written.
* `pdf_save_error` — `None` on success, otherwise a human-readable message.

### Zero-configuration defaults

By default navaja tries to make the captcha form reachable without manual
configuration:

* **Host:** `navaja-doc` and `navaja-mcp` auto-detect the bind interface in
  this order:
  1. `NAVAJA_CAPTCHA_HOST`, if set.
  2. The IPv4 address of the `tailscale0` interface, if it exists.
  3. `127.0.0.1`.
* **Token:** the captcha URL-path token is stable across runs. It is read from
  `NAVAJA_CAPTCHA_TOKEN` when set, otherwise from
  `$XDG_STATE_HOME/navaja/captcha-token` (default
  `~/.local/state/navaja/captcha-token`). If none exists, a new token is
  generated with `secrets.token_urlsafe(32)` and persisted. The state directory
  is created with mode `0700` and the token file with mode `0600`; the file is
  written atomically so concurrent runs cannot leave it torn.

The chosen host is announced on stderr together with the form URL, so it is
never a silent exposure.

## It runs headless

navaja does **not** open a browser on the machine that runs the server. The
local form runs as a tiny HTTP server, and the human reaches it from any device
that can connect to that interface.

This is deliberately designed for a VPS or other headless host. You can run
`navaja-mcp` on a server with no display and solve the captcha through an
SSH tunnel. For example, if the form is served on `127.0.0.1:8765` on the
remote host, forward it with:

```bash
ssh -L 8765:127.0.0.1:8765 <your-vps>
```

Then open `http://127.0.0.1:8765/<token>/` locally. This has been verified
end to end: a real CENDOJ captcha was solved through an SSH tunnel and the
server returned the PDF on the first attempt. Because the tunnel runs over
port 22, already allowed by typical host firewalls, no `ufw` change was
needed.

## Configuration

Set these environment variables before starting the server:

| Variable | Default | Purpose |
| --- | --- | --- |
| `NAVAJA_CAPTCHA_HOST` | auto-detect | Interface the local form binds to. When unset, navaja picks the `tailscale0` IPv4 address, falling back to `127.0.0.1`. |
| `NAVAJA_CAPTCHA_PORT` | `8765` | Port the local form listens on. |
| `NAVAJA_CAPTCHA_TOKEN` | persisted | Stable URL-path token. When unset, navaja reads or creates `$XDG_STATE_HOME/navaja/captcha-token` (default `~/.local/state/navaja/captcha-token`). Must be at least 16 characters and only contain `A-Z`, `a-z`, `0-9`, `-`, `_`. |
| `NAVAJA_PDF_DIR` | `~/.local/share/navaja/pdfs` | Directory where full-text PDFs are automatically saved. Falls back to `$XDG_DATA_HOME/navaja/pdfs` when `XDG_DATA_HOME` is set. |

If `NAVAJA_CAPTCHA_HOST` is `0.0.0.0`, `::`, or empty, the server refuses to
start. Bind a specific interface.

## MCP client registration

Add this `mcpServers` entry to your MCP client config (for example in Claude,
Cursor, or any stdio MCP host):

```json
{
  "mcpServers": {
    "navaja": {
      "command": "uv",
      "args": [
        "--directory",
        "/home/ubuntu/projects/navaja",
        "run",
        "navaja-mcp"
      ],
      "env": {
        "NAVAJA_CAPTCHA_HOST": "<your-tailnet-ip>",
        "NAVAJA_CAPTCHA_PORT": "8765",
        "NAVAJA_CAPTCHA_TOKEN": "<your-stable-token-at-least-16-chars>"
      }
    }
  }
}
```

`NAVAJA_CAPTCHA_TOKEN` is optional, but a stable token is recommended when the
server binds a non-loopback address. Replace `<your-tailnet-ip>` with your
actual Tailscale IP, or use `127.0.0.1` when the MCP client and the browser run
on the same machine.

## Standalone testing

Fetch a single document from the command line with the `navaja-doc` script:

```bash
navaja-doc "https://www.poderjudicial.es/search/AN/openDocument/<16-or-32-hex-hash>/<YYYYMMDD>"
```

If the default port is already held by a running `navaja-mcp` session,
`navaja-doc` fails fast with an actionable message instead of making a
CENDOJ round-trip first. Use `--port 0` to let the OS pick a free port; the
actual form URL is announced on stderr.

When the response is a PDF, `navaja-doc` saves it automatically to the
directory described in [PDF persistence](#pdf-persistence) above and reports
the path in its stdout output.

To get a real document URL right now, run:

```bash
uv run python -c \
  "from navaja.cendoj import CendojClient; c=CendojClient(); print(c.search('clausulas abusivas').as_dict()['results'][0]['url_documento']); c.close()"
```

Then paste that URL into `navaja-doc`.

## Security rules

The local captcha form is a small web surface. Treat it carefully:

1. **Never bind `0.0.0.0`**. The code rejects it. Bind only a specific
   interface: `127.0.0.1`, a Tailscale IP, or another private address.
2. **Keep the form on a private network** or reach it through an SSH tunnel.
   Do not expose it to the public internet.
3. **Tailscale users:** use `tailscale serve`, **never** `tailscale funnel`.
   `funnel` publishes the service to the public internet; `serve` stays on your
   tailnet.

These rules exist because this project's own host was previously compromised
through an exposed port. Binding narrowly is not a formality here.

## Development

```bash
uv sync
uv run pytest          # deterministic, runs against saved fixtures
NAVAJA_LIVE=1 uv run pytest -m live   # opt-in: hits the real site
```

Current suite: `280 passed, 1 skipped`.

Tests never touch the live site unless `NAVAJA_LIVE=1` is set.

## Verified

The live CENDOJ captcha round-trip has been verified end to end:

```bash
uv run navaja-doc "https://www.poderjudicial.es/search/AN/openDocument/3fb62a5395c8aaa1a0a8778d75e36f0d/20260917"
```

Output:

```
Fetching full text for https://www.poderjudicial.es/search/AN/openDocument/3fb62a5395c8aaa1a0a8778d75e36f0d/20260917
Captcha form will bind to 127.0.0.1 because no tailscale0 interface found
Captcha form binding to 127.0.0.1; ready at http://127.0.0.1:8765/<token>/
Result: success
Attempts: 1
Content-Type: application/pdf; name="STS_3679_2026.pdf"
Text preview: JURISPRUDENCIA Roj: STS 3679/2026 - ECLI:ES:TS:2026:3679 Id Cendoj: 28079110012026101379
Órgano: Tribunal Supremo. Sala de lo Civil Sede: Madrid Sección: 1 Fecha: 10/09/2026
Nº de Recurso: 288/2022 Nº de Resolución: 1413/2026 Procedimiento: Recurso de casación
Ponente: PEDRO JOSE VELA TORRES Tipo de Resolución: Sentencia
```

What this proves:

- A correct human answer to the site's `stickyImg` captcha returns the real
  PDF. The full round-trip works.
- It succeeded on the first attempt, using only the existing captcha POST
  body. No extra cookies, Referer header, or retry dance were needed.
- The local-form-plus-SSH-tunnel flow works on a headless VPS. The tunnel
  runs over port 22, already allowed on `tailscale0`, so the host firewall
  (`ufw`) did not need to be changed.
- `pypdf` extracts clean text from the returned PDF.
- The PDF metadata is richer than the search-result parser currently
  produces. The document carries `Órgano`, `Sede`, `Sección`, `Fecha`,
  `Nº de Recurso`, `Nº de Resolución`, `Procedimiento`, `Ponente`,
  `Tipo de Resolución` and `Id Cendoj`. Notably, it includes
  `Sede: Madrid`, which the current search parser leaves empty for
  Tribunal Supremo rulings.

This verification was performed with `navaja-doc` directly, not through an
MCP client.
