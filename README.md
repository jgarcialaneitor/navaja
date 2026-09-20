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

## It runs headless

navaja does **not** open a browser on the machine that runs the server. The
local form runs as a tiny HTTP server, and the human reaches it from any device
that can connect to that interface.

This is deliberately designed for a VPS or other headless host. You can run
`navaja-mcp` on a server with no display and open the form URL on your laptop,
phone, or Tailscale-connected device.

## Configuration

Set these environment variables before starting the server:

| Variable | Default | Purpose |
| --- | --- | --- |
| `NAVAJA_CAPTCHA_HOST` | `127.0.0.1` | Interface the local form binds to. Use a Tailscale IP such as `100.x.y.z` when the human is on another machine. |
| `NAVAJA_CAPTCHA_PORT` | `8765` | Port the local form listens on. |
| `NAVAJA_CAPTCHA_TOKEN` | random per run | Optional stable URL-path token. Must be at least 16 characters and only contain `A-Z`, `a-z`, `0-9`, `-`, `_`. |

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
navaja-doc "https://www.poderjudicial.es/search/AN/openDocument/<32-hex-hash>/<YYYYMMDD>"
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

## Unverified

The live CENDOJ captcha round-trip — a real `stickyImg` image served by the
site, answered by a human through the local form, followed by an actual PDF or
HTML response — has not been exercised end to end. That step requires a human
with a browser and a real document URL. Everything else is covered by offline
tests.
