# pool server admin guide

One person, the server admin, sets up a pool server once. Everyone else only
needs a browser, plus the agent if they share a computer (see the
[user guide](user-guide.md)).

**Contents**

1. [What you get](#1-what-you-get)
2. [Before you start](#2-before-you-start)
3. [Install](#3-install)
4. [First steps after installing](#4-first-steps-after-installing)
5. [HTTPS: with or without a domain name](#5-https-with-or-without-a-domain-name)
6. [Everyday administration](#6-everyday-administration)
7. [Security notes](#7-security-notes)
8. [Troubleshooting](#8-troubleshooting)
9. [Not done yet](#9-not-done-yet)

## 1. What you get

Everything runs in Docker on one machine, behind one HTTPS address:

```
                      https://ADDRESS
                            │
                    Caddy (HTTPS, certificates)
          ┌─────────────────┼──────────────────────┐
     /idp/...        /pool-artifacts/...       everything else
     Keycloak            SeaweedFS              pool API + web UI
    (sign-in)          (job output files)       + scheduler
          └───────── PostgreSQL ─┘
```

- **Public:** only ports 80 (redirects to HTTPS) and 443.
- **Private:** the database, the file store's own port, Keycloak's admin console and its master realm are reachable only from the server itself.
- **Computers that share:** they connect out to `https://ADDRESS`; they need no open ports.

## 2. Before you start

- **A Linux server that stays on:** Ubuntu or Debian. Tested on Ubuntu 24.04 with 2 CPUs, 4 GB of memory and a 20 GB disk.
- **Ports 80 and 443** open to the people who will use it. In a cloud, that is the provider's firewall or security group.
- **A domain name pointing to the server** (recommended), e.g. `pool.example.org`. With it, certificates are free and automatic, and browsers trust them. Without one, see [§5](#5-https-with-or-without-a-domain-name).
- An email address for your admin account and certificate notices.

## 3. Install

On the server:

```bash
wget -qO install.sh https://raw.githubusercontent.com/Borhan20/distributed-sharing/main/deploy/install.sh
sudo bash install.sh
```

It asks two questions:

1. **Address:** your domain name, or press Enter to use the server's IP address.
2. **Your email address:** your admin account, and certificate notices.

Then it installs Docker if needed, downloads pool to `/opt/pool`, generates
every password and key, starts everything, checks HTTPS, and prints:

```
pool is running at https://pool.example.org   (installed in about 8 min)

  Sign in:  https://pool.example.org
  User:     jane
  Password: ...   (one-time: you choose your own at first sign-in; it is not stored anywhere)
```

Write the one-time password down: it is shown once. If an install run stops
halfway, run it again; it shows the password at the end of the first run that
completes.

**Options**, for scripts or special networks (`--help` lists them):

| Option | Use |
|---|---|
| `--address`, `--email`, `--yes` | Install without questions |
| `--tls internal` | Use the server's own certificate authority even with a domain (closed networks) |
| `--proxy http://proxy:3128` | The server reaches the internet only through a proxy |
| `--ref BRANCH` | Install another branch or tag |

## 4. First steps after installing

1. Open `https://ADDRESS` and sign in with the user and one-time password. You are asked to choose your own password.
2. **Set up two-step sign-in.** A banner offers it: add pool to an authenticator app on your phone (any TOTP app), then sign in once more with a code. Invites, group settings and votes need it, for every coordinator and admin.
3. **Create a group** (Groups → Create a group). On a server installed this way, only server admins create groups, so strangers who register cannot use your server for themselves.
4. **Invite people:** open the group → Invites → Create invite, and send the link. They choose *Register* on the sign-in page, then accept the invite.
5. To let a group run itself, invite someone as its **coordinator**; coordinators run their group without you.
6. Share at least one computer with the group (My computers in the web UI), or the group's jobs wait.

## 5. HTTPS: with or without a domain name

**With a domain name** (`--address pool.example.org`): Caddy gets certificates
from Let's Encrypt and renews them by itself. The domain must point to the
server, and ports 80 and 443 must be reachable from the internet while
certificates are issued.

**Without one** (`--address 203.0.113.10`, or Enter at the question): Caddy
uses its own certificate authority (CA). Everything works and traffic is
encrypted, but nobody trusts that CA by default:

- **Browsers** show a warning until the CA is trusted. Import `/opt/pool/deploy/pool-ca.crt` into the browser or operating system, after checking its SHA-256 fingerprint (the installer prints it).
- **Agents** must be told about it: copy `pool-ca.crt` to the computer and enroll with `pool-agent enroll --server https://ADDRESS --server-ca pool-ca.crt --code CODE`.

This suits a lab network or a test. For people outside your network, get a
domain name. Changing the address later is not automated yet:

1. Set `POOL_ADDRESS` (and `POOL_TLS`, your email for automatic certificates) in `/opt/pool/deploy/.env`.
2. Change the `pool-web` client's redirect URI and web origin in Keycloak's admin console ([§7](#7-security-notes)).
3. Run `docker compose up -d` in `/opt/pool/deploy`.
4. Enrolled computers must enroll again, because they remember the server address.

## 6. Everyday administration

Run these in `/opt/pool/deploy` as root:

| Task | Command |
|---|---|
| What is running | `docker compose ps` |
| Logs | `docker compose logs -f api` (or `scheduler`, `keycloak`, `caddy`, `seaweedfs`) |
| Restart everything | `docker compose restart` |
| Stop / start | `docker compose down` / `docker compose up -d` (data is kept) |
| Upgrade | Run the installer again. Settings and data are kept; the database is migrated. |

**Back up**, regularly, to another machine (backup and restore have not been
tested end to end yet):

- `/opt/pool/deploy/.env`: every password. Without it, the rest cannot be read.
- **The database:** `docker compose exec -T postgres pg_dumpall -U pool > pool-db.sql`. It holds groups, jobs, accounts (Keycloak), and the audit log.
- **The Docker volumes** `pool_pool-keys` (the device CA and signing key: lose it and every computer must enroll again), `pool_seaweed-data` (job output files) and `pool_caddy-data` (certificates and, without a domain, the CA).

## 7. Security notes

- **Keycloak's admin console** is not public. Reach it through an SSH tunnel:
  `ssh -L 8080:127.0.0.1:8080 you@server`, then open `http://127.0.0.1:8080/idp/admin`.
  The user is `admin`; the password is `KEYCLOAK_ADMIN_PASSWORD` in `.env`.
  Use it to reset a member's password (the server sends no email yet), or to turn registration off: realm `pool` → Realm settings → Login → User registration.
- **Two-step sign-in** is required for coordinators' and admins' privileged actions (`POOL_REQUIRE_MFA_FOR_ADMINS`, on by default). Members are never asked for a code unless they set one up themselves. The installer configures Keycloak to report the authenticator-app step in its tokens (`deploy/keycloak/configure.py`, re-applied on every upgrade).
  If someone loses their phone: in Keycloak's admin console, Users → that user → Credentials → delete the OTP entry; they set up a new app at the next banner prompt.
- **Who creates groups:** `POOL_GROUP_CREATORS=admins` in `.env` (installer default) or `anyone`. Restart with `docker compose up -d` after changing it.
- **Server admins:** the handles in `POOL_SERVER_ADMINS`. Add yours there to make another person a server admin.
- **Secrets on disk:** `.env` and the file store's keys are readable only by root and, for the keys, the container's own user (uid 1000). On many cloud images uid 1000 is the default login user.
- **Not yet reviewed:** pool has not had its security review (plan §8.10). Do not use it for confidential data yet.

## 8. Troubleshooting

| Problem | Check |
|---|---|
| The installer says the address "does not answer yet" | With a domain: does it point to this server, and are ports 80 and 443 open? `docker compose logs caddy` shows the certificate attempts. |
| Browsers warn about the certificate | Expected without a domain name ([§5](#5-https-with-or-without-a-domain-name)). |
| Sign-in page does not load | `docker compose logs keycloak`. Keycloak needs about a minute after a restart. |
| An agent cannot connect | `server URL must use https`: use `https://ADDRESS`. A certificate error with an IP address: enroll with `--server-ca pool-ca.crt`. |
| Jobs wait | The job page says why; see the user guide's troubleshooting. |

## 9. Not done yet

- Email: password reset and email verification need an SMTP server, not yet configured.
- Automatic address change, automatic backups, more than one server for high availability.
