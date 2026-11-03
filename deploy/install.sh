#!/usr/bin/env bash
# Install a pool server on a fresh Ubuntu or Debian machine (docs/admin-guide.md).
#
#   wget -qO install.sh https://raw.githubusercontent.com/Borhan20/distributed-sharing/main/deploy/install.sh
#   sudo bash install.sh
#
# It asks for the server's address and your email address, then installs Docker if needed, generates
# every password and key, gets HTTPS certificates, starts everything, and prints your sign-in details.
# Run it again to upgrade: your settings and data are kept.
#
# Options (all optional; without them you are asked):
#   --address NAME|IP   domain name pointing to this server (free automatic certificates), or an IP address
#   --email ADDRESS     your email: the first admin account, and certificate expiry notices
#   --tls auto|internal auto: Let's Encrypt (needs a domain and ports 80/443 open to the internet)
#                       internal: Caddy's own certificate authority (LAN and test servers). Default: auto
#                       for a domain, internal for an IP address
#   --dir PATH          where pool lives (default /opt/pool)
#   --repo URL --ref R  where to get pool (default: the GitHub repository, branch main)
#   --source DIR        use a local copy of pool instead of downloading it (testing)
#   --proxy URL         reach the internet through this HTTP proxy (apt, Docker, image builds)
#   --yes               do not ask; fail if --address or --email is missing
set -euo pipefail
# SECURITY-SENSITIVE: generates and places every secret, creates the first admin. Needs security review (plan §8.10).

REPO="https://github.com/Borhan20/distributed-sharing.git"
REF="main"
DIR="/opt/pool"
ADDRESS=""
EMAIL=""
TLS=""
SOURCE=""
PROXY="${https_proxy:-${HTTPS_PROXY:-${http_proxy:-${HTTP_PROXY:-}}}}"
ASSUME_YES=0
STARTED=$(date +%s)

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }
while [ $# -gt 0 ]; do
  case "$1" in
    --address) ADDRESS="$2"; shift 2 ;;
    --email) EMAIL="$2"; shift 2 ;;
    --tls) TLS="$2"; shift 2 ;;
    --dir) DIR="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --source) SOURCE="$2"; shift 2 ;;
    --proxy) PROXY="$2"; shift 2 ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
is_ip() { [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ || "$1" == *:* ]]; }

[ "$(id -u)" = 0 ] || fail "run this as root, for example: sudo bash install.sh"
[ -r /etc/os-release ] && . /etc/os-release
case "${ID:-}" in ubuntu|debian) ;; *) fail "this installer supports Ubuntu and Debian (found ${PRETTY_NAME:-unknown})." ;; esac
command -v apt-get >/dev/null || fail "apt-get is missing"

ENV_FILE="$DIR/deploy/.env"
if [ -f "$ENV_FILE" ]; then
  echo "Found an existing installation in $DIR: keeping its settings and data (upgrade)."
  ADDRESS=$(sed -n 's/^POOL_ADDRESS=//p' "$ENV_FILE")
else
  # ---- the questions (at most two) ------------------------------------------------------------
  guess_ip=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1 || true)
  if [ -z "$ADDRESS" ]; then
    [ "$ASSUME_YES" = 1 ] && fail "--address is required with --yes"
    echo "Server address: a domain name that points to this server (for example pool.example.org)."
    echo "Press Enter to use this server's IP address ${guess_ip:-?} instead (members then see a certificate warning)."
    read -r -p "Address: " ADDRESS </dev/tty
    ADDRESS=${ADDRESS:-$guess_ip}
  fi
  [ -n "$ADDRESS" ] || fail "no address given"
  [[ "$ADDRESS" =~ ^[A-Za-z0-9.:-]+$ ]] || fail "the address may only contain letters, digits, dots, dashes and colons"
  if [ -z "$EMAIL" ]; then
    [ "$ASSUME_YES" = 1 ] && fail "--email is required with --yes"
    read -r -p "Your email address (your admin account; certificate notices): " EMAIL </dev/tty
  fi
  [[ "$EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || fail "\"$EMAIL\" does not look like an email address"
  if [ -z "$TLS" ]; then
    if is_ip "$ADDRESS"; then TLS=internal; else TLS=auto; fi
  fi
  case "$TLS" in auto|internal) ;; *) fail "--tls must be auto or internal" ;; esac
  if [ "$TLS" = auto ] && is_ip "$ADDRESS"; then
    fail "automatic certificates need a domain name; use --tls internal for an IP address"
  fi
fi

if [ -n "$PROXY" ]; then
  # apt, curl and git read these; Docker gets its own settings below.
  export http_proxy="$PROXY" https_proxy="$PROXY" HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY"
  # Containers talk to each other by service name; that traffic must never go to the proxy.
  NO_PROXY_LIST="localhost,127.0.0.1,::1,postgres,keycloak,seaweedfs,api,caddy,$ADDRESS"
  export no_proxy="$NO_PROXY_LIST" NO_PROXY="$NO_PROXY_LIST"
  echo "Using the proxy $PROXY for downloads."
fi

step "Installing what the server needs (Docker, git, curl)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git python3 openssl >/dev/null
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  # Docker's documented apt repository install (docs.docker.com/engine/install/ubuntu and /debian).
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID ${VERSION_CODENAME} stable" \
    >/etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
fi
if [ -n "$PROXY" ]; then
  # The daemon pulls images through the proxy; the client passes it to image builds (RUN steps).
  mkdir -p /etc/systemd/system/docker.service.d
  printf '[Service]\nEnvironment="HTTP_PROXY=%s" "HTTPS_PROXY=%s" "NO_PROXY=%s"\n' "$PROXY" "$PROXY" "$NO_PROXY_LIST" \
    >/etc/systemd/system/docker.service.d/http-proxy.conf
  systemctl daemon-reload
  systemctl restart docker
  python3 - "$PROXY" "$NO_PROXY_LIST" <<'PY'
import json, os, sys
path = os.path.expanduser("~/.docker/config.json")
os.makedirs(os.path.dirname(path), exist_ok=True)
cfg = json.load(open(path)) if os.path.exists(path) else {}
cfg.setdefault("proxies", {})["default"] = {"httpProxy": sys.argv[1], "httpsProxy": sys.argv[1], "noProxy": sys.argv[2]}
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
PY
fi
systemctl enable --now docker >/dev/null 2>&1 || true
docker compose version

step "Getting pool into $DIR"
if [ -n "$SOURCE" ]; then
  mkdir -p "$DIR"
  tar -C "$SOURCE" --exclude=./deploy/.env --exclude=./deploy/seaweedfs-s3.json \
      --exclude=./deploy/keycloak/pool-realm.server.json -cf - . | tar -C "$DIR" -xf -
  echo "copied from $SOURCE"
elif [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch --quiet origin "$REF" && git -C "$DIR" checkout --quiet FETCH_HEAD
  echo "updated to $(git -C "$DIR" rev-parse --short HEAD) ($REF)"
else
  git clone --quiet --depth 1 --branch "$REF" "$REPO" "$DIR"
  echo "downloaded $(git -C "$DIR" rev-parse --short HEAD) ($REF)"
fi
cd "$DIR/deploy"

if [ ! -f "$ENV_FILE" ]; then
  step "Creating settings, passwords and keys (deploy/.env, readable by root only)"
  python3 - "$DIR/deploy" "$ADDRESS" "$EMAIL" "$TLS" <<'PY'
import json, os, re, secrets, sys
deploy, address, email, tls = sys.argv[1:5]
handle = re.sub(r"[^a-z0-9._-]+", "-", email.split("@", 1)[0].lower()).strip("-.")[:48] or "admin"
s3_key, s3_secret = secrets.token_hex(16), secrets.token_hex(32)
temp_password = secrets.token_urlsafe(12)

def write(path, text, mode=0o600, uid=None):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, mode)
    if uid is not None:
        os.chown(path, uid, 0)

write(os.path.join(deploy, ".env"), "\n".join([
    "# Generated by deploy/install.sh. Keep it secret and back it up; never commit it.",
    "COMPOSE_FILE=compose.yaml:compose.server.yaml",
    "COMPOSE_PROFILES=storage",
    f"POOL_ADDRESS={address}",
    f"POOL_TLS={email if tls == 'auto' else 'internal'}",
    f"POOL_ADMIN_EMAIL={email}",
    f'POOL_SERVER_ADMINS=["{handle}"]',
    "POOL_GROUP_CREATORS=admins",
    "POOL_BIND=127.0.0.1",
    f"POSTGRES_PASSWORD={secrets.token_hex(24)}",
    f"KEYCLOAK_DB_PASSWORD={secrets.token_hex(24)}",
    f"KEYCLOAK_ADMIN_PASSWORD={secrets.token_hex(24)}",
    f"POOL_S3_ACCESS_KEY={s3_key}",
    f"POOL_S3_SECRET_KEY={s3_secret}",
    "", ""]))

s3 = json.load(open(os.path.join(deploy, "seaweedfs-s3.json.example")))
s3["identities"][0]["credentials"] = [{"accessKey": s3_key, "secretKey": s3_secret}]
# SeaweedFS drops to its "seaweed" user (uid 1000) in the container; only it and root may read the keys.
write(os.path.join(deploy, "seaweedfs-s3.json"), json.dumps(s3, indent=2) + "\n", 0o600, 1000)

realm = json.load(open(os.path.join(deploy, "keycloak", "pool-realm.json")))
base = f"https://{address}"
realm.update({"registrationAllowed": True, "loginWithEmailAllowed": True, "resetPasswordAllowed": False,
              "bruteForceProtected": True, "sslRequired": "external"})
for client in realm["clients"]:
    if client["clientId"] == "pool-web":
        client["redirectUris"] = [f"{base}/*"]
        client["webOrigins"] = [base]
        client.setdefault("attributes", {})["post.logout.redirect.uris"] = "+"
realm["users"] = [{
    "username": handle, "email": email, "emailVerified": True, "enabled": True,
    "credentials": [{"type": "password", "value": temp_password, "temporary": True}],
    "requiredActions": ["UPDATE_PASSWORD"],
}]
# Keycloak runs as uid 1000 in its container; only it (and root) may read the one-time password.
write(os.path.join(deploy, "keycloak", "pool-realm.server.json"), json.dumps(realm, indent=2) + "\n", 0o600, 1000)
# Kept (root only) until an install run completes, so a run that fails halfway does not lose it.
write(os.path.join(deploy, ".first-admin"), f"{handle} {temp_password}\n")
PY
fi
envget() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -1; }
POOL_TLS=$(envget POOL_TLS)

step "Starting the database"
docker compose up -d --wait postgres
# Keycloak keeps its accounts in its own database next to pool's (idempotent).
# The password goes through stdin, never the command line (where other users could see it).
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U pool -d pool -q <<SQL
SELECT 'CREATE ROLE keycloak LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'keycloak') \gexec
ALTER ROLE keycloak PASSWORD '$(envget KEYCLOAK_DB_PASSWORD)';
SELECT 'CREATE DATABASE keycloak OWNER keycloak' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'keycloak') \gexec
SQL

step "Building and starting pool (the first time takes a few minutes)"
docker compose up -d --build --wait
# Caddy reads its config from a mounted file that this run may have replaced; a container keeps
# the old file until it is recreated, so recreate it (a second or two without HTTPS on upgrades).
docker compose up -d --no-deps --force-recreate --wait caddy

step "Turning on two-step sign-in for coordinators and admins"
# Keycloak reports password and authenticator-app steps in its tokens (amr), so pool can require them.
KEYCLOAK_ADMIN_PASSWORD="$(envget KEYCLOAK_ADMIN_PASSWORD)" \
  python3 "$DIR/deploy/keycloak/configure.py" --url http://127.0.0.1:8080/idp

step "Checking HTTPS on https://$ADDRESS"
CURL=(curl -fsS --max-time 10)
# A domain may not resolve to this machine from itself (cloud NAT); an IP address is used as is.
is_ip "$ADDRESS" || CURL+=(--resolve "$ADDRESS:443:127.0.0.1")
if [ "$POOL_TLS" = internal ]; then
  for _ in $(seq 1 30); do docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt "$DIR/deploy/pool-ca.crt" >/dev/null 2>&1 && break; sleep 2; done
  [ -s "$DIR/deploy/pool-ca.crt" ] || fail "Caddy did not create its certificate authority; see: docker compose logs caddy"
  chmod 644 "$DIR/deploy/pool-ca.crt"
  CURL+=(--cacert "$DIR/deploy/pool-ca.crt")
fi
ok=0
for _ in $(seq 1 60); do
  if "${CURL[@]}" "https://$ADDRESS/readyz" >/dev/null 2>&1 \
     && "${CURL[@]}" "https://$ADDRESS/idp/realms/pool/.well-known/openid-configuration" >/dev/null 2>&1; then
    ok=1; break
  fi
  sleep 3
done
[ "$ok" = 1 ] || fail "https://$ADDRESS does not answer yet. Certificates need the domain to point here and ports 80 and 443 open; see: docker compose logs caddy"

# The realm (with the admin's one-time password) is imported by now; do not keep that password on disk.
python3 - "$DIR/deploy/keycloak/pool-realm.server.json" <<'PY'
import json, sys
path = sys.argv[1]
realm = json.load(open(path))
if realm.pop("users", None) is not None:
    with open(path, "w") as f:
        json.dump(realm, f, indent=2)
        f.write("\n")
PY
FIRST_ADMIN="$DIR/deploy/.first-admin"
if [ -f "$FIRST_ADMIN" ]; then
  read -r ADMIN_HANDLE ADMIN_PASSWORD <"$FIRST_ADMIN"
fi

MINUTES=$(( ($(date +%s) - STARTED + 59) / 60 ))
cat <<EOF

pool is running at https://$ADDRESS   (installed in about $MINUTES min)
EOF
if [ -n "${ADMIN_PASSWORD:-}" ]; then
  cat <<EOF

  Sign in:  https://$ADDRESS
  User:     $ADMIN_HANDLE
  Password: $ADMIN_PASSWORD   (one-time: you choose your own at first sign-in; it is not stored anywhere)

  You are the server admin. On this server only server admins create groups:
  create one, then invite people from its Invites tab. Members register on the sign-in page.
  Invites, group settings and votes need two-step sign-in: the web page offers to set up an
  authenticator app (any TOTP app, e.g. on your phone) the first time.
EOF
  rm -f "$FIRST_ADMIN"  # shown once; from now on it exists only on your screen
fi
if [ "$POOL_TLS" = internal ]; then
  FPR=$(openssl x509 -in "$DIR/deploy/pool-ca.crt" -noout -fingerprint -sha256 | cut -d= -f2)
  cat <<EOF

  Certificates come from this server's own authority, so browsers warn until it is trusted.
    CA certificate: $DIR/deploy/pool-ca.crt
    SHA-256:        $FPR
  Give that file to people who share computers; they enroll with:
    pool-agent enroll --server https://$ADDRESS --server-ca pool-ca.crt --code CODE
  To switch to a domain name later (trusted certificates), see docs/admin-guide.md section 5.
EOF
fi
cat <<EOF

  Settings:  $DIR/deploy/.env (back it up; it holds every password)
  Manage:    cd $DIR/deploy && docker compose ps | logs -f api | down
  Upgrade:   run this installer again
  Guide:     https://$ADDRESS/guide   and   $DIR/docs/admin-guide.md
  Firewall:  members need ports 80 and 443 of this server.
EOF
