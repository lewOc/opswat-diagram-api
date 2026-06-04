# Ubuntu VM Deployment

Current VM deployment target:

- Host: `ops-web-tools@192.168.0.184`
- App path: `/home/ops-web-tools/apps/opswat-diagram-api`
- Service: `opswat-diagram-api.service`
- Port: `8020`
- LAN helper URL: `http://192.168.0.184:8020/helper`
- LAN API docs: `http://192.168.0.184:8020/docs`

The VM `.env` is intentionally keyless. Users can provide their own OpenAI API key in the prompt helper when choosing GPT Image output.

## Service Management

```bash
ssh ops-web-tools@192.168.0.184
systemctl --user status opswat-diagram-api.service
systemctl --user restart opswat-diagram-api.service
journalctl --user -u opswat-diagram-api.service -f
```

## Health Check

```bash
curl http://192.168.0.184:8020/api/health
```

## Updating The VM

From the local repo:

```bash
rsync -az --exclude .git --exclude .venv --exclude .env --exclude __pycache__ ./ ops-web-tools@192.168.0.184:/home/ops-web-tools/apps/opswat-diagram-api/
ssh ops-web-tools@192.168.0.184 'cd /home/ops-web-tools/apps/opswat-diagram-api && python3 -m pip install --user --break-system-packages -r requirements.txt && systemctl --user restart opswat-diagram-api.service'
```

## Public Domain

Because `192.168.0.184` is a private LAN address, public DNS for `rndrlab.com` cannot point directly at it. Use one of these approaches:

- Recommended: create a new Cloudflare Tunnel route, for example `diagrams.rndrlab.com`, pointing to `http://127.0.0.1:8020` on the VM.
- Alternative: set up router port forwarding to the VM and point DNS to the public WAN IP.

Do not reuse or modify the existing `pov.rndrlab.com` or `meetings.rndrlab.com` routes for this service.
