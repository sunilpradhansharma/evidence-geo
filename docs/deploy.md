# Deployment

Simple, low-tech deploy of the Evidence Monitoring Agent to a single EC2 box.

## How it works

```text
Bitbucket Pipelines (on push to main)
  1. tests  -> backend import + pytest, frontend typecheck + build
  2. rsync  -> copies the working tree to ~/evidence-monitoring-agent/ on EC2
  3. ssh    -> runs scripts/ec2_deploy.sh on the box
                 -> docker build (one image: nginx + uvicorn)
                 -> docker run  (replaces the running container, port 80)
                 -> seeds the question bank (idempotent)
  4. smoke  -> curl http://EC2_HOST/healthz until it returns {"status":"ok"}
```

One Docker image runs everything on **port 80**:

- **nginx** serves the built React SPA and reverse-proxies `/api/*` to the
  backend (stripping the `/api` prefix, exactly like the Vite dev proxy).
- **uvicorn** runs the FastAPI app on `127.0.0.1:8000`.
- **supervisord** keeps both alive.

No container registry, no docker compose, no Kubernetes. The image is built
on the EC2 host straight from the rsynced working tree.

## What goes where

| Path | Purpose |
|------|---------|
| `Dockerfile` | Builds the single nginx + uvicorn image. |
| `deploy/nginx.conf` | Static SPA + `/api/` reverse proxy + `/healthz`. |
| `deploy/supervisord.conf` | Runs uvicorn and nginx together. |
| `deploy/.env.production.example` | Template for the server-side `.env`. |
| `scripts/ec2_deploy.sh` | Runs on EC2: build image, swap container, seed DB. |
| `bitbucket-pipelines.yml` | CI/CD: test on PRs, test + deploy on `main`. |

The SQLite DB lives on the host at `~/evidence-monitoring-agent/data/` (mounted
to `/app/data` in the container) so it **survives redeploys**. The `.env` lives
only on the host and is never committed or rsynced.

---

## One-time setup

### 1. Required Bitbucket repository variables

Repository settings -> **Repository variables** (mark all as *Secured*):

| Variable | Value |
|----------|-------|
| `EC2_HOST` | Public DNS or IP, e.g. `ec2-3-12-36-59.us-east-2.compute.amazonaws.com` |
| `EC2_USER` | SSH user. Ubuntu AMI: `ubuntu`. Amazon Linux: `ec2-user`. |
| `EC2_SSH_KEY` | **Base64-encoded** private key (see below). |

Base64-encode the `.pem` so the multi-line key survives as a single variable:

```bash
# Linux
base64 -w0 your-key.pem
# macOS
base64 -i your-key.pem
```

Paste the resulting single line as the value of `EC2_SSH_KEY`.

> The Atlassian `rsync-deploy` / `ssh-run` pipes decode this automatically.
> Alternatively you can configure an SSH key under Repository settings ->
> **SSH keys** and drop the `SSH_KEY` variable from the pipes.

### 2. Prepare the EC2 instance

SSH into the box and install Docker (one time).

**Ubuntu:**

```bash
sudo apt-get update
sudo apt-get install -y docker.io
sudo usermod -aG docker "$USER"     # then log out / back in
```

**Amazon Linux 2/2023:**

```bash
sudo yum install -y docker
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"     # then log out / back in
```

Verify: `docker ps` should work without `sudo`.

### 3. Open the right ports (Security Group)

- **22 (SSH)** inbound — so the pipeline can rsync/ssh. For a POC you can use
  `0.0.0.0/0`; tighter is to allow the
  [Bitbucket Pipelines IP ranges](https://support.atlassian.com/bitbucket-cloud/docs/what-are-the-bitbucket-cloud-ip-addresses-i-should-use-to-configure-my-corporate-firewall/).
- **80 (HTTP)** inbound — so the app is reachable.

### 4. Create the server-side `.env`

The app needs AWS credentials for Bedrock. Create the env file on the host:

```bash
mkdir -p ~/evidence-monitoring-agent
# paste deploy/.env.production.example contents and fill in real values:
nano ~/evidence-monitoring-agent/.env
```

Keep `DATABASE_URL=sqlite+aiosqlite:////app/data/evidence_monitoring.db`
(four slashes = absolute path) so the DB lands on the mounted volume.

### 5. Make sure the SSH public key is authorized

The public key matching `EC2_SSH_KEY` must be in
`~/.ssh/authorized_keys` for `EC2_USER` on the box (the key you launched the
instance with usually already is).

---

## Deploying

- **Automatic:** push/merge to `main`. Tests run, then the deploy job runs.
- **PRs:** open a pull request — tests run, **no deploy**.
- **Manual:** Pipelines -> **Run pipeline** -> branch `main` -> custom
  pipeline **`deploy-main`**.

The deploy job fails loudly if `http://EC2_HOST/healthz` doesn't return
`{"status":"ok"}` within ~50s.

---

## Operating the box

```bash
# what's running
docker ps

# tail logs (nginx + uvicorn are both here)
docker logs -f evidence-monitoring-agent

# manual redeploy from the working tree already on the box
bash ~/evidence-monitoring-agent/scripts/ec2_deploy.sh

# restart without rebuilding
docker restart evidence-monitoring-agent
```

The container restarts automatically on reboot (`--restart unless-stopped`).

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| Pipeline: `Permission denied (publickey)` | `EC2_SSH_KEY` wrong/not base64, or public key not in `authorized_keys`. |
| Pipeline: rsync/ssh times out | Security Group not allowing port 22 from Bitbucket. |
| `ERROR: env file not found` in deploy log | Create `~/evidence-monitoring-agent/.env` (step 4). |
| `/healthz` never OK | `docker logs evidence-monitoring-agent` — usually bad AWS creds or a backend import error. |
| App loads but API calls 502 | uvicorn crashed; check `docker logs`. nginx proxies `/api/` -> `127.0.0.1:8000`. |
| Empty dashboard / no questions | Seed didn't run; `docker exec evidence-monitoring-agent python -m scripts.seed_questions`. |
| Port 80 already in use | Another web server on the box; stop it or set `HOST_PORT` env when running `ec2_deploy.sh`. |
| Build: `No space left on device` (`[Errno 28]`) during `pip install` | EC2 disk full from accumulated Docker images and build cache. `ec2_deploy.sh` now prunes before each build, so a re-run should self-heal. To unblock immediately, SSH to the box and run `docker system prune -af && docker builder prune -af`. If it keeps recurring, grow the EBS volume (`df -h /` to confirm). |
