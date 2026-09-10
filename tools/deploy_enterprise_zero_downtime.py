from __future__ import annotations

import base64
import re
import shlex
import time
from pathlib import Path

import paramiko

from deploy_enterprise_update import ROOT, REMOTE_ROOT, load_connection, run, upload_file, upload_tree


NGINX_CONFIG = "/etc/nginx/conf.d/fbcy.conf"


def main() -> int:
    host, port, username, password = load_connection()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=port, username=username, password=password, timeout=20)
    sftp = client.open_sftp()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    release = f"{REMOTE_ROOT}/releases/{stamp}"

    current = run(client, f"grep -m1 'proxy_pass http://127.0.0.1:.*/admin' {NGINX_CONFIG}")
    match = re.search(r"127\.0\.0\.1:(\d+)/admin", current)
    if not match:
        raise RuntimeError("无法识别当前生产前端端口")
    active_web = int(match.group(1))
    if active_web not in {8300, 8301}:
        raise RuntimeError(f"未知生产前端端口: {active_web}")
    active_api = 8876 if active_web == 8300 else 8877
    target_web = 8301 if active_web == 8300 else 8300
    target_api = 8877 if active_api == 8876 else 8876

    active_container = run(
        client,
        f"docker ps --filter publish={active_api} --format '{{{{.Names}}}}' | head -1",
    ).strip()
    if not active_container or not re.fullmatch(r"[A-Za-z0-9_.-]+", active_container):
        raise RuntimeError("无法识别当前生产容器")
    image = run(
        client, f"docker inspect {shlex.quote(active_container)} --format '{{{{.Config.Image}}}}'"
    ).strip()
    if not image or not re.fullmatch(r"[A-Za-z0-9_./:@-]+", image):
        raise RuntimeError("无法识别生产镜像")

    upload_file(sftp, ROOT / "doubao_dashboard_server.py", f"{release}/doubao_dashboard_server.py")
    upload_file(sftp, ROOT / "monitor_core/remote_tasks.py", f"{release}/monitor_core/remote_tasks.py")
    upload_file(sftp, ROOT / "monitor_core/quality.py", f"{release}/monitor_core/quality.py")
    upload_tree(sftp, ROOT / "yuanbao_monitor/dashboard/dist", f"{release}/dashboard/dist")

    next_container = f"geo-panel-slot-{target_api}"
    env_file = f"/tmp/{next_container}-{stamp}.env"
    backup = f"{NGINX_CONFIG}.backup-{stamp}"
    switch_started = False
    run(client, f"docker rm -f {shlex.quote(next_container)} >/dev/null 2>&1 || true")
    run(
        client,
        f"docker inspect {shlex.quote(active_container)} --format "
        f"'{{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}' > {shlex.quote(env_file)} "
        f"&& chmod 600 {shlex.quote(env_file)}",
    )
    command = " ".join([
        "docker run -d",
        f"--name {shlex.quote(next_container)}",
        "--restart unless-stopped --cpus 1 --memory 640m",
        f"--env-file {shlex.quote(env_file)}",
        f"-p 127.0.0.1:{target_web}:3000 -p 127.0.0.1:{target_api}:8765",
        f"-v {REMOTE_ROOT}/deploy/hybrid/runtime:/app/runtime",
        f"-v {REMOTE_ROOT}/deploy/hybrid/entrypoint.sh:/app/entrypoint.sh:ro",
        f"-v {release}/doubao_dashboard_server.py:/app/doubao_dashboard_server.py:ro",
        f"-v {REMOTE_ROOT}/doubao_env_loader.py:/app/doubao_env_loader.py:ro",
        f"-v {release}/monitor_core/remote_tasks.py:/app/monitor_core/remote_tasks.py:ro",
        f"-v {release}/monitor_core/quality.py:/app/monitor_core/quality.py:ro",
        f"-v {REMOTE_ROOT}/model_plugins:/app/model_plugins:ro",
        f"-v {REMOTE_ROOT}/web_collectors/config.py:/app/web_collectors/config.py:ro",
        f"-v {release}/dashboard/dist:/app/yuanbao_monitor/dashboard/dist:ro",
        f"-v {REMOTE_ROOT}/yuanbao_monitor/dashboard/scripts/verify-production-assets.mjs:"
        "/app/yuanbao_monitor/dashboard/scripts/verify-production-assets.mjs:ro",
        "--entrypoint /usr/bin/tini",
        shlex.quote(image),
        "-- /bin/sh /app/entrypoint.sh",
    ])
    try:
        run(client, command)
        run(
            client,
            f"for i in $(seq 1 40); do "
            f"curl -fsS http://127.0.0.1:{target_api}/api/health >/dev/null && "
            f"curl -fsS http://127.0.0.1:{target_web}/admin >/dev/null && exit 0; "
            "sleep 2; done; exit 1",
        )

        switch_script = f'''from pathlib import Path
p=Path({NGINX_CONFIG!r})
s=p.read_text()
s=s.replace("127.0.0.1:{active_web}", "127.0.0.1:{target_web}")
s=s.replace("127.0.0.1:{active_api}", "127.0.0.1:{target_api}")
p.write_text(s)
'''
        encoded = base64.b64encode(switch_script.encode("utf-8")).decode("ascii")
        switch_started = True
        run(
            client,
            f"cp {NGINX_CONFIG} {shlex.quote(backup)} && "
            f"python3 -c \"import base64;exec(base64.b64decode('{encoded}'))\" && "
            "nginx -t && systemctl reload nginx",
        )
        run(client, "curl -fsS https://www.ifbcy.com/geo/api/health >/dev/null")

        # Update the canonical checkout only after traffic has moved away from
        # its bind mounts. The stopped prior container remains an immediate
        # rollback option until a later release takes the slot.
        upload_file(sftp, ROOT / "doubao_dashboard_server.py", f"{REMOTE_ROOT}/doubao_dashboard_server.py")
        upload_file(sftp, ROOT / "monitor_core/remote_tasks.py", f"{REMOTE_ROOT}/monitor_core/remote_tasks.py")
        upload_file(sftp, ROOT / "monitor_core/quality.py", f"{REMOTE_ROOT}/monitor_core/quality.py")
        upload_tree(sftp, ROOT / "yuanbao_monitor/dashboard/dist", f"{REMOTE_ROOT}/yuanbao_monitor/dashboard/dist")
        run(client, f"rm -f {shlex.quote(env_file)}")
        run(client, f"docker stop {shlex.quote(active_container)} >/dev/null")
    except Exception:
        if switch_started:
            try:
                run(
                    client,
                    f"test -f {shlex.quote(backup)} && cp {shlex.quote(backup)} {NGINX_CONFIG} "
                    "&& nginx -t && systemctl reload nginx",
                )
            except Exception:
                pass
        run(client, f"docker rm -f {shlex.quote(next_container)} >/dev/null 2>&1 || true; rm -f {shlex.quote(env_file)}")
        raise
    finally:
        sftp.close()
        client.close()

    print(f"Zero-downtime deployment completed on ports {target_web}/{target_api}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
