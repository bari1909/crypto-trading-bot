#!/usr/bin/env bash
# One-time hardening + Docker install for a fresh Ubuntu 24.04 server. Run as root:
#   ssh root@SERVER 'bash -s' < deploy/server_setup.sh
# Result: user "trader" (SSH key only, sudo, docker), root & password logins disabled,
# firewall allowing only SSH, automatic security updates, 2 GB swap, UTC clock, daily DB backups.
set -euo pipefail

USER_NAME=trader

echo "== packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get upgrade -yq
apt-get install -yq ca-certificates curl ufw unattended-upgrades fail2ban

echo "== user $USER_NAME"
if ! id "$USER_NAME" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "$USER_NAME"
fi
usermod -aG sudo "$USER_NAME"
echo "$USER_NAME ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/90-$USER_NAME
chmod 440 /etc/sudoers.d/90-$USER_NAME
install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" /home/$USER_NAME/.ssh
install -m 600 -o "$USER_NAME" -g "$USER_NAME" /root/.ssh/authorized_keys /home/$USER_NAME/.ssh/authorized_keys

echo "== ssh: keys only, no root"
cat > /etc/ssh/sshd_config.d/90-hardening.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
EOF
systemctl reload ssh || systemctl reload sshd

echo "== firewall: SSH only (dashboards stay private, reach them through an SSH tunnel)"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable

echo "== automatic security updates"
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "== swap 2G"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi

echo "== timezone UTC"
timedatectl set-timezone UTC

echo "== docker (official apt repository)"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -yq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
usermod -aG docker "$USER_NAME"
systemctl enable --now docker

echo "== docker log rotation"
cat > /etc/docker/daemon.json <<'EOF'
{ "log-driver": "json-file", "log-opts": { "max-size": "20m", "max-file": "5" } }
EOF
systemctl restart docker

echo "== daily database backup (03:15 UTC, keeps 14 days)"
install -d -o "$USER_NAME" -g "$USER_NAME" /home/$USER_NAME/backups
cat > /etc/cron.d/scalper-backup <<EOF
15 3 * * * $USER_NAME cd /home/$USER_NAME/bot && for db in scalper scalper_futures; do docker compose exec -T db pg_dump -U scalper \$db | gzip > /home/$USER_NAME/backups/\$db-\$(date +\%F).sql.gz; done; find /home/$USER_NAME/backups -name '*.sql.gz' -mtime +14 -delete
EOF

echo "SETUP DONE. From now on log in as: ssh $USER_NAME@<server-ip>"
