from __future__ import annotations

import base64
import getpass
import hashlib
import os
from pathlib import Path
import re
import secrets
import shlex
import sys
import time

import paramiko


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
REMOTE_ROOT = "/opt/geo-monitor"


def load_connection() -> tuple[str, int, str, str]:
    text = (WORKSPACE / "vps_config.txt").read_text(encoding="utf-8-sig")
    ssh_line = next(line for line in text.splitlines() if line.strip().lower().startswith("ssh "))
    password_line = next(line for line in text.splitlines() if line.strip().lower().startswith("password"))
    match = re.search(r"ssh\s+([^@\s]+)@([^\s]+)(?:\s+-p\s+(\d+))?", ssh_line, re.I)
    if not match:
        raise RuntimeError("无法解析 vps_config.txt 中的 SSH 配置")
    password = password_line.split(":", 1)[1].strip()
    return match.group(2), int(match.group(3) or 22), match.group(1), password


def run(client: paramiko.SSHClient, command: str) -> str:
    _, stdout, stderr = client.exec_command(command, timeout=120)
    status = stdout.channel.recv_exit_status()
    output = stdout.read().decode("utf-8", "replace")
    error = stderr.read().decode("utf-8", "replace")
    if status:
        raise RuntimeError(f"远程命令失败({status}): {error.strip() or output.strip()}")
    return output


def mkdirs(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    parts = remote_dir.strip("/").split("/")
    current = ""
    for part in parts:
        current += "/" + part
        try:
            sftp.stat(current)
        except FileNotFoundError:
            sftp.mkdir(current)


def upload_file(sftp: paramiko.SFTPClient, local: Path, remote: str) -> None:
    mkdirs(sftp, remote.rsplit("/", 1)[0])
    sftp.put(str(local), remote)


def upload_tree(sftp: paramiko.SFTPClient, local_dir: Path, remote_dir: str) -> None:
    mkdirs(sftp, remote_dir)
    for path in local_dir.rglob("*"):
        relative = path.relative_to(local_dir).as_posix()
        remote = f"{remote_dir}/{relative}"
        if path.is_dir():
            mkdirs(sftp, remote)
        else:
            upload_file(sftp, path, remote)


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    iterations = 600_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join(
        [
            "pbkdf2_sha256",
            str(iterations),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        ]
    )


def update_env(sftp: paramiko.SFTPClient, admin_password: str) -> None:
    remote = f"{REMOTE_ROOT}/deploy/hybrid/server.env"
    with sftp.open(remote, "r") as handle:
        content = handle.read().decode("utf-8")
    values = {
        "MONITOR_ADMIN_USERNAME": "admin_jyzc",
        # Compose treats a single dollar sign in env files as interpolation.
        "MONITOR_ADMIN_PASSWORD_HASH": password_hash(admin_password).replace("$", "$$"),
        "MONITOR_ADMIN_SESSION_SECRET": secrets.token_urlsafe(48),
    }
    lines = content.splitlines()
    present: set[str] = set()
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in values:
            lines[index] = f"{key}={values[key]}"
            present.add(key)
    for key, value in values.items():
        if key not in present:
            lines.append(f"{key}={value}")
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    with sftp.open(remote, "w") as handle:
        handle.write(payload)
    sftp.chmod(remote, 0o600)


def main() -> int:
    preserve_admin = os.environ.get("GEO_PRESERVE_ADMIN_SECRETS", "") == "1"
    admin_password = "" if preserve_admin else getpass.getpass("管理员密码（不会回显或保存）: ")
    if not preserve_admin and not admin_password:
        print("未提供管理员密码", file=sys.stderr)
        return 2
    host, port, username, ssh_password = load_connection()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=port, username=username, password=ssh_password, timeout=20)
    sftp = client.open_sftp()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = f"{REMOTE_ROOT}/deploy/hybrid/runtime/deploy-backups/{stamp}"
    targets = [
        "doubao_dashboard_server.py",
        "monitor_core/remote_tasks.py",
        "model_plugins/quark/plugin.py",
        "model_plugins/kimi/plugin.py",
        "web_collectors/config.py",
        "deploy/hybrid/docker-compose.yml",
        "deploy/hybrid/server.env",
        "yuanbao_monitor/dashboard/dist",
    ]
    quoted = " ".join(shlex.quote(f"{REMOTE_ROOT}/{item}") for item in targets)
    run(
        client,
        f"mkdir -p {shlex.quote(backup)} && set -- {quoted}; "
        f"for p do [ ! -e \"$p\" ] || cp -a \"$p\" {shlex.quote(backup)}/; done",
    )

    upload_file(sftp, ROOT / "doubao_dashboard_server.py", f"{REMOTE_ROOT}/doubao_dashboard_server.py")
    upload_file(sftp, ROOT / "monitor_core/remote_tasks.py", f"{REMOTE_ROOT}/monitor_core/remote_tasks.py")
    upload_file(sftp, ROOT / "model_plugins/quark/plugin.py", f"{REMOTE_ROOT}/model_plugins/quark/plugin.py")
    upload_file(sftp, ROOT / "model_plugins/kimi/plugin.py", f"{REMOTE_ROOT}/model_plugins/kimi/plugin.py")
    upload_file(sftp, ROOT / "web_collectors/config.py", f"{REMOTE_ROOT}/web_collectors/config.py")
    upload_file(sftp, ROOT / "deploy/hybrid/docker-compose.yml", f"{REMOTE_ROOT}/deploy/hybrid/docker-compose.yml")
    upload_file(sftp, ROOT / "deploy/hybrid/server.env.example", f"{REMOTE_ROOT}/deploy/hybrid/server.env.example")
    upload_file(sftp, ROOT / "deploy/hybrid/README.md", f"{REMOTE_ROOT}/deploy/hybrid/README.md")
    upload_tree(sftp, ROOT / "yuanbao_monitor/dashboard/dist", f"{REMOTE_ROOT}/yuanbao_monitor/dashboard/dist")
    if not preserve_admin:
        update_env(sftp, admin_password)
    admin_password = ""
    sftp.close()

    run(client, f"cd {REMOTE_ROOT}/deploy/hybrid && docker compose up -d --force-recreate panel")
    health = run(client, "for i in $(seq 1 30); do curl -fsS http://127.0.0.1:8876/api/health && exit 0; sleep 2; done; exit 1")
    print(f"部署完成，备份目录: {backup}")
    print("服务健康检查通过" if '"ok"' in health else "服务已响应")
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
