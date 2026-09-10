from __future__ import annotations

import base64
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

import paramiko

from deploy_enterprise_update import load_connection, run, upload_tree


ROOT = Path(__file__).resolve().parents[1]
CASE_ROOT = ROOT.parent / "anli"
DIST = CASE_ROOT / "dist"
REMOTE_BASE = "/opt/geo-monitor/cases"
NGINX_CONFIG = "/etc/nginx/conf.d/fbcy.conf"


def main() -> int:
    build_environment = os.environ.copy()
    build_environment["VITE_BASE_PATH"] = "/geo/bydz/"
    npm_command = "npm.cmd" if os.name == "nt" else "npm"
    subprocess.run(
        [npm_command, "run", "build"],
        cwd=CASE_ROOT,
        env=build_environment,
        check=True,
    )
    if (not (DIST / "index.html").is_file() or not (DIST / "assets").is_dir()
            or not (DIST / "og.png").is_file()):
        raise RuntimeError("百一电子案例面板尚未完成生产构建")
    index_html = (DIST / "index.html").read_text(encoding="utf-8")
    if 'src="/geo/bydz/assets/' not in index_html or 'href="/geo/bydz/assets/' not in index_html:
        raise RuntimeError("案例页静态资源前缀错误，已阻止发布")

    host, port, username, password = load_connection()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=port, username=username, password=password, timeout=20)
    sftp = client.open_sftp()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    release = f"{REMOTE_BASE}/releases/bydz-{stamp}"
    current = f"{REMOTE_BASE}/bydz-current"
    backup = f"{NGINX_CONFIG}.bydz-{stamp}"
    previous = run(client, f"readlink -f {shlex.quote(current)} 2>/dev/null || true").strip()
    try:
        upload_tree(sftp, DIST, release)
        run(client, f"test -s {shlex.quote(release + '/index.html')}")
        run(client, f"test -s {shlex.quote(release + '/og.png')}")
        run(client, f"mkdir -p {shlex.quote(REMOTE_BASE)} && ln -sfn {shlex.quote(release)} {shlex.quote(current)}")

        block = f'''    # BYDZ_CASE_BEGIN
    location = /geo/bydz {{
        return 301 /geo/bydz/;
    }}

    location = /geo/bydz/ {{
        rewrite ^ /geo/bydz/index.html last;
    }}

    location = /geo/bydz/index.html {{
        alias {current}/index.html;
        add_header Cache-Control "no-cache";
    }}

    location ^~ /geo/bydz/assets/ {{
        alias {current}/assets/;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }}

    location = /geo/bydz/og.png {{
        alias {current}/og.png;
        expires 7d;
        add_header Cache-Control "public, max-age=604800";
    }}
    # BYDZ_CASE_END
'''
        patch = f'''from pathlib import Path
import re
p=Path({NGINX_CONFIG!r})
s=p.read_text()
block={block!r}
pattern=r"[ \\t]*# BYDZ_CASE_BEGIN.*?# BYDZ_CASE_END\\n?"
if re.search(pattern,s,flags=re.S):
    s=re.sub(pattern,block,s,count=1,flags=re.S)
else:
    marker="    # GEO_MONITOR_BEGIN\\n"
    if marker not in s:
        raise RuntimeError("GEO Nginx marker missing")
    s=s.replace(marker,marker+block+"\\n",1)
p.write_text(s)
'''
        encoded = base64.b64encode(patch.encode("utf-8")).decode("ascii")
        run(
            client,
            f"cp {shlex.quote(NGINX_CONFIG)} {shlex.quote(backup)} && "
            f"python3 -c \"import base64;exec(base64.b64decode('{encoded}'))\" && "
            "nginx -t && systemctl reload nginx && sleep 2",
        )
        try:
            run(
                client,
                "curl -kfsS --resolve www.ifbcy.com:443:127.0.0.1 "
                "https://www.ifbcy.com/geo/bydz/ >/dev/null",
            )
        except Exception as exc:
            diagnostic = run(
                client,
                "sed -n '/BYDZ_CASE_BEGIN/,/BYDZ_CASE_END/p' "
                f"{shlex.quote(NGINX_CONFIG)}; tail -n 3 /var/log/nginx/fbcy_error.log",
            )
            run(
                client,
                f"cp {shlex.quote(backup)} {shlex.quote(NGINX_CONFIG)} && "
                "nginx -t && systemctl reload nginx && sleep 2",
            )
            raise RuntimeError(f"案例页发布自检失败，已回滚：\n{diagnostic}") from exc
    except Exception:
        if previous and re.fullmatch(r"/opt/geo-monitor/cases/releases/bydz-[0-9-]+", previous):
            try:
                run(client, f"ln -sfn {shlex.quote(previous)} {shlex.quote(current)}")
            except Exception:
                pass
        raise
    finally:
        sftp.close()
        client.close()

    print("Published https://www.ifbcy.com/geo/bydz/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
