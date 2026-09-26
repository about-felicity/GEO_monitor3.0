from __future__ import annotations

import base64
import json
import shlex
import sys
from pathlib import Path

import paramiko

from deploy_enterprise_update import load_connection, run


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    slug = sys.argv[1] if len(sys.argv) > 1 else "case-dhxs-20260918"
    output = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "runtime" / "case_dhxs_report.json"
    remote_script = f'''from monitor_core.remote_tasks import RemoteTaskQueue
import json
queue = RemoteTaskQueue("/app/runtime/remote_tasks.sqlite3")
print(json.dumps(queue.diagnosis_report({slug!r}, include_readiness=False), ensure_ascii=False, default=str))
'''
    encoded = base64.b64encode(remote_script.encode("utf-8")).decode("ascii")
    command = (
        "container=$(docker ps --filter publish=8877 --format '{{.Names}}' | head -1); "
        "if [ -z \"$container\" ]; then container=$(docker ps --filter publish=8876 --format '{{.Names}}' | head -1); fi; "
        "test -n \"$container\" && docker exec \"$container\" "
        f"python3 -c {shlex.quote('import base64;exec(base64.b64decode(' + repr(encoded) + '))')}"
    )
    host, port, username, password = load_connection()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=port, username=username, password=password, timeout=20)
    try:
        payload = json.loads(run(client, command))
    finally:
        client.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = payload.get("report") or {}
    print(f"Exported {len(report.get('answers') or [])} answers to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
