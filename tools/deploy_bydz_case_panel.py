from __future__ import annotations

import base64
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

import paramiko

from deploy_enterprise_update import load_connection, run, upload_tree


ROOT = Path(__file__).resolve().parents[1]
CASE_ROOT = ROOT.parent / "anli"
DIST_ROOT = CASE_ROOT / "dist"
REMOTE_BASE = "/opt/geo-monitor/cases"
NGINX_CONFIG = "/etc/nginx/conf.d/fbcy.conf"
CASES = {"bydz": "百一电子", "dhxs": "大海鲜生"}


def location_block(currents: dict[str, str]) -> str:
    sections: list[str] = []
    for slug, current in currents.items():
        sections.append(f'''    location = /geo/{slug} {{
        return 301 /geo/{slug}/;
    }}

    location = /geo/{slug}/ {{
        rewrite ^ /geo/{slug}/index.html last;
    }}

    location = /geo/{slug}/index.html {{
        alias {current}/index.html;
        add_header Cache-Control "no-cache";
    }}

    location ^~ /geo/{slug}/assets/ {{
        alias {current}/assets/;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }}

    location ^~ /geo/{slug}/platform-icons/ {{
        alias {current}/platform-icons/;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }}

    location = /geo/{slug}/favicon.svg {{
        alias {current}/favicon.svg;
        expires 7d;
    }}

    location = /geo/{slug}/og.png {{
        alias {current}/og.png;
        expires 7d;
        add_header Cache-Control "public, max-age=604800";
    }}''')
    return "    # BYDZ_CASE_BEGIN\n" + "\n\n".join(sections) + "\n    # BYDZ_CASE_END\n"


def main() -> int:
    npm_command = "npm.cmd" if os.name == "nt" else "npm"
    subprocess.run([npm_command, "run", "test"], cwd=CASE_ROOT, check=True)
    subprocess.run([npm_command, "run", "build:cases"], cwd=CASE_ROOT, check=True)

    for slug, label in CASES.items():
        dist = DIST_ROOT / slug
        if not (dist / "index.html").is_file() or not (dist / "assets").is_dir() or not (dist / "og.png").is_file():
            raise RuntimeError(f"{label}案例面板尚未完成生产构建")
        index_html = (dist / "index.html").read_text(encoding="utf-8")
        prefix = f'/geo/{slug}/assets/'
        if f'src="{prefix}' not in index_html or f'href="{prefix}' not in index_html:
            raise RuntimeError(f"{label}案例页静态资源前缀错误，已阻止发布")
        if label not in index_html:
            raise RuntimeError(f"{label}案例页元数据错误，已阻止发布")

    host, port, username, password = load_connection()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=port, username=username, password=password, timeout=20)
    sftp = client.open_sftp()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    releases = {slug: f"{REMOTE_BASE}/releases/{slug}-{stamp}" for slug in CASES}
    currents = {slug: f"{REMOTE_BASE}/{slug}-current" for slug in CASES}
    backup = f"{NGINX_CONFIG}.cases-{stamp}"
    previous = {
        slug: run(client, f"readlink -f {shlex.quote(current)} 2>/dev/null || true").strip()
        for slug, current in currents.items()
    }
    try:
        for slug in CASES:
            upload_tree(sftp, DIST_ROOT / slug, releases[slug])
            run(client, f"test -s {shlex.quote(releases[slug] + '/index.html')}")
            run(client, f"test -s {shlex.quote(releases[slug] + '/og.png')}")
            run(client, f"mkdir -p {shlex.quote(REMOTE_BASE)} && ln -sfn {shlex.quote(releases[slug])} {shlex.quote(currents[slug])}")

        block = location_block(currents)
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
            for slug, label in CASES.items():
                html = run(
                    client,
                    "curl -kfsS --resolve www.ifbcy.com:443:127.0.0.1 "
                    f"https://www.ifbcy.com/geo/{slug}/",
                )
                if label not in html:
                    raise RuntimeError(f"{label}页面内容自检失败")
                run(
                    client,
                    "curl -kfsS --resolve www.ifbcy.com:443:127.0.0.1 "
                    f"https://www.ifbcy.com/geo/{slug}/og.png >/dev/null",
                )
        except Exception as exc:
            diagnostic = run(
                client,
                "sed -n '/BYDZ_CASE_BEGIN/,/BYDZ_CASE_END/p' "
                f"{shlex.quote(NGINX_CONFIG)}; tail -n 3 /var/log/nginx/fbcy_error.log",
            )
            run(client, f"cp {shlex.quote(backup)} {shlex.quote(NGINX_CONFIG)} && nginx -t && systemctl reload nginx && sleep 2")
            raise RuntimeError(f"案例页发布自检失败，已回滚：\n{diagnostic}") from exc
    except Exception:
        for slug, old_target in previous.items():
            if old_target and re.fullmatch(rf"/opt/geo-monitor/cases/releases/{slug}-[0-9-]+", old_target):
                try:
                    run(client, f"ln -sfn {shlex.quote(old_target)} {shlex.quote(currents[slug])}")
                except Exception:
                    pass
        raise
    finally:
        sftp.close()
        client.close()

    for slug in CASES:
        print(f"Published https://www.ifbcy.com/geo/{slug}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
