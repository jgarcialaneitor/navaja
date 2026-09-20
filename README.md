# navaja

MCP server for personal [CENDOJ](https://www.poderjudicial.es/search/indexAN.jsp)
case-law research.

Ask for case law, get candidates with metadata and the site's own automatic
summaries, and read the full text of the resolutions you pick.

## What navaja is

`navaja-mcp` exposes three tools for an MCP client:

- `buscar_sentencias` — free-text search. No captcha is needed.
- `ver_texto_completo` — fetch the full text of a resolution.
- `estado_servidor` — runtime configuration snapshot.

Search, metadata and the automatic summaries come straight from the public
results page. Only the full-text step can trigger the site's
`Control Descargas masivas` captcha.

## How full-text retrieval works

When you ask for the full text of a resolution, navaja requests the document
from CENDOJ. If the site responds with its captcha page, navaja starts a
**short-lived local HTTP form** on a private network interface, serves the
captcha image, and waits for a human to type the answer.

There is **no automatic captcha solver** in navaja. The captcha image is never
sent to a vision model, an OCR service, or any third party. A human reads the
image and submits the answer through the local form.

The challenge is session-sticky: once the session has solved it, subsequent
full-text requests in the same session usually do not ask again. In practice
this means roughly **one solve per research session**, not one per resolution.

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

Current suite: `59 passed, 1 skipped`.

Tests never touch the live site unless `NAVAJA_LIVE=1` is set.

## Verified

The live CENDOJ captcha round-trip has been verified end to end:

```bash
uv run navaja-doc "https://www.poderjudicial.es/search/AN/openDocument/3fb62a5395c8aaa1a0a8778d75e36f0d/20260917"
```

Output:

```
Fetching full text for https://www.poderjudicial.es/search/AN/openDocument/3fb62a5395c8aaa1a0a8778d75e36f0d/20260917
Captcha form ready at http://127.0.0.1:8765/<token>/
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
