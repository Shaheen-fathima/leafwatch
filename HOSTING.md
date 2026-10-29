# Running LeafWatch continuously (what "always online" really needs)

A laptop running `run.bat` is a **demo**. It is online only while the PC is on, awake, connected, and the window is open; its address
(http://192.168.x.x:8000) works only on the same Wi-Fi. Genuine 24x7 hosting needs all of the following:

| Need | Why | Typical choice |
|---|---|---|
| An always-on machine | the process must run when your PC is off | cloud VM / VPS (always free tiers sleep), or PaaS container |
| A public address + HTTPS | phones need a URL; browsers block GPS and the admin key would travel in clear text over http | domain + Caddy / nginx / cloud load balancer with a TLS certificate |
| A process supervisor | restart after crash/reboot | systemd, Docker `restart: always`, Windows service (NSSM) |
| Secrets in the environment | `LEAFWATCH_ADMIN_KEY` must not be in code or chat | host's secret store / env file with restricted permissions |
| A real database | SQLite + in-process WebSocket = ONE process; two workers would not share sockets or locks | PostgreSQL (+ PostGIS for distance), Redis pub/sub for websocket fan-out |
| Persistent storage + backups | uploads and DB must survive redeploys and disk loss | volume or object storage; scheduled DB backups, tested restores |
| Farmer authentication | today a farmer is just an id: anyone who knows it can read that farmer's alerts | phone OTP + session tokens; per-farmer websocket tokens |
| Notifications that reach closed apps | WebSocket/toast only works while the page is open | SMS / WhatsApp Business API / web push / FCM |
| Rate limiting and abuse control | spam uploads, brute-force | reverse-proxy limits, per-farmer quotas (an in-process admin lock-out exists) |
| Model serving capacity | PyTorch inference on CPU takes CPU/RAM per photo | right-sized VM (or GPU), request queue |
| Monitoring | know when it is down or alerts fail | uptime check on `/api/stats`, log shipping, error alerts |
| Privacy/legal | stores farmer locations and photos | consent text, retention policy, data-protection compliance for your country |

None of these are implemented in this ZIP; `uvicorn` on one PC is what it provides.
