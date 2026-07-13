# How sbx egress ("the firewall") works, and how danno reaches local Ollama — for review

**Date:** 2026-07-10 · **Author:** Claude (Opus 4.8), from a live investigation on
`sbx v0.34.0`, macOS (Apple Silicon), Docker Desktop. · **Purpose:** an
independent-review record of my current understanding — the model, the evidence, the
plan, and **explicitly what is test-verified vs inferred**, so a reviewer can find my
mistakes. (Context: earlier in the same session I reached several *wrong* "certain"
conclusions that only testing caught — see "Where I could still be wrong".)

> **Read this first — the one methodology rule.** Judge reachability by the **HTTP
> status code**, never by a tool's exit code. `curl` without `-f` exits **0 on a 403**,
> so "curl succeeded" ≠ "reached". All allow/deny claims below use
> `curl -s -o /dev/null -w '%{http_code}'`: **200 = reached, 403 = proxy-denied,
> 000 = connection failed (bypassed the proxy / nothing listening)**. My first big wrong
> conclusion ("sbx doesn't isolate") came from reading curl's exit code instead.

## 0. REVIEW OUTCOME (2026-07-10, independent session) — §3 refuted, §6 unnecessary

The review this document requested happened: official-docs research plus a fresh live
matrix and real harness turns (`sbx v0.34.0`, Docker Desktop 29.4.2, macOS). Verdict:

- **§1/§2/§4 mechanism — CONFIRMED.** Proxy-only egress; `balanced` = curated
  default-deny; rules match literally and are port-scoped; the proxy dials from the
  host's own network position; `127.0.0.1:11434`-through-proxy genuinely works.
- **§3 — REFUTED: sbx HAS the `host.docker.internal`→`localhost` rewrite.** It is
  officially documented ([sandbox usage docs](https://docs.docker.com/ai/sandboxes/usage/):
  *"The sandbox proxy translates this to `localhost` before routing the request"*, with
  the exact example `sbx policy allow network localhost:11434`) and live-verified:
  with allow token `localhost:11434`, a **default-env**
  `curl http://host.docker.internal:11434/api/tags` → **200**, while `example.com`/
  LAN/`host.docker.internal:22` → 403. §3's 403 came from allowing the **wrong
  token** (`host.docker.internal:11434`): matching is literal on the **post-rewrite**
  string, so the rule must say `localhost:11434` — the same token the legacy backend
  uses. Converse verified: with only `127.0.0.1:11434` allowed (danno's current sbx
  resolver output), the documented `host.docker.internal` dial → **403** — the
  workaround doesn't duplicate the rewrite, it **blocks** the natural path.
- **§5's harness pessimism — REFUTED for the Node/Bun clients.** opencode 1.17.11
  completed a **real model turn** under sbx with danno's normal config (`baseURL
  http://host.docker.internal:11434/v1` + allow `localhost:11434`) — Bun fetch honors
  the proxy env. sbx injects `NODE_USE_ENV_PROXY=1` (in-sandbox `node fetch` → 200),
  and sbx **accepts CONNECT to an allowed non-443 port** (`curl --proxytunnel` → 200
  allowed / 403 unallowed), so undici-based occ works too. `--env-file NO_PROXY` IS
  overridden (re-confirmed), but plain `sbx exec -e NO_PROXY=…` **is honored** — and
  none of the `NO_PROXY` surgery turns out to be needed at all.
- **§6 relay rework — UNNECESSARY.** The relay's exact opener pattern (explicit
  `ProxyHandler`, upstream `host.docker.internal`) was run in-sandbox under the
  `localhost:11434` token → **200 unchanged**. No `DANNO_RELAY_UPSTREAM_HOST`, no
  `no_proxy`-clearing, no self-loop risk. Whether the relay is needed *at all* is now
  a per-harness/per-backend question — see the corrected Phase-2 in
  [`plan-sbx-migration.md`](plan-sbx-migration.md).
- **The §3 citation `NVIDIA/OpenShell#263` is a MIS-CITE.** That issue belongs to
  NVIDIA's OpenShell sandbox project (analogous prior art), not sbx's tracker; the
  `SBX-WORKAROUND(OpenShell#263)` code markers inherit the error.
- Legacy `docker sandbox` was re-verified the same day with danno's exact flags —
  contract holds, but **legacy denials are HTTP 500 with a policy body, not 403**
  (see [`plan-sbx-migration.md`](plan-sbx-migration.md) W7). This narrows the
  methodology rule above: on legacy, judge by **status + body**, not 403 alone.

Sections below are preserved as the original investigation record; inline
**[REFUTED 2026-07-10 — see §0]** notes mark the overturned claims.

## 1. The model (how I think it works, and why)

**Each sbx sandbox is a microVM with its own network namespace.** The only way out is a
**host-side HTTP(S) proxy** — Docker's docs say exactly this: *"The only way traffic can
leave a sandbox is through an HTTP/HTTPS proxy on your host, which enforces access rules
on every outbound request."* UDP and ICMP are blocked at the network layer; non-HTTP TCP
needs explicit allow rules.

Inside the sandbox (observed via `sbx exec … env`):

```
HTTP_PROXY  = HTTPS_PROXY  = http://gateway.docker.internal:3128   (also lowercase)
NO_PROXY    = no_proxy     = localhost,127.0.0.1,::1,gateway.docker.internal
```

So: a well-behaved HTTP client sends everything to `gateway.docker.internal:3128`
**except** the `NO_PROXY` hosts, which it connects to directly.

**Policy** (`sbx policy …`): a **global base** (`sbx policy init allow-all|balanced|
deny-all`) plus **allow/deny rules** (global or `--sandbox <name>`-scoped). **Deny beats
allow.** Rule resources are hostnames/domains/IPs with optional `:port`; `**` = all
hosts. Crucially, **`balanced` is not network-default-allow**: `sbx policy ls` shows it
grants two curated allow-lists — `default-ai-services` (e.g. `api.anthropic.com:443`,
`**.openai.com:443`) and `default-package-managers` (pypi, npm, crates, …:443) — and
**nothing else on the network is permitted** (arbitrary hosts are denied). (It also has
`filesystem read/write allow-all`, unrelated to egress.)

**Why enforcement is at the proxy, not the kernel:** the proxy inspects each request's
target host:port against the policy and returns **403** for a denied one. This is why the
*routing* of a request (does it go through the proxy at all?) is decisive — a request
that bypasses the proxy is neither filtered **nor** delivered to the host.

**The proxy connects to targets from the *host's* network position.** This is the single
most important consequence and the thing I got wrong twice. The proxy is host-side, so
when it is asked to reach `X:port`, it dials `X:port` **as the host would** — including
the host's own loopback.

## 2. Evidence (the test matrix)

All rows: base policy `balanced`, one sandbox, a single per-sandbox allow rule, requests
from inside via `sbx exec`, read as HTTP codes.

| Allow rule | Request | Routing | Code | Meaning |
|---|---|---|---|---|
| `10.0.1.9:11434` (Mac LAN IP) | `http://10.0.1.9:11434/api/tags` | proxied (not in NO_PROXY) | **200** | reached Ollama |
| `10.0.1.9:11434` | `https://example.com` | proxied | **403** | internet denied |
| `10.0.1.9:11434` | `http://10.0.1.1` (LAN router) | proxied | **403** | LAN denied |
| `10.0.1.9:11434` | `http://10.0.1.9:22` (SSH, same host) | proxied | **403** | **port-scoped** |
| `127.0.0.1:11434` | `http://127.0.0.1:11434/api/tags` **forced `--proxy`** | proxied | **200** | reached Ollama via host loopback |
| `127.0.0.1:11434` | `http://127.0.0.1:11434/api/tags` (default) | **NO_PROXY → direct** | **000** | hit the sandbox's own loopback |
| `127.0.0.1:11434` | `http://127.0.0.1:22` forced `--proxy` | proxied | **403** | port-scoped |
| *(none)* | `http://127.0.0.1:11434` forced `--proxy` | proxied | **403** | it's the policy, not an open proxy |

Name resolution **inside** the sandbox (observed): `host.docker.internal → fe80::1`
(link-local), `gateway.docker.internal → fd91:…::` (the proxy host, ULA), `localhost →
::1`, `127.0.0.1 → 127.0.0.1` (the sandbox's own loopback).

## 3. Why the docker-style aliases DON'T reach Ollama on sbx **[REFUTED 2026-07-10 — see §0]**

- **`docker sandbox` (legacy) rewrote `host.docker.internal`→`localhost` on the host
  side**, so its allow-list token `localhost:11434` mapped to the host's Ollama. **sbx has
  no such rewrite** — this is the whole gap (upstream feature request:
  [NVIDIA/OpenShell#263](https://github.com/NVIDIA/OpenShell/issues/263)).
  **[REFUTED: sbx has the same rewrite — documented and live-verified (§0). The
  OpenShell#263 link is a mis-cite: a different project's analogous request.]**
- On sbx, `host.docker.internal` resolves to `fe80::1` — a link-local address the policy
  can't match and that doesn't reach Ollama; allowing `host.docker.internal:11434` +
  dialing it returned **403**. **[The observation is real but the inference was wrong:
  the in-sandbox DNS answer is irrelevant to a proxied request (the proxy gets the
  hostname), and the 403 was the wrong ALLOW TOKEN — the rule must name the
  post-rewrite `localhost:11434`, which was never tested. With it: 200 (§0).]**
- `gateway.docker.internal` is the proxy host; Ollama isn't on it (`:11434 → 000`).
- `localhost`/`127.0.0.1` **direct** are the sandbox's *own* loopback (a different netns
  from the host) → **000**.

## 4. How to reach local Ollama, and why (the mechanism)

Ollama on the Mac binds `0.0.0.0:11434`, so it listens on **every** host interface,
including the host's loopback `127.0.0.1`. The sbx proxy is host-side. Therefore:

> **Route `127.0.0.1:11434` through the host proxy** → the proxy connects to
> `127.0.0.1:11434` **from the host** → the host's own loopback → **Ollama** → 200.

Verified (table §2). And it is **policy-enforced** (no rule → 403) and **port-scoped**
(`:22` → 403). This is *better* than the LAN-IP approach: **network-independent** (no LAN
IP that changes with wifi/VPN), **works offline** (loopback always exists on the host),
and unambiguously *this host* (a LAN IP could be another machine).

**Two things are therefore required to reach a same-host Ollama:**
**[SUPERSEDED 2026-07-10 — see §0: the simpler documented route is allow
`localhost:11434` + dial `host.docker.internal` through the proxy (the rewrite
exists), which needs NEITHER of the two steps below. The mechanism described here
still works, but danno's `127.0.0.1:11434` token 403s the documented route.]**

1. **Allow `127.0.0.1:11434`** in the sbx policy. *(danno does this today — `configure_proxy`
   resolves a local Ollama alias to `127.0.0.1`. Verified through `provision()`:
   `127.0.0.1:11434 → 200`, `example.com`/LAN/`:22` → 403.)*
2. **Make the harness's HTTP traffic to `127.0.0.1:11434` go THROUGH the proxy** rather
   than bypass it (default `NO_PROXY` contains `127.0.0.1`, so a naive client bypasses to
   the sandbox's empty loopback = 000). **This is the unfinished part.**

## 5. Why requirement #2 is hard (verified findings)

Making a client route `127.0.0.1` through the proxy needs the client to (a) have
`127.0.0.1` removed from its effective `NO_PROXY`, **and** (b) actually honor `HTTP_PROXY`.

- **`sbx exec --env-file NO_PROXY=…` is IGNORED** — inside the exec, `NO_PROXY` still
  resolved to sbx's injected value (`localhost,127.0.0.1,::1,gateway.docker.internal`). So
  danno cannot set it via the env-file it already uses.
- **An in-shell `export` works, but must set BOTH `NO_PROXY` and `no_proxy`** — setting
  only uppercase gave **000** (curl honored the still-`127.0.0.1` lowercase `no_proxy`);
  setting both gave **200**.
- **Even with `NO_PROXY` fixed, only `HTTP_PROXY`-honoring clients benefit.** `curl` does.
  The harness clients are the problem: **opencode (Node), claurst (Rust), occ (Node)**. I
  have *not re-tested* these this session, but danno's existing architecture is strong
  evidence they do NOT honor it: **claurst and occ already dial an in-sandbox relay** at
  `127.0.0.1:11434` precisely because their clients ignore the proxy env (claurst's Rust
  client is documented in-code as ignoring `HTTP(S)_PROXY`).
  **[REFUTED for opencode 2026-07-10 — a real turn worked with no changes (§0). occ's
  undici also works on sbx (CONNECT to allowed ports accepted). The claurst/occ relay
  exists for the LEGACY squid (CONNECT-to-:11434 rejected / Rust client), not for sbx;
  and the whole `NO_PROXY` problem dissolves once the dial target is
  `host.docker.internal` (not in `NO_PROXY`) instead of `127.0.0.1`.]**

## 6. The proposed fix (the relay) — **[WITHDRAWN 2026-07-10 — see §0 and the corrected Phase-2 in `plan-sbx-migration.md`; the relay works on sbx UNCHANGED under the `localhost:11434` token, and most harnesses need no relay at all]**

danno already runs a small Python **relay** inside the sandbox listening on
`127.0.0.1:11434` (`driver.py:_OLLAMA_RELAY_SOURCE`), used by claurst and occ. The harness
dials `127.0.0.1:11434` **directly** (it's in `NO_PROXY`, so this hop needs no proxy — it
reaches the relay in the sandbox). The relay, a process danno controls, then forwards to
the host Ollama **through the proxy** (it can force this in its own `urllib`).

Today the relay's `UPSTREAM = http://host.docker.internal:11434` → broken on sbx. The
change:

1. Add `DANNO_RELAY_UPSTREAM_HOST` (default `host.docker.internal`); the claurst/occ
   launchers set it to **`127.0.0.1`** when the backend is sbx.
2. In the relay, when a proxy is configured, **clear `no_proxy`/`NO_PROXY`** so
   `urllib.ProxyHandler` forces even `127.0.0.1` through the proxy (otherwise urllib's
   proxy-bypass sends it to the sandbox's own loopback = the relay itself → loop). No-op on
   docker (its upstream isn't loopback).
3. **opencode** does *not* use the relay (it relied on the docker proxy-rewrite). On sbx,
   route opencode through the relay too (base_url `http://127.0.0.1:11434/v1` + launch the
   relay), unless a real turn shows opencode honors `HTTP_PROXY` (I doubt it).

**Verification gate (per harness):** a real model turn returns output **and** the boundary
still shows `example.com`/LAN → 403.

## 7. Remote Ollama (Ollama on a different machine)

Loopback can't reach another host. For a remote Ollama the sandbox must name the remote's
**real routable IP:port** (a concrete base_url, which danno passes through literally and
allows). Verified analog: allowing the Mac's LAN IP `10.0.1.9:11434` and dialing it → 200,
while the LAN router and other ports stay 403.

## 8. What is VERIFIED vs INFERRED **[the 2026-07-10 review (§0) resolved every "inferred" item: opencode honors the proxy (real turn), the relay works without changes, and the doc's central §3 inference was wrong]**

**Verified this session (HTTP codes / observed env, on `sbx v0.34.0`/macOS):**
- Egress is proxy-enforced; `balanced` is default-deny for arbitrary hosts (§1, §2).
- A per-sandbox allow of exactly `host:port` permits that and denies everything else,
  including other ports on the same host (port-scoped).
- `127.0.0.1:11434` **through the proxy** reaches the host's Ollama (200); direct → 000;
  no allow → 403 (§2, §4).
- Name resolutions of the docker aliases inside the sandbox (§2).
- `--env-file NO_PROXY` is overridden; in-shell `export` needs both cases (§5).
- danno's `configure_proxy` emits `127.0.0.1:11434` and the boundary holds through
  `provision()` (§4).

**Inferred, NOT re-tested this session (a reviewer should challenge these):**
- That opencode/occ/claurst clients don't honor `HTTP_PROXY` — based on danno's existing
  relay design + in-code notes, not a fresh test.
- That the relay change (§6) will work end-to-end — reasoned from the mechanism, not run.
- That the proxy runs literally on the Mac host process vs a dedicated gateway VM — I say
  "host-side"; the ULA address for `gateway.docker.internal` suggests a gateway netns, but
  the *behavior* (it reaches the host's `127.0.0.1`) is what's verified, not the topology.

## 9. Where I could still be wrong (invitation to the reviewer) **[answered in §0: relay-urllib = works (no no_proxy-clearing needed); opencode = honors the proxy; loopback egress = port-scoped and policy-enforced on both backends; and the biggest wrong thing was §3, which this section didn't list]**

- **Does the relay's `urllib` actually force `127.0.0.1` through the proxy after clearing
  `no_proxy`?** Reasoned, not tested. If `ProxyHandler` still bypasses loopback, the relay
  loops on itself.
- **Does opencode honor `HTTP_PROXY`?** If yes, the simpler env route (base_url +
  both-case `NO_PROXY` via an in-shell export wrapper) could avoid routing opencode through
  the relay.
- **Is allowing host-loopback egress an SSRF-class risk?** I argue no: it's port-scoped
  (`:22`→403) and identical in kind to allowing the host's LAN Ollama, which is the
  intended hole. But a reviewer should confirm the policy can't be widened by a wildcard.
- **`sbx` is experimental ("the API will change").** Everything here is `v0.34.0`-specific
  and may shift.

## 10. How to reproduce (minimal)

```bash
# boundary probe (run each request THROUGH the proxy to test the policy, not NO_PROXY):
N=t; sbx create --name $N shell /tmp; sbx policy allow network --sandbox $N 127.0.0.1:11434; sbx stop $N
sbx exec $N sh -lc 'for u in http://127.0.0.1:11434/api/tags http://127.0.0.1:22 https://example.com; do
  c=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 --noproxy "" --proxy http://gateway.docker.internal:3128 "$u");
  echo "$u -> $c"; done'   # expect 200 / 403 / 403
sbx rm --force $N
```

Related: [`plan-sbx-migration.md`](plan-sbx-migration.md) (full migration + Phase-2 plan),
[`sbx-workarounds.md`](sbx-workarounds.md) (the declared, removable accommodations), and
the `sandbox-security-contract-fail-loud` memory (the isolation invariant + the
verify-by-403 lesson).
