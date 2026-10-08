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
printf 'REGISTRY=ghcr.io/OWNER\nTAG=latest\nSITE_ADDRESS=:80\n' > .env
docker compose --env-file .env -f deploy/docker-compose.prod.yml up -d --build
docker compose --env-file .env -f deploy/docker-compose.prod.yml ps
curl -s localhost/health | jq
```

The compose file **requires** `AUTH_SECRET` and `AUTH_PASSWORD_HASH`; it refuses
to start without them (see section 4a). Expect three services: `caddy`,
`frontend`, `backend` (healthy). Then browse `http://$IP` and sign in.

## 4a. Authentication

The UI and every `/api/*` route require a session. There is no anonymous access
to runs, and `/health` plus `/api/auth/*` are the only public paths — the
healthcheck and uptime probes keep working.

### Generate credentials

The scripted path, which is what you want almost always:

```bash
./deploy/bootstrap-env.sh     # writes .env, prints the password once
```

Or by hand:

```bash
python backend/scripts/gen_auth.py            # random password
python backend/scripts/gen_auth.py 'my-pass'  # or a chosen one
```

It prints an scrypt hash, a signing secret, and an API key. Append to the VM's
`.env`:

```
AUTH_USERNAME=admin
AUTH_SECRET=<64-char random string>
AUTH_PASSWORD_HASH=scrypt.16384.8.1.<salt>.<digest>
AUTH_API_KEY=mat_<random>
AUTH_COOKIE_SECURE=0
AUTH_SESSION_HOURS=12
```

`bootstrap-env.sh` is idempotent: it preserves existing values and fills in only
what is missing. To change a lost password:

```bash
./deploy/bootstrap-env.sh --reset-password 'new-password'
docker compose --env-file .env -f deploy/docker-compose.prod.yml up -d
```

> **The hash separator is a dot, not a `$`.** This is deliberate: Docker Compose
> treats `$` in an interpolated value as a variable reference, so a
> `$`-delimited hash arrives corrupted and every login fails. `python-dotenv` is
> not involved — this is Compose's own interpolation of the `environment:` block.
> There is a regression test (`test_password_hash_survives_compose_interpolation`)
> pinning the format.

Never commit `.env`; it is gitignored. If you rotate `AUTH_PASSWORD_HASH` or
`AUTH_SECRET`, all existing sessions stop working immediately.

### How it works

- Passwords: `hashlib.scrypt` (stdlib, memory-hard) with a per-password random
  salt, verified with `hmac.compare_digest`.
- Sessions: stateless signed cookies (`itsdangerous`), `HttpOnly` +
  `SameSite=strict`, 12-hour expiry. There is no server-side session store, so
  **logout only clears the browser's cookie** — a stolen cookie remains valid
  until it expires. Rotating `AUTH_SECRET` revokes all of them at once.
- CSRF: double-submit token. The SPA reads the non-`HttpOnly` `mat_csrf` cookie
  and echoes it in `X-CSRF-Token` on every write. Enforced centrally in
  middleware, so a route added later is covered automatically.
- Login throttling: 5 failures per client IP in 5 minutes → 15-minute block.

### Headless access

```bash
curl -H "X-API-Key: $AUTH_API_KEY" http://$IP/api/runs
curl -H "X-API-Key: $AUTH_API_KEY" -X POST http://$IP/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"requirement":"Build a task manager REST API with CRUD /tasks"}'
```

API-key callers skip the CSRF check, since they are not browsers.

### Verify it is actually enforced

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://$IP/api/runs        # 401
curl -s -o /dev/null -w '%{http_code}\n' http://$IP/health          # 200
curl -s -c /tmp/j -X POST http://$IP/api/auth/login \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$AUTH_USERNAME\",\"password\":\"$PASS\"}"
curl -s -b /tmp/j -o /dev/null -w '%{http_code}\n' http://$IP/api/runs  # 200
```

### Serving over plain HTTP

Session cookies are set with `Secure` off by default because the initial
deployment is `http://$IP`. That means the password and cookie travel in
cleartext on the wire. Once TLS is up (section 6), set
`AUTH_COOKIE_SECURE=1` and recreate the container, or the browser will refuse to
store the session.

Do not leave a public deployment on plain HTTP. Restrict the firewall to your
IP (section 8) until TLS is in place.

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

Pick a provider with `LLM_PROVIDER` in `.env`:

| Value | What it does | Cost of a run on a small VM |
|---|---|---|
| `gemini` | calls the Gemini API with `GEMINI_API_KEY` | ~1-2 min, free tier rate-limits hard |
| `local`  | runs Ollama on the VM | ~3-6 min on 2-4 vCPUs |

`gemini` is the practical choice on a small VM; a 2-vCPU VM cannot generate a
full app locally in reasonable time. There is no template fallback any more — a
run that cannot reach a backend ends `blocked`, which is intentional.

For local generation on the VM:

```bash
# Pull the model once (takes a few minutes).
docker run --rm -v ollama-models:/root/.ollama \
  ollama/ollama pull qwen2.5-coder:7b-instruct-q4_K_M
docker run --rm -v ollama-models:/root/.ollama \
  ollama/ollama pull qwen2.5-coder:3b
```

Then run Ollama as a sidecar rather than on the host:

```bash
printf 'LLM_PROVIDER=local\nOLLAMA_URL=http://ollama:11434\nSKIP_OLLAMA=0\n' >> .env
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

Local calls default to a **600-second** per-call budget
(`OLLAMA_TIMEOUT`, or `LLM_TIMEOUT` which overrides it for every backend). This
matters: at ~6 tok/s a 4000-token reply needs ~11 minutes, so a short timeout
truncates the answer mid-file and the parser then reports a malformed generation.
Per-role token ceilings (`OLLAMA_NUM_PREDICT`, or `OLLAMA_MODEL_*` plus
`role_num_predict`) also have to be large enough for a multi-file answer.

If a local run reports `model=none` with failures in the log, the model was not
reachable or the call was cut short; check `curl -s localhost/api/llm | jq`.

Set `LLM_PROVIDER=gemini` with a `GEMINI_API_KEY` and the agents route there.
Three operational caveats, all hit during development:

* **Model ids churn.** `gemini-2.0-flash` and `gemini-2.5-flash` both now return
  404 with "no longer available to new users"; `gemini-3.8-flash` intermittently
  returns 503 under load. `GEMINI_MODELS` is an ordered comma-separated list
  (default `gemini-3-flash-preview,gemini-flash-latest,gemini-3.8-flash`) so one
  bad id does not fail the run. `GEMINI_MODEL` pins a single id, which disables
  the list — usually a mistake.
* **Check the ids before a run.** `python backend/evals/check_gemini.py` probes
  each id in about two seconds and prints which ones answer. Note the
  distinction: **404 means the id is retired**, **429 means the quota is
  exhausted and resets daily**.
* **`GEMINI_MAX_OUTPUT_TOKENS` must be generous** (default 8192). The flash
  models spend output tokens on reasoning before answering; a small cap returns
  empty content with `finishReason=MAX_TOKENS`, which is indistinguishable from
  a broken model.

**The free tier rate-limits hard.** Once the quota is gone every id returns 429
and the run ends `blocked` — it does not degrade to a fake green result.
Transient 429/503 responses are retried with exponential backoff
(`LLM_RETRY_MAX`, `LLM_RETRY_BACKOFF`) before the run is abandoned; timeouts are
deliberately *not* retried, because three 120s waits would become a six-minute
stall. Check `/api/llm` or `llm_log.jsonl` to see which failures occurred.

Check what actually happened:

```bash
# The call log lives in the runs volume, not under /app.
tail -f ~/app/data/runs/llm_log.jsonl | jq -c '{role,model,latency,failures}'
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

**Authentication is a single shared password, not per-user accounts.** One
operator account plus an API key. Fine for a personal tool; if several people
need access, add real user records and per-run ownership first.

**Sessions are stateless.** Logout clears the local cookie; a copied cookie
stays valid for up to `AUTH_SESSION_HOURS`. Rotate `AUTH_SECRET` to revoke
everything immediately.

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

**The generated-code sandbox still runs with the API's privileges.** Auth means
only an authenticated operator can start a run; it does not sandbox the code that
run executes. See above.

**In-memory run registry.** Run metadata lives in the process; only the artifact
directories survive a restart (`rehydrate()` picks them up). Fine for a single
user, wrong for more than one.

**CPU-only LLM.** See section 7. This is the practical reason
`LLM_PROVIDER=gemini` is the default: a local model cannot generate a full app
quickly enough to be useful on a small VM, and there is no template fallback to
paper over it.

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `502` from Caddy | backend unhealthy | `$C logs backend`; check `/health` |
| Frontend crash-looping, `host not found in upstream "backend"` | frontend and backend on different networks | both must be on `edge`; confirm with `$C ps` |
| SSE hangs, no live updates | proxy buffering | `proxy_buffering off` in `frontend/nginx/default.conf`; `flush_interval -1` in Caddyfile |
| `429` on create | run concurrency limit | raise `MAX_CONCURRENT_RUNS` or wait |
| Runs end `blocked` | no backend reachable | `curl -s localhost/api/llm \| jq`; the error is in `run.error` and the log |
| Runs end `blocked`, `429` in log | Gemini free quota exhausted | wait for the daily reset, or use `LLM_PROVIDER=local` |
| Runs end `blocked`, `404` in log | retired Gemini model id | `python backend/evals/check_gemini.py`, then update `GEMINI_MODELS` |
| Generation looks truncated | token ceiling too low | raise `OLLAMA_NUM_PREDICT` / `GEMINI_MAX_OUTPUT_TOKENS` |
| Local generation times out | CPU inference slower than the budget | raise `OLLAMA_TIMEOUT`, use the 3b model, or switch to Gemini |
| `permission denied` on Docker | user not in the `docker` group | `sudo usermod -aG docker $USER` then re-login |
| Caddy will not start after editing | invalid config | `docker compose ... run --rm caddy caddy validate --config /etc/caddy/Caddyfile` |