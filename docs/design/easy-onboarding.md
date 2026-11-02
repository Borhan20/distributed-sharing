# Easy onboarding: design proposal

| | |
|---|---|
| **Status** | Proposed, for review |
| **Date** | 3 November 2026 |
| **Plan links** | Phase 1 exit "invite → enrolled device → first job < 15 minutes" (plan §9), owner tray and kill switch (§7.6), signed releases (Phase 5), self-hosting (ADR 0001) |

## 1. Goal

Joining a group and sharing a computer should feel like joining a shared
document, not like setting up a server. Only one person, the admin, does
technical setup, once. Everyone else uses a web browser, plus one normal
installer if they want to share their computer.

Nobody except the admin should ever see source code, YAML files, endpoints,
keys or certificates.

### Decisions already made (3 November 2026)

- The server can run anywhere, but we design mainly for a **cloud server**.
- There is **no domain name yet**; there may be one later. The design must work without one.
- Members use mostly **Linux**, some **Windows** and some **macOS**.
- Delivery order: the four agreed steps in [§8](#8-delivery-order-and-acceptance-checks), preceded by small fixes (step 0) found while trying the dev stack.

## 2. What it is like today

Found while trying the local dev stack on 3 November 2026:

| What the person did | What happened | Why |
|---|---|---|
| Created a group and submitted the job shown in the web form | The job stayed pending | The form's default job uses a placeholder image (`registry.example.org/...@sha256:000...`) that cannot be downloaded (`web/src/lib/jobspec.ts`) |
| Opened the Devices page on the PC running the server | "No devices yet" | The server and the sharing agent are separate. A PC becomes a device only after the agent is installed, enrolled, given a policy and started |
| Waited for the job | No explanation | The job page does not say why tasks wait (no device in the group, not enough memory, image cannot be pulled) |
| Followed the Devices page instructions | The command fails on a dev server | The shown `pool-agent enroll` command leaves out `--insecure-http`, which a plain-http localhost server needs. It also does not mention the policy file or `pool-agent run` |
| Shared a device | Two separate steps | The device must be shared in the web UI **and** the group added to `policy.yaml` on the device |

Sharing a computer today means: build the agent from source, type commands,
edit a YAML file, keep a terminal open. That is the biggest gap.

## 3. Principles

1. **Browser first.** Every member task except installing the sharing app happens in the web UI.
2. **One installer, no codes to copy.** A computer joins by clicking *Approve* in the browser.
3. **Safe by default.** A new device shares nothing until its owner picks a group. Stopping everything is one click.
4. **Explain waiting.** Every pending job says why, in plain words, and what would fix it.
5. **The admin answers few questions.** Everything that can be generated or derived is.
6. **Same security as today.** Every change below keeps the plan's security model (§8): short-lived device certificates, signed task envelopes, sandboxed containers, audit log. Changes to these paths need human security review.

## 4. Admin: set up the server once

**Target:** on a fresh cloud server, run one command, answer at most three
questions, and get a working HTTPS site with login, in under 15 minutes.

1. Rent a small Linux cloud server. It has to stay on.
2. Run the installer. It asks only:
   - the server's address: a domain name if you have one, otherwise nothing (see below);
   - an email address for certificate expiry notices;
   - whether to allow sign-in with an external account (optional, can be added later).
3. The installer:
   - installs Docker if it is missing;
   - generates every password and key (database, Keycloak admin, object store, device CA, signing key);
   - fills in every setting, including both object-store addresses;
   - starts PostgreSQL, API, scheduler, Keycloak, SeaweedFS and a reverse proxy;
   - prints a **one-time setup link**.
4. The admin opens the setup link and creates the admin account. The link works once, so nobody else can claim the server first by being faster.

### HTTPS without manual certificates

The reverse proxy would be **Caddy** (Apache-2.0). It obtains and renews free
Let's Encrypt certificates by itself, so nobody handles certificates.

- **With a domain:** point the domain (e.g. `pool.example.org`) at the server; Caddy does the rest.
- **Without a domain** (our situation now), options to test in a spike:
  1. a free hostname derived from the server's IP address, from a public wildcard-DNS service, with a Let's Encrypt certificate;
  2. a Let's Encrypt certificate for the bare IP address, if that is available to us at the time;
  3. a tunnel service that provides an HTTPS address.
  Each has limits (rate limits, availability, third-party dependency) that the spike must measure. Moving to a real domain later should be one command that re-runs the address step.
- Self-signed certificates are **not** an option for members: browsers show warnings, which is the opposite of easy.

### One public address

Today the object store needs its own public address
(`POOL_S3_PUBLIC_ENDPOINT`). The installer should instead route the bucket's
path (`/pool-artifacts/...`) on the main address to SeaweedFS. Presigned URLs
then use the same address as the site, and there is nothing extra to set up.

To verify in the spike: the proxy must pass the `Host` header and the path
unchanged, or the URL signatures fail; and no API route may use that path.

### Sign-in for members

- Keycloak (already bundled) with self-registration on. A new account sees
  nothing until it accepts an invite, because groups are invite-only.
- Optional sign-in with an external account (Google, Microsoft, GitHub,
  university login) through Keycloak identity brokering. It is not required,
  because each provider needs its own app registration by the admin.
- Open question: password reset and email verification need an email (SMTP)
  service. Without one, invite links are shared by chat and the admin resets
  passwords.

## 5. Coordinator: create a group and invite people

Most of this already exists in the web UI: create a group, create an invite
link, approve join requests. Additions:

- Share the invite link with a **Copy** button and a short ready-made message.
- The group page shows **"Computers in this group: 0"**, with a button to share your own.

## 6. Member who runs jobs: browser only

1. Click the invite link, then sign up or sign in, then **Join group**.
2. Submit a job from a **form** instead of YAML:
   - pick a template (e.g. "Run a Python script", "Parameter sweep");
   - type an image name such as `python:3.12`. The server looks up and **pins the exact version (digest)** at submit time and shows it, so jobs stay reproducible without the member knowing what a digest is;
   - set values to sweep (e.g. seeds 0–9), resources and time limit;
   - YAML stays available under "Advanced" for experienced users.
3. Watch progress and logs, and **download outputs** from the job page (uses the object store's presigned download URLs).
4. Every waiting job shows a reason, for example:
   - "No computer is shared with this group yet. Share yours."
   - "Needs 4 GB memory; the largest shared computer offers 2 GB."
   - "All computers are busy; position 3 in the queue."
   - "The image could not be downloaded: check its name."

macOS members use this browser path. Sharing a Mac is out of scope for v1
(plan non-goals: Docker on macOS cannot use the Apple GPU).

## 7. Member who shares a computer: one installer

### Install

The group page has **Share my computer**. It detects the visitor's system and offers:

| System | Package | Runs as |
|---|---|---|
| Linux (main target) | `.deb` and `.rpm`, plus a one-line install script served by the group's own server | systemd service |
| Windows | Installer (`.msi` or `.exe`) | Windows service, plus the user-session helper the plan already requires for idle detection (§7.6) |
| macOS | Not in v1 | — |

The installer checks prerequisites and guides the person through them:
Docker on Linux (rootless preferred, as plan §8.3 requires for T1), WSL2 on
Windows (may need admin rights and a reboot).

### Pair the computer (no codes to copy)

1. After install, the app opens the browser at the server's **pairing page** with a short code already filled in.
2. The page, where the member is already signed in, shows the computer's name, system, CPU, memory and GPU, and asks **"Connect this computer to your account?"**
3. On **Approve**, the agent receives its certificate (the same enrollment as today: one-time code plus a key generated on the device).
4. The member picks which groups may use the computer. This writes the share on the server **and** the local policy in one step, replacing today's two separate steps.

Security notes for review:

- Pairing codes expire after 10 minutes and work once; requests are rate-limited.
- Risk: someone tricks a member into approving the attacker's computer. The attacker's computer could then receive that member's group jobs. Mitigations: show the computer's details on the approval page, only approve codes the member's own browser opened, notify the member of every new device, and keep one-click revoke.

### Control it from the tray

A tray icon replaces editing `policy.yaml`:

- **Sharing on / Paused**, and a red **Stop everything now** (the kill switch the plan requires).
- When to share: "only when I'm away" (default), "always", or a schedule.
- How much: CPU, memory and GPU sliders, per group.
- What is running now, and who sent it ("what ran here").
- Notifications when a job starts on this computer.

Defaults: share nothing until a group is chosen; only when idle; keep at least
one CPU core and some memory for the owner.

### Local development

The PC that runs a dev stack should be able to share itself with one action:
a dev script that starts the stack **and** enrolls this PC for the signed-in
dev user. On the Devices page, dev mode should show a working copy-paste
command (with `--insecure-http`) and the remaining steps.

## 8. Delivery order and acceptance checks

Each step is its own branch and pull request.

| Step | Scope | Done when (measured, not estimated) |
|---|---|---|
| 0. Quick fixes (**done**, plus output downloads, a user guide and `scripts/dev-up.sh`) | Runnable default job in the form; working enroll command and full steps on the Devices page; pending reasons on the job page; one-step share | On a fresh dev stack, a new user's default job runs, and a pending job explains itself |
| 1. Easy server install | Installer, Caddy, generated secrets, one public address, setup link, self-registration; the no-domain spike | On a fresh cloud VM: one command, ≤ 3 questions, no file edited by hand, HTTPS login in < 15 minutes |
| 2. Share my computer | Release builds from CI, `.deb`/`.rpm`/install script, Windows installer, services, pairing page | A Linux member goes from invite link to "computer shown in group" in < 10 minutes without editing a file; same check on Windows |
| 3. Tray app | Pause, resume, kill switch, limits, groups, what ran here | Every policy setting can be changed without opening a file; the kill switch stops containers within seconds (test) |
| 4. Job form | Templates, image-name pinning, sweeps, output download | A member submits a sweep and downloads its outputs without writing YAML or a digest |

The plan's Phase 1 exit check, "invite → enrolled device → first job in under
15 minutes", should be timed with a real new user after step 2.

## 9. What we cannot make disappear

- **Docker or WSL2** on computers that share; the installer can only guide.
- **Code signing:** Windows warns about unsigned installers. A code-signing certificate has a yearly cost; signed releases are planned for Phase 5 and may need to move earlier.
- **An always-on server** with a monthly cost, unless a university or lab server is available.
- **No hosted service:** a service run by us would be easiest of all, but ADR 0001 decided on self-hosting only. This proposal keeps that decision.

## 10. Open questions and spikes

1. **HTTPS without a domain:** which of the options in §4 works reliably, and its limits.
2. **One public address:** confirm the proxy keeps `Host` and path so presigned URLs stay valid.
3. **Email:** do we bundle or require an SMTP service for invites and password reset?
4. **Tray app technology:** a cross-platform toolkit with a permissive licence that passes `scripts/check_licenses.py`.
5. **Rootless Docker** set up by the Linux installer: how much can be automated safely.
6. **Image pinning at submit:** the server needs registry access; private registries need credentials (secrets are Phase 3).
7. **macOS sharing:** revisit after v1, as the plan says.
