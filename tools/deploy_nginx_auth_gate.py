from __future__ import annotations

import time

import paramiko

from deploy_enterprise_update import load_connection, run


CONFIG = "/etc/nginx/conf.d/fbcy.conf"
BEGIN = "    # GEO_MONITOR_BEGIN"
END = "    # GEO_MONITOR_END"
BLOCK = r'''    # GEO_MONITOR_BEGIN
    location = /geo {
        return 301 /geo/;
    }

    # Login shell and its static assets must remain reachable before a session exists.
    location ^~ /geo/admin {
        proxy_pass http://127.0.0.1:8300/admin;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    location ^~ /geo/assets/ {
        proxy_pass http://127.0.0.1:8300/assets/;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    location = /geo/quark-icon.svg {
        proxy_pass http://127.0.0.1:8300/quark-icon.svg;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    location ^~ /geo/api/ {
        proxy_pass http://127.0.0.1:8876/api/;
        proxy_set_header Host $host;
        # Preserve the browser route during auth_request subrequests so the
        # application can selectively admit completed shareable reports.
        proxy_set_header X-Original-URI $request_uri;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        client_max_body_size 10m;
    }

    location = /geo/tasks {
        auth_request /geo/api/admin/authorize;
        error_page 401 = @geo_login;
        proxy_pass http://127.0.0.1:8876/tasks;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    location @geo_login {
        return 302 /geo/admin?next=$request_uri;
    }

    location ^~ /geo/ {
        auth_request /geo/api/admin/authorize;
        error_page 401 = @geo_login;
        proxy_pass http://127.0.0.1:8300/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
    # GEO_MONITOR_END'''


def main() -> None:
    host, port, username, password = load_connection()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=port, username=username, password=password, timeout=20)
    sftp = client.open_sftp()
    with sftp.open(CONFIG, "r") as handle:
        content = handle.read().decode("utf-8")
    start = content.find(BEGIN)
    finish = content.find(END, start)
    if start < 0 or finish < 0:
        raise RuntimeError("未找到 GEO_MONITOR 配置标记")
    finish += len(END)
    updated = content[:start] + BLOCK + content[finish:]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = f"{CONFIG}.backup-{stamp}"
    run(client, f"cp -a {CONFIG} {backup}")
    temporary = f"{CONFIG}.codex-new"
    with sftp.open(temporary, "w") as handle:
        handle.write(updated.encode("utf-8"))
    sftp.chmod(temporary, 0o644)
    run(client, f"mv {temporary} {CONFIG}")
    try:
        run(client, "nginx -t")
        run(client, "systemctl reload nginx")
    except Exception:
        run(client, f"cp -a {backup} {CONFIG} && nginx -t && systemctl reload nginx")
        raise
    sftp.close()
    client.close()
    print(f"Nginx 登录硬门禁已部署，备份: {backup}")


if __name__ == "__main__":
    main()
