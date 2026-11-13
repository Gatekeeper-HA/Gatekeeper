#!/bin/sh
# Prepare a new Ubuntu 24.04 server for cloud/compose.yml. Run it once, as root:
#
#     sh setup-server.sh <admin user>
#
# - an admin user, with root's SSH keys, sudo and Docker;
# - SSH with keys only, and no root login;
# - a firewall: SSH, DNS and HTTP(S) only;
# - security updates installed automatically, rebooting at 04:00 when one needs it;
# - Docker, with its logs capped.
set -eu

ADMIN=${1:?usage: sh setup-server.sh <admin user>}

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get -y upgrade
apt-get -y install docker.io docker-compose-v2 ufw unattended-upgrades git

id "$ADMIN" >/dev/null 2>&1 || adduser --disabled-password --gecos "" "$ADMIN"
usermod -aG sudo,docker "$ADMIN"
install -d -m 700 -o "$ADMIN" -g "$ADMIN" "/home/$ADMIN/.ssh"
install -m 600 -o "$ADMIN" -g "$ADMIN" /root/.ssh/authorized_keys "/home/$ADMIN/.ssh/authorized_keys"
echo "$ADMIN ALL=(ALL) NOPASSWD:ALL" >"/etc/sudoers.d/90-$ADMIN"
chmod 440 "/etc/sudoers.d/90-$ADMIN"

# Read before cloud-init's 50-cloud-init.conf, so these win.
cat >/etc/ssh/sshd_config.d/10-hubnames.conf <<EOF
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
EOF
systemctl try-reload-or-restart ssh

# Docker's published ports (53, 80, 443) bypass ufw; it guards the rest.
ufw default deny incoming
ufw allow 22/tcp
ufw allow 53
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

cat >/etc/apt/apt.conf.d/52hubnames <<EOF
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:00";
EOF
systemctl enable --now unattended-upgrades

cat >/etc/docker/daemon.json <<EOF
{"log-driver": "json-file", "log-opts": {"max-size": "10m", "max-file": "3"}}
EOF
systemctl restart docker

echo "Done. Log in as $ADMIN from now on; root can't log in over SSH."
