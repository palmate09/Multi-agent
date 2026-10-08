# Deploying to a GCP VM

Target: one GCP VM running Docker Compose, fronted by Caddy. The API and the
built SPA run as containers; the UI is reachable over the VM's external IP.

```
browser ──HTTP(S)──> Caddy ──> nginx (SPA + /api proxy) ──> FastAPI (uvicorn)
                                                              │
                                                              ├──> Ollama (host or sidecar)
                                                              └──> pytest sandbox (local subprocess)
```

## 0. What you need first

| Item | Why | Notes |
|---|---|---|
| GCP project with billing enabled | compute + quota | any project |
| `gcloud` authenticated | `gcloud compute instances create` | already on this laptop |
| A GitHub repo + `ghcr.io` access | image registry | images are private by default |
| A deploy SSH key | CI → VM deploys | add the public key in step 2 |

## 1. Create the VM

e2-medium (2 vCPU / 4 GB) is enough for the UI and API. **e2-standard-4 (4 vCPU /
16 GB) is the minimum that makes local Ollama usable** — a 7B q4 model needs
~5 GB resident, and a 4 GB VM will swap to a crawl.

```bash
PROJECT_ID=your-project
ZONE=us-central1-a
NAME=multi-agent-team

gcloud config set project "$PROJECT_ID"

gcloud compute firewall-rules allow-multi-agent-team \
  --network=default --direction=INGRESS --action=ALLOW \
  --rules=tcp:22,tcp:80,tcp:443 --target-tags=multi-agent-team

gcloud compute instances create "$NAME" \
  --zone="$ZONE" \
  --machine-type=e2-standard-4 \
  --tags=multi-agent-team \
  --image-family=ubuntu-2404-lts-amd64 \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=60GB \
  --tags=multi-agent-team

IP=$(gcloud compute instances describe "$NAME" --zone="$ZONE" \
  --format='value(networkInterfaces[0].accessConfigs[0].natIP)')
echo "$IP"
```

The firewall rule is the network-level control. `vm-setup.sh` also configures
`ufw` on the VM itself, so the two must agree — the script opens exactly
22/80/443.

## 2. Bootstrap the VM

```bash
gcloud compute scp deploy/vm-setup.sh "$NAME:$ZONE:/tmp/vm-setup.sh"
gcloud compute ssh "$NAME" --zone="$ZONE" --command \
  "sudo DEPLOY_USER=deploy bash /tmp/vm-setup.sh"
```

This installs Docker + the compose plugin, creates the `deploy` user in the
`docker` group, adds 4 GB of swap, tightens sysctls, and applies `ufw`.

Then add **your laptop's public key** so CI can deploy:

```bash
ssh-copy-id deploy@"$IP"
```

## 3. GitHub secrets and variables

Repository → Settings → Secrets and variables → Actions.

**Secrets** (all required):

| Secret | Value |
|---|---|
| `VM_HOST` | the VM external IP |
| `VM_SSH_USER` | `deploy` |
| `VM_SSH_PRIVATE_KEY` | contents of the private key matching the authorized key |

**Variables**:

| Variable | Value | Purpose |
|---|---|---|
| `SITE_ADDRESS` | `:80` | plain HTTP on port 80 (default) |

The `GITHUB_TOKEN` used to push and to authenticate on the VM is provided
automatically by Actions; it needs the package to be public or the VM account
to be a collaborator on the repo.

Make the packages public if the VM's Docker login cannot see them:

```bash
gh package set-visibility ghcr.io/$OWNER/multi-agent-backend -a public 2>/dev/null || true
```

## 4. First deploy by hand

Do this once before wiring up CI, so a failure is easy to read.

```bash
ssh deploy@"$IP" 'mkdir -p ~/app && cd ~/app'
scp -r deploy deploy@"$IP":~/app/
ssh deploy@"$IP"
```

On the VM:

```bash
cd ~/app
printf 'REGISTRY=ghcr.io/OWNER\nTAG=latest\nSITE_ADDRESS=:80\nSKIP_OLLAMA=1\n' > .env
docker compose --env-file .env -f deploy/docker-compose.prod.yml up -d --build
docker compose --env-file .env -f deploy/docker-compose.prod.yml ps
curl -s localhost/health | jq
```

Expect three services: `caddy`, `frontend`, `backend` (healthy). Then browse
`http://$IP`.

## 5. Automated deploys

Push a tag. `.github/workflows/deploy.yml` builds both images, pushes them to
GHCR, copies the compose file to the VM, logs in to GHCR from the VM, pulls,
and rolls.

```bash
git tag v1.0.0
git push origin v1.0.0
```

Watch it: `gh run list` / `gh run watch`.

Manual trigger with an explicit tag: **Actions → Deploy → Run workflow**.

Rollback is automatic on a failed health check (it reverts to `latest`). To roll
back by hand:

```bash
ssh deploy@"$IP" 'cd ~/app && sed -i "s/^TAG=.*/TAG=v0.9.0/" .env && \
  docker compose --env-file .env -f deploy/docker-compose.prod.yml up -d'
```

## 6. HTTPS without a domain

You can encrypt the IP deployment with a locally-trusted certificate.

```bash
ssh deploy@"$IP"
cd ~/app
cp deploy/Caddyfile.tls deploy/Caddyfile
sed -i 's/^SITE_ADDRESS=.*/SITE_ADDRESS=:443/' .env
docker compose --env-file .env -f deploy/docker-compose.prod.yml up -d
```

Export the internal CA root and install it on any client that should trust it:

```bash
docker compose --env-file .env -f deploy/docker-compose.prod.yml exec caddy \
  wget -qO- https://caddy:8443/pki/ca.crt > caddy-root.crt
```

Browsers still warn until that root is in the OS trust store. This is
encryption, not verified identity — treat it as such.

### Buying a domain later

1. Point an `A` record for `app.example.com` at `$IP`.
2. `sed -i 's/^SITE_ADDRESS=.*/SITE_ADDRESS=https:\/\/app.example.com/' .env`
3. Restore the plain `Caddyfile` (the TLS one pins the internal CA, which must
   not be used once Let's Encrypt is available):
   ```bash
   cd ~/app
   git -C repo checkout deploy/Caddyfile && cp repo/deploy/Caddyfile deploy/Caddyfile
   ```
4. `docker compose --env-file .env -f deploy/docker-compose.prod.yml up -d`

Caddy provisions a real Let's Encrypt certificate automatically. Port 80 must
stay open for the HTTP-01 challenge.

## 7. Running the LLM on the VM

Template mode is the default and needs no model, but it only covers
task-manager CRUD. For real generation on the VM:

```bash
# Pull the model once (takes a few minutes).
docker run --rm -v ollama-models:/root/.ollama \
  ollama/ollama pull qwen2.5-coder:7b-instruct-q4_K_M
docker run --rm -v ollama-models:/root/.ollama \
  ollama/ollama pull qwen2.5-coder:3b
```

Then run Ollama as a sidecar rather than on the host:

```bash
printf 'OLLAMA_URL=http://ollama:11434\nSKIP_OLLAMA=0\n' >> .env
docker compose --env-file .env -f deploy/docker-compose.prod.yml --profile ollama up -d
```

The sidecar is on the internal `edge` network and publishes no ports. To use
host Ollama instead, keep the systemd service and point `OLLAMA_URL` at
`http://host.docker.internal:11434` (the compose file already maps that host
name).

CPU-only inference is slow. Measured on an e2-standard-4 (4 vCPU, no GPU):

| Model | Throughput | Wall clock for a 200-token reply |
|---|---|---|
| `qwen2.5-coder:3b` | ~15 tok/s | ~5 s |
| `qwen2.5-coder:7b-instruct-q4_K_M` | **5.6 tok/s** | ~15 s |

`agents/llm.py` defaults to a **90-second** per-call timeout. At 5.6 tok/s that
caps the 7B at roughly 500 tokens, while the Developer role asks for 4000. So on
CPU-only hardware the heavy roles will time out and silently fall back to
template mode — you get a green run that never used the model.

Check what actually happened:

```bash
tail -f ~/app/data/runs/llm_log.jsonl | jq -c '{role,model,latency}'
curl -s localhost/api/llm | jq
```

Ways to make the LLM path real on CPU:

1. **Use the 3B everywhere** — fastest and it fits the timeout:
   `OLLAMA_MODEL=qwen2.5-coder:3b`
2. **Route only the light roles locally** and the heavy ones to a hosted free
   tier (Gemini Flash is the least painful): put `GEMINI_API_KEY` in `.env` and
   let Ollama miss fall through to it.
3. **Raise the timeout** in `backend/agents/llm.py` (the `timeout` default on
   `generate()`) and on the `MAX_CONCURRENT_RUNS` front door, then expect a
   multi-minute run per attempt.

A CPU-only VM is a poor place to run the 7B. If this matters for the
demonstration, add a GPU VM (`g2-standard-8`, ~10 GB VRAM) — the 7B q4 then
runs at interactive speed and `OLLAMA_MODEL` needs no change.

## 8. Operating it

```bash
cd ~/app
C="docker compose --env-file .env -f deploy/docker-compose.prod.yml"

$C ps                      # health of each service
$C logs -f backend         # API logs
$C logs -f caddy           # access logs
$C top                     # live resource use
docker stats --no-stream   # one-shot snapshot
```

Run artifacts live in the `runs-data` volume, not on the host filesystem:

```bash
$C exec backend ls /data/runs
$C exec backend cat /data/runs/<run_id>/summary.json
```

Back them up by copying the volume:

```bash
docker run --rm -v multi-agent-team_runs-data:/src -v "$PWD:/dst" \
  alpine tar czf /dst/runs-backup.tar.gz -C /src .
```

### Restricting access

The app is currently open to the internet. To limit it to your IP:

```bash
gcloud compute firewall-rules delete allow-multi-agent-team
gcloud compute firewall-rules allow-multi-agent-team-https \
  --network=default --direction=INGRESS --action=ALLOW \
  --rules=tcp:443 --source-ranges=YOUR_IP/32 --target-tags=multi-agent-team
```

Keep 22 open to your IP or you will lock yourself out.

## 9. Known limitations

**The sandbox is not isolated in this topology.** `sandbox/runner.py` has a
Docker path (no network, 512 MB, 1 CPU) and a local-subprocess fallback. Inside
the backend container Docker is not available, so production runs the fallback:
pytest executing LLM-generated code as the `appuser` in the backend container,
with network access. That container can reach the host's Docker socket
boundaries? No — it has no socket mount, so it cannot escape to the host that
way. But it *can* make outbound network calls, and generated code runs
arbitrary Python.

Mitigations that matter, in order of value:

1. Do not expose the deployment publicly (step 8).
2. Move sandbox execution to a sibling container with its own network
   namespace. This is the real fix and the main reason to move off a single VM.
3. Until then, run the backend with egress restricted to the Ollama host, so
   generated code cannot reach the internet.

The backend container has no Docker socket mount, so it cannot use the daemon to
escape. But it does have outbound network access, and generated code runs
arbitrary Python inside it.

**No authentication.** Anyone who can reach the UI can start runs, which execute
generated code. Add auth before this faces the internet.

**In-memory run registry.** Run metadata lives in the process; only the artifact
directories survive a restart (`rehydrate()` picks them up). Fine for a single
user, wrong for more than one.

**CPU-only LLM.** See section 7. This is the practical reason template mode is
the default.

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `502` from Caddy | backend unhealthy | `$C logs backend`; check `/health` |
| Frontend crash-looping, `host not found in upstream "backend"` | frontend and backend on different networks | both must be on `edge`; confirm with `$C ps` |
| SSE hangs, no live updates | proxy buffering | `proxy_buffering off` in `frontend/nginx/default.conf`; `flush_interval -1` in Caddyfile |
| `429` on create | run concurrency limit | raise `MAX_CONCURRENT_RUNS` or wait |
| Runs stay `template` mode | Ollama unreachable from the container | `curl -s localhost/api/llm \| jq`; check `OLLAMA_URL` |
| Generated code times out | CPU inference too slow for the 90 s limit | raise the timeout, use the 3B model, or use a hosted tier |
| `permission denied` on Docker | user not in the `docker` group | `sudo usermod -aG docker $USER` then re-login |
| Caddy will not start after editing | invalid config | `docker compose ... run --rm caddy caddy validate --config /etc/caddy/Caddyfile` |