# Ubuntu VM Deployment

Current VM deployment target:

- Host: `ops-web-tools@192.168.0.184`
- App path: `/home/ops-web-tools/apps/opswat-diagram-api`
- Service: `opswat-diagram-api.service`
- Port: `8020`
- LAN helper URL: `http://192.168.0.184:8020/helper`
- LAN API docs: `http://192.168.0.184:8020/docs`
- Public helper URL: `https://diagrams.rndrlab.com/helper`
- Public API docs: `https://diagrams.rndrlab.com/docs`
- Cloudflare tunnel: `opswat-diagram-api`
- Cloudflare tunnel service: `cloudflared-opswat-diagram-api.service`

The VM `.env` is intentionally keyless. Users can provide their own OpenAI API key in the prompt helper when choosing GPT Image output.

## Service Management

```bash
ssh ops-web-tools@192.168.0.184
systemctl --user status opswat-diagram-api.service
systemctl --user restart opswat-diagram-api.service
journalctl --user -u opswat-diagram-api.service -f
systemctl --user status cloudflared-opswat-diagram-api.service
journalctl --user -u cloudflared-opswat-diagram-api.service -f
```

## Health Check

```bash
curl http://192.168.0.184:8020/api/health
curl https://diagrams.rndrlab.com/api/health
```

## Updating The VM

From the local repo:

```bash
rsync -az --exclude .git --exclude .venv --exclude .env --exclude __pycache__ ./ ops-web-tools@192.168.0.184:/home/ops-web-tools/apps/opswat-diagram-api/
ssh ops-web-tools@192.168.0.184 'cd /home/ops-web-tools/apps/opswat-diagram-api && python3 -m pip install --user --break-system-packages -r requirements.txt && systemctl --user restart opswat-diagram-api.service'
```

## Public Domain

The public route is served through a dedicated Cloudflare Tunnel:

- Hostname: `diagrams.rndrlab.com`
- Origin service: `http://127.0.0.1:8020`
- Tunnel config: `/home/ops-web-tools/.cloudflared/opswat-diagram-api.yml`

This route is separate from `pov.rndrlab.com` and `meetings.rndrlab.com`.
