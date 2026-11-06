# Server install test in a local VM

Tests `deploy/install.sh` on a fresh Ubuntu server without a cloud account, using an LXD
virtual machine. The VM has no public address, so Let's Encrypt cannot be tested here: the
installer runs with `--tls internal` (Caddy's own CA). Everything else is the real path.

Needs: LXD with VM support (`/dev/kvm`), Docker, Go, `google-chrome` and `uv` on this machine.

```bash
# 1. A fresh server (Ubuntu 24.04, like a small cloud VM), and a snapshot to start over from
lxc launch ubuntu:24.04 pool-install-test --vm -c limits.cpu=2 -c limits.memory=4GiB -d root,size=20GiB
lxc exec pool-install-test -- cloud-init status --wait
lxc snapshot pool-install-test fresh                 # later: lxc restore pool-install-test fresh
VM=$(lxc list pool-install-test --format csv -c 4 | cut -d' ' -f1)

# 2. Copy this working tree in and install, as an admin would
git ls-files -co --exclude-standard | tar -czf /tmp/pool-src.tgz -T -
lxc file push /tmp/pool-src.tgz pool-install-test/root/pool-src.tgz
lxc exec pool-install-test -- sh -c 'mkdir -p /root/pool-src && tar -xzf /root/pool-src.tgz -C /root/pool-src'
lxc exec pool-install-test -- bash /root/pool-src/deploy/install.sh --source /root/pool-src \
    --address "$VM" --email admin@example.org --yes          # add --proxy URL if needed, see below
# note the one-time password it prints

# 3. Check it from this machine, like members would
lxc file pull pool-install-test/opt/pool/deploy/pool-ca.crt /tmp/pool-ca.crt
(cd agent && CGO_ENABLED=0 go build -o /tmp/pool-agent ./cmd/pool-agent)
uv run python tests/vm/server_acceptance.py --address "$VM" --ca /tmp/pool-ca.crt \
    --admin admin --admin-password ONE-TIME-PASSWORD --agent /tmp/pool-agent
```

## When the VM has no internet

On a development machine that runs Docker and `ufw`, the firewall may drop traffic forwarded
from the LXD bridge: the VM resolves names but cannot connect anywhere. Two ways out:

- **Proxy (no firewall change):** run `python tests/vm/forward_proxy.py --listen 10.188.221.1:3128`
  (your `lxdbr0` address) and install with `--proxy http://10.188.221.1:3128`. It listens only on
  the bridge, so only local VMs can use it.
- **Allow the bridge through the firewall** (needs sudo; the iptables rules last until reboot):

  ```bash
  sudo iptables -I DOCKER-USER -i lxdbr0 -j ACCEPT
  sudo iptables -I DOCKER-USER -o lxdbr0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
  sudo ufw route allow in on lxdbr0
  sudo ufw route allow out on lxdbr0
  ```
