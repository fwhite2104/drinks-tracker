# Public deployment runbook — Cloudflare Tunnel (spec §7.2)

Operator runbook for exposing the API publicly from the Proxmox VM
(VM 104 "docker", `/opt/drinks-tracker`) via a Cloudflare Tunnel, per the
ticket-02 decision. Repo-side prep (this directory) is done; everything here
is the human VM/dashboard step that closes
`.scratch/mobile-app/issues/10-public-deployment-cloudflared.md`.

Placeholders used throughout: `<your-domain>`, `<TUNNEL-UUID>`.
No secrets or hostnames from this repo go into the tunnel config.

---

## 1. Stack up (existing compose, unchanged)

```sh
cd /opt/drinks-tracker
git pull
# .env must exist locally (never committed) — compose env_file requires it
docker compose up -d --build
curl -sf http://localhost:8000/health | python3 -m json.tool
```

The stack (collector cron, discovery cron, api) runs unchanged from the
existing `docker-compose.yml`. Verify the LAN endpoint before touching
Cloudflare.

## 2. Prerequisites

- A domain on Cloudflare DNS (ticket-02 prerequisite, ~€10/yr).
- Cloudflare account with Zero Trust enabled (free tier is sufficient).

## 3. cloudflared: install, tunnel create, DNS route

Option A (recommended — container on the compose network, so the service
name `api` in `config.yml` resolves):

```sh
# One-time: create the tunnel and get credentials
docker run -it --rm -v /etc/cloudflared:/etc/cloudflared \
  cloudflare/cloudflared:latest tunnel login
docker run -it --rm -v /etc/cloudflared:/etc/cloudflared \
  cloudflare/cloudflared:latest tunnel create drinks-tracker
# → credentials written to /etc/cloudflared/<TUNNEL-UUID>.json

# Config: copy the template and fill in <TUNNEL-UUID> and api.<your-domain>
cp deploy/config.yml.example /etc/cloudflared/config.yml
$EDITOR /etc/cloudflared/config.yml

# Route DNS: api.<your-domain> → tunnel (creates a CNAME)
docker run -it --rm -v /etc/cloudflared:/etc/cloudflared \
  cloudflare/cloudflared:latest tunnel route dns drinks-tracker api.<your-domain>

# Run persistently (restarts with the host)
docker run -d --name cloudflared --restart unless-stopped \
  --network drinks-tracker_default \
  -v /etc/cloudflared:/etc/cloudflared:ro \
  cloudflare/cloudflared:latest tunnel --config /etc/cloudflared/config.yml run
```

Option B (system service on the VM host): install cloudflared from
Cloudflare's apt repo, then `cloudflared service install`, and change the
ingress service URL to `http://localhost:8000` (port 8000 is already
published by compose).

Check tunnel status: Cloudflare dashboard → Zero Trust → Networks → Tunnels,
or `docker logs -f cloudflared`.

## 4. Cloudflare Access — operator paths

Zero Trust → Access → Applications → **Add → Self-hosted**, one app per
operator prefix (path matching is supported on self-hosted apps):

| Application | Domain | Policy |
|---|---|---|
| Feed API — operator runs | `api.<your-domain>/runs*` | Allow · Emails · <feilim> |
| Feed API — operator results | `api.<your-domain>/results*` | same |
| Feed API — discovery candidates | `api.<your-domain>/candidates*` | same |
| Feed API — coverage | `api.<your-domain>/coverage*` | same |
| Operator Dashboard (optional, only if `operator.<your-domain>` is enabled in the tunnel config) | `operator.<your-domain>/*` | same |

- Session duration: 24h is plenty for review sprints.
- Nothing to add for public routes — everything not covered by an Access app
  is anonymous (see `ingress.md` for the full split).
- Sanity check: `curl -s https://api.<your-domain>/runs` must return the
  Access login redirect (302/403), while `/health` returns 200 JSON.

## 5. Edge rate limiting (ticket-02: no auth v1, rate-limit at the tunnel)

Cloudflare dashboard → Security → WAF → Rate limiting rules (free tier
includes one rule):

- **Match**: hostname `api.<your-domain>` AND path not starting `/health`
  (keep health cheap for uptime checks).
- **Threshold**: e.g. 60 requests / 1 minute / per IP (mobile app polls
  rarely; 60/min is generous headroom).
- **Action**: Block (or Managed Challenge) for 1 minute.

Note: Cloudflare also applies free-tier DDoS protection automatically. If
abuse becomes a concern before auth lands, tighten the threshold or add a
challenge rule rather than app code (ticket-02 deferred-auth note).

## 6. Verify from outside the LAN

```sh
make deploy-check BASE_URL=https://api.<your-domain>
```

(`deploy/healthcheck.sh` — checks `/health` reports `status: ok` and
`/consumer/feed` returns valid JSON. Run it from a phone on mobile data or
any host off the home network; the point is to prove the tunnel path works,
not the LAN path.)

Done when: `deploy-check` passes off-LAN, `/runs` redirects to Access, and
the app's prod base URL (`https://api.<your-domain>`, injected via EAS per
ticket-02) returns the feed. Then resolve ticket 10.
