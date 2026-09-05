# Remote access: what is decided, and what is deliberately not built

Status: **design only. Nothing here is implemented.** Written down so the
reasoning does not have to be rebuilt, and so the constraints are not
rediscovered the hard way.

---

## 1. What already works without any of this

| Need | Covered by |
|---|---|
| Alert when a print fails | Telegram/Discord push, with the annotated photo |
| Look at the printer right now | `/check` returns a fresh annotated frame |
| Pause or cancel from anywhere | Telegram/Discord buttons, with nonce + expiry + state re-check |
| Watch the annotated view at home | Mainsail's camera list, or `:58888` |

**All of it over outbound connections.** Nothing listens on the internet,
nothing is forwarded, and there is no link that can be forwarded to
someone else. The only case not covered is **watching a live stream from
outside the LAN**, and that is the only thing the rest of this file is
about.

---

## 2. OctoPrint's own position, which this follows

[A Guide To Safe Remote Access of OctoPrint](https://octoprint.org/blog/2018/09/03/safe-remote-access/),
with an editor's note from Gina (OctoPrint's author):

> "A guest post by Jubaleth on a topic that is very dear to my heart and on
> which **I'm starting to sound like a broken record** — please heed this
> warning and invest the time that properly securing internal services
> needs."

The post's own summary of the rule:

> "there are many ways one can safely access an OctoPrint instance
> remotely, that **do not involve blindly forwarding ports on your router**"

Its recommendations, in the order given: **plugins** (cloud or
messenger-based) for everyone, then **VPN**, then **reverse proxy** for
advanced users. It also notes the reverse proxy or VPN should ideally run
on **a separate box** from the printer.

⚠️ The word that matters is **"blindly"**. What is rejected is exposure
with no protection in front of it — not tunnels as such. ngrok's plugin is
recommended by name, and the reason is visible in its README: it ships
**Basic Auth by default**. A bare Cloudflare Quick Tunnel is the same
technology with that one part missing.

**→ The test is not "which product". It is: if this link reaches a
stranger, what can they do?**

---

## 3. Why authentication alone is not enough

⚠️ **MFA and a strong password protect the login. They do nothing about a
vulnerability that fires before the login.**

Queried from OSV for the `octoprint` PyPI package: **48 known
vulnerabilities, of which 4 are described as exploitable without
authentication.** The clearest:

```
GHSA-2vjq-hg5w-5gm7
"OctoPrint has an Authentication Bypass via X-Forwarded-For Header"
```

Two more are denial of service via a malformed HTTP request, one of them
from this year (`PYSEC-2026-1713`).

**So the defence that matters most is the one that stops the request from
reaching OctoPrint's code at all** — an authenticating reverse proxy in
front. That is a different class of defence from a password:

| Layer | Stops | Does not stop |
|---|---|---|
| **nginx auth (Basic or token)** | ⭐ **pre-auth bugs, scanning, brute force** | someone holding valid credentials |
| OctoPrint password | ordinary access | ⚠️ pre-auth bugs |
| OctoPrint MFA (TOTP) | stolen or guessed passwords | ⚠️ pre-auth bugs |

⚠️ **Weak passwords void all of it.** One account with `admin/123456`
among the users makes every layer above decorative — and OctoPrint has no
lockout after failed logins by default, so an exposed instance is brute
forced continuously. **Auditing every account's password is a prerequisite
for exposing anything, not an optional extra.**

**OctoPrint's own MFA is available and official**: `OctoPrint/OctoPrint-MfaTotp`,
requires OctoPrint >= 1.11.0, standard TOTP (Google Authenticator, Aegis,
1Password). Install via the Plugin Manager, then User Settings -> 2FA: TOTP
-> Enroll.

---

## 4. The design, if this is ever built

```
① Telegram:  /remote
      -> service mints a one-time token, starts cloudflared
② bot replies:  https://<random>.trycloudflare.com/gate?t=<token>
③ phone opens it -> nginx -> PiNozCam /gate
      -> token verified with secrets.compare_digest
      -> signed short-lived cookie set, 302 to /
④ OctoPrint's interface loads
⑤ every later request: nginx auth_request -> PiNozCam /api/authcheck
      -> 200 pass / 401 refuse
```

Verified available on the CB2: **nginx 1.26.3 with
`--with-http_auth_request_module`**, which is what step ⑤ needs.

⚠️ The token is exchanged for a cookie rather than kept in the URL. A URL
carrying the credential would appear in browser history, in logs, in every
image and API request the page makes, and would leak in full if forwarded.

### Decided controls

| Control | Decision | Reason |
|---|---|---|
| **Token TTL, 2 min** | ✅ do | Trivial, no downside |
| **Token single-use** | ✅ do | `pop()`, not `get()`. Whoever opens it first gets the session; everyone after gets 403. Same shape as OAuth's authorization code |
| **Session TTL, 15 min** | ✅ do | This is what actually bounds the exposure window |
| **Manual kill** | ✅ do | `/stopremote` kills cloudflared; the hostname ceases to exist for the whole internet |
| **IP binding** | ⛔ **do not** | See below |

### ⛔ Why not IP binding

**It breaks normal use.** A phone moving between Wi-Fi and mobile data
changes IP, as does walking out of the house, as does carrier NAT rotation.
The user gets thrown out mid-view and it looks like a fault.

⚠️ **And through a tunnel the visible IP is Cloudflare's, not the user's.**
The real address arrives in a `CF-Connecting-IP` header — **and headers are
client-supplied**. Trusting one is exactly the mistake behind
`GHSA-2vjq-hg5w-5gm7` above. Doing it safely means accepting that header
only from Cloudflare's published IP ranges and keeping that list current.
**A control that both misfires on real users and depends on a mechanism
with a known CVE is not worth its cost.**

`/stopremote` covers the same intent — revoke access now — without either
problem.

### Non-negotiable implementation details

- **Sign the cookie** (HMAC). An unsigned one can simply be forged.
- **`Secure` + `HttpOnly` + `SameSite=Lax`.**
- **Every location must pass through `auth_request` except `/gate`.**
  ⚠️ One missed `location` block — static files, an API prefix — is an
  open door.
- **Proxy the WebSocket upgrade headers.** OctoPrint's live state needs
  them; without them the UI loads and then spins forever.
- If a read-only *video* link is built instead of full access, that page
  must carry **no external links at all** and
  `<meta name="referrer" content="no-referrer">` — otherwise the browser
  sends the token-bearing URL in the `Referer` header to whatever it links
  to.

### Why the tunnel's worst property is the right one here

Quick Tunnel's hostname is regenerated on every start; cloudflared's own
source returns a fresh `ID`, `Name`, `Hostname`, `AccountTag` and `Secret`
per run, and the secret only ever lives in memory. Normally that makes it
useless for anything permanent.

⚠️ But **Telegram and Discord both retain messages indefinitely by
default** (recorded elsewhere in this project). The link will still be
sitting in the chat months later, so the message's lifetime cannot bound
the exposure — only the tunnel's can. **A hostname that dies with the
process is exactly the property needed.**

Quick Tunnel also needs **no account at all** — cloudflared POSTs
anonymously and is handed credentials — at the cost of what its own
disclaimer states: *"account-less Tunnels have no uptime guarantee... and
Cloudflare reserves the right to investigate your use of Tunnels."*

---

## 5. Recommendation: do not build this yet

Everything in section 1 already works, with no listening port, no
forwarded link and no account anywhere. This whole design exists to serve
one case — **a live stream from outside the LAN** — and `/check` returning
a fresh photo on demand covers most of what that is wanted for.

⚠️ And the two options are not equal in consequence:

| Exposed | If the link leaks |
|---|---|
| Read-only `/live` + token | someone watches your printer |
| **Full OctoPrint UI** | ⚠️ **someone controls your printer** — arbitrary G-code, heater setpoints. `OctoPrint-FirmwareCheck` exists because thermal runaway is a real failure mode |

If it is built, build the read-only video view first.

---

## 6. Nobody else in this ecosystem does it

Checked the three established messenger integrations for any tunnel or
temporary-link feature. Keywords `tunnel`, `cloudflare`, `ngrok`,
`temporary link`, `expose` in each README:

| Project | Hits |
|---|---|
| `jacopotediosi/OctoPrint-Telegram` | **0** |
| `nlef/moonraker-telegram-bot` (331★) | **0** |
| `cameroncros/OctoPrint-DiscordRemote` | **0** |

**Not one of them offers a remote link.** What the OctoPrint Telegram
plugin offers instead is telling: *"videos from your webcams"* — it sends
the footage **into the chat**, rather than handing out a way in.

⚠️ That is the mature pattern in this ecosystem: **push the content, do
not publish an entrance.**

| | Push content | Hand out a link |
|---|---|---|
| Listening port | none | required |
| Link that can be forwarded | none exists | yes |
| Third-party tunnel dependency | none | yes |
| User has to install something | no | cloudflared |

### The cheaper middle option

Sending a **short video clip** instead of a still photo carries far more
information than `/check` does today, needs no tunnel, no port, no token
and no nginx — and it is what the largest Telegram integration already
does. **If "a photo is not enough" is the real complaint, that is the
change worth making, not this document's design.**
