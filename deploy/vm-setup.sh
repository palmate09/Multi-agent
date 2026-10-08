#!/usr/bin/env bash
# GCP VM bootstrap for the multi-agent team stack.
#
# Run as the deploy user (or root) on a fresh Ubuntu 22.04/24.04 VM:
#   curl -O https://raw.githubusercontent.com/OWNER/REPO/main/deploy/vm-setup.sh
#   sudo bash vm-setup.sh
#
# Installs Docker, creates the app directory and the non-privileged deploy
# user, and prints the follow-up commands you must run yourself.
set -euo pipefail

DEPLOY_USER="${DEPLOY_USER:-deploy}"
APP_DIR="${APP_DIR:-/opt/multi-agent-team}"
SWAP_SIZE="${SWAP_SIZE:-4G}"

log()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root: sudo bash vm-setup.sh"

log "Installing base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  ca-certificates curl gnupg git jq ufw htop python3 python3-venv

log "Installing Docker from the official repository"
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
fi
docker --version
docker compose version

log "Adding $DEPLOY_USER to the docker group"
if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
  useradd -m -s /bin/bash "$DEPLOY_USER"
  # A deploy key is added in the GitHub secrets step; authorize a key now.
  install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "/home/$DEPLOY_USER/.ssh"
fi
usermod -aG docker "$DEPLOY_USER"

log "Creating $APP_DIR"
mkdir -p "$APP_DIR/deploy"
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$APP_DIR"

log "Configuring swap (${SWAP_SIZE}) for Ollama on a low-RAM VM"
if swapon --show=NAME --noheadings | grep -q .; then
  log "swap already active, skipping"
elif ! grep -q "/swapfile" /etc/fstab; then
  fallocate -l "$SWAP_SIZE" /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=4096
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
else
  log "/swapfile already in fstab"
fi

log "Tuning kernel settings for container workloads"
cat > /etc/sysctl.d/99-multi-agent.conf <<'EOF'
vm.max_map_count = 262144
fs.file-max = 65535
EOF
sysctl --system >/dev/null

log "Firewall: allow SSH, HTTP, HTTPS; drop the rest"
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp comment 'SSH' >/dev/null
ufw allow 80/tcp comment 'HTTP' >/dev/null
ufw allow 443/tcp comment 'HTTPS' >/dev/null
ufw --force enable >/dev/null
ufw status verbose

log "Enabling unattended security updates"
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq unattended-upgrades
systemctl enable --now unattended-upgrades

log "Done"
cat <<EOF

Next steps (run as the deploy user):

  1. Authorize your deploy key (from your laptop):
       ssh-copy-id $DEPLOY_USER@<VM_EXTERNAL_IP>

  2. Pull the app and build locally for a first run:
       sudo -u $DEPLOY_USER bash -c "cd $APP_DIR && \\
         git clone <REPO_URL> repo && cp -r repo/deploy/. deploy/"

  3. Add the required secrets (never commit these):
       sudo -u $DEPLOY_USER bash -c "printf 'REGISTRY=ghcr.io/OWNER\\nTAG=latest\\nSITE_ADDRESS=:80\\nSKIP_OLLAMA=0\\n' > $APP_DIR/.env"

  4. Roll it out:
       sudo -u $DEPLOY_USER bash -c "cd $APP_DIR && \\
         docker compose --env-file .env -f deploy/docker-compose.prod.yml up -d"

  5. Optional: pull local models so the LLM path is real.
       sudo -u $DEPLOY_USER bash -c "docker run --rm -v ollama-models:/root/.ollama \\
         ollama/ollama pull qwen2.5-coder:7b-instruct-q4_K_M"
       # then run the stack with: docker compose ... --profile ollama up -d
       # and set OLLAMA_URL=http://ollama:11434 in .env

EOF