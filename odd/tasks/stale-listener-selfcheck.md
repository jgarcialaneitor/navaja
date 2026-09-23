# Feature: stale-listener self-check (#18)

GitHub issue: https://github.com/jgarcialaneitor/navaja/issues/18
Branch: `feat/stale-listener-selfcheck` (off `main` `cd6c819`)

## Problem

`estado_servidor` reports `captcha_listening: true` and the form URL, but it
cannot tell whether the listener behind that URL is *this* process. Measured in
#17: two navaja processes on the same port, same persisted token → same URL;
the tool reported the correct URL while the browser connected to the other
process, whose page always showed *idle*.

## Design (from the issue, decisions recorded here)

1. Per-process nonce: each `CaptchaServer` gets a random nonce at creation.
2. New token-scoped endpoint `GET /{token}/whoami` (inside the existing
   token-gated surface; 404 without a valid token) returning JSON:
   `{"listener": "navaja-captcha", "nonce": <hex>, "pid": <int>}`.
3. `estado_servidor` self-check: when it thinks a listener is running, it GETs
   its own `captcha_url` + `whoami` with a bounded timeout. Self-owned check
   first (in-process registry, zero network): if a local `CaptchaServer`
   exists for `(host, port)` and its serve thread is alive, the listener is
   ours. Only when the local registry has no live server for that address does
   it do the network whoami GET:
   - whoami reachable with our nonce → `captcha_listening: true`, `pid: <ours>`.
   - whoami reachable, different nonce or not navaja → someone else (or a
     stale process) holds the port: `captcha_listening: false`,
     `listener_conflict: true`, `listener_pid: <their pid>`.
   - whoami unreachable/timeout → `captcha_listening: false`,
     `listener_conflict: false` (something listens but is not ours and did not
     answer as navaja).
4. New keys published on `estado_servidor`: `pid`, `nonce`, `listener_conflict`,
   `listener_pid` (null when not conflicting). `captcha_listening` only claims
   true when this process can prove the listener answers with its own nonce.

Decision: vocabulary kept minimal and additive; existing keys keep meaning.

## Tasks

- [ ] 1. Create branch, nonce in `CaptchaServer` + `/whoami` endpoint (TDD)
- [x] 2. Self-check helper `whoami_check` in captcha.py (network, bounded) — commit `fb08a68`
- [x] 3. Wire into `estado_servidor` (new keys, self-owned fast path) (TDD) — commit `fb08a68`
- [x] 4. README: document the new keys + conflict meaning — commit `docs(readme)`
- [ ] 5. Full suite + lint, work-unit commit, PR
