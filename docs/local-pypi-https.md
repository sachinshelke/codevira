# Local PyPI with HTTPS — for private-registry testing and IDE integration

This guide sets up a **local HTTPS-secured PyPI registry** using Docker, so
you can:

- Smoke-test a release candidate by uploading to a private registry and
  reinstalling `pipx install codevira` from it (exactly the flow a real
  user will hit, just scoped to your machine).
- Serve `codevira` packages to team machines or CI over HTTPS.
- Test HTTPS-aware install flows before pushing to public PyPI.

The stack is two containers on a shared docker network:

```
┌──────────────────────────┐      ┌────────────────────────────┐
│  nginx:alpine            │      │  pypiserver/pypiserver     │
│  :8443  (HTTPS, self-sig)│ ───▶ │  :8080  (plain HTTP)       │
│  reverse proxy           │      │  htpasswd auth on uploads  │
└──────────────────────────┘      └────────────────────────────┘
```

The self-signed cert is fine for local testing. For LAN/team use, replace
it with a real cert from your own CA or Let's Encrypt via DNS-01.

---

## One-time setup

```bash
BASE=~/.codevira-local-pypi
mkdir -p "$BASE/packages" "$BASE/certs"

# 1. Create test credentials (replace 'testuser/testpass' for real use)
htpasswd -cb "$BASE/htpasswd" testuser testpass

# 2. Generate a self-signed cert for localhost
openssl req -x509 -newkey rsa:4096 -nodes \
  -keyout "$BASE/certs/key.pem" -out "$BASE/certs/cert.pem" \
  -days 365 \
  -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"

# 3. Write an nginx config that reverse-proxies HTTPS:8443 → pypi:8080
cat > "$BASE/nginx.conf" <<'CONF'
events { worker_connections 1024; }
http {
  server {
    listen 443 ssl;
    server_name localhost;
    ssl_certificate     /etc/nginx/certs/cert.pem;
    ssl_certificate_key /etc/nginx/certs/key.pem;
    client_max_body_size 50M;

    location / {
      proxy_pass http://pypi:8080;
      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto https;
    }
  }
}
CONF

# 4. Create a shared network so nginx can resolve 'pypi' by name
docker network create codevira-pypi-net
```

## Start the stack

```bash
BASE=~/.codevira-local-pypi

# pypiserver (name it 'pypi' so nginx can resolve it)
docker run -d \
  --name pypi \
  --network codevira-pypi-net \
  -p 8080:8080 \
  -v "$BASE/packages:/data/packages" \
  -v "$BASE/htpasswd:/data/.htpasswd:ro" \
  pypiserver/pypiserver:latest \
  run \
    --passwords /data/.htpasswd \
    --authenticate update \
    --overwrite \
    /data/packages

# nginx HTTPS reverse proxy on 8443
docker run -d \
  --name codevira-pypi-nginx \
  --network codevira-pypi-net \
  -p 8443:443 \
  -v "$BASE/certs:/etc/nginx/certs:ro" \
  -v "$BASE/nginx.conf:/etc/nginx/nginx.conf:ro" \
  nginx:alpine

# Health check (the -k is OK — we know the cert is self-signed)
curl -sk https://localhost:8443/ | head -3
```

## Publish a release to your local registry

```bash
cd /path/to/agent-mcp

# 1. Build the distribution artifacts
rm -rf dist/ build/ *.egg-info
python3 -m build

# 2. Upload via HTTPS
TWINE_USERNAME=testuser TWINE_PASSWORD=testpass twine upload \
  --repository-url https://localhost:8443 \
  --cert ~/.codevira-local-pypi/certs/cert.pem \
  dist/codevira-1.8.0-py3-none-any.whl \
  dist/codevira-1.8.0.tar.gz
```

## Install from the local registry

```bash
pipx uninstall codevira  # remove any previous install

pipx install codevira==1.8.0 \
  --python python3.11 \
  --index-url "https://testuser:testpass@localhost:8443/simple/" \
  --pip-args="--trusted-host localhost --cert $HOME/.codevira-local-pypi/certs/cert.pem"

# Verify
codevira --help
python3 -c "import mcp_server; print(mcp_server.__version__)"
```

## Cleanup

Stop and remove containers + network when done:

```bash
docker rm -f codevira-pypi-nginx pypi
docker network rm codevira-pypi-net
# Optional — delete everything:
rm -rf ~/.codevira-local-pypi
```

---

## Common gotchas

- **`--skip-existing` not supported.** `pypiserver` doesn't implement this
  twine flag. Either delete the old wheel from `~/.codevira-local-pypi/
  packages/` or set `--overwrite` at container start (as above) so
  re-uploads of the same version replace the existing file.

- **Self-signed cert trust.** Every client that hits the registry needs to
  trust `~/.codevira-local-pypi/certs/cert.pem`. `twine` takes
  `--cert <path>`, `pip` takes `--cert <path>` plus `--trusted-host
  localhost`. For system-wide trust on macOS you can add it to the
  Keychain, but for per-command flags it's simpler to pass `--cert` each
  time.

- **Port 8443 already in use.** Change the host port in the `-p
  8443:443` mapping. The container-side port stays `443` (inside nginx).

- **Adding `codevira` to an IDE/app that requires HTTPS.** Most MCP clients
  (Claude Code, Cursor, Windsurf, Antigravity) use **stdio transport by
  default**, not HTTPS — stdio needs no URL at all, just the `codevira`
  binary path. `codevira register` sets this up for you. HTTPS transport
  is only needed for the preview `codevira serve --https` multi-project
  server mode (see v1.7 HTTPS-preview docs). If your client is asking for
  an HTTPS URL, that's the serve path — different from this local-PyPI
  guide, which covers **package distribution**.

---

## Why HTTP isn't enough for team/CI use

Local HTTP works fine for solo smoke-testing on a single machine, but:

- `pip` in modern versions warns on `--index-url http://...` and some
  environments block plain-HTTP index URLs entirely.
- When team members on other machines `pipx install codevira` from your
  registry, they expect HTTPS.
- Corporate proxies often block plain-HTTP traffic.

The self-signed-cert setup above is a one-command step up from HTTP and
gives you a realistic distribution flow without touching public PyPI.
