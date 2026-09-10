from __future__ import annotations

import argparse
import base64
import json

import paramiko

from deploy_enterprise_update import load_connection, run


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    args = parser.parse_args()
    script = f'''import json, sqlite3
db = sqlite3.connect("/app/runtime/remote_tasks.sqlite3")
db.row_factory = sqlite3.Row
task = db.execute("select status,error,message,current_model,completed_steps,total_steps from remote_tasks where id=?", ({args.task_id!r},)).fetchone()
events = db.execute("select level,message,created_at from remote_task_events where task_id=? order by id desc limit 12", ({args.task_id!r},)).fetchall()
print(json.dumps({{"task": dict(task) if task else None, "events": [dict(row) for row in events]}}, ensure_ascii=False))
'''
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    command = (
        "cd /opt/geo-monitor/deploy/hybrid && docker compose exec -T panel "
        f"python3 -c \"import base64;exec(base64.b64decode('{encoded}'))\""
    )
    host, port, username, password = load_connection()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=port, username=username, password=password, timeout=20)
    payload = json.loads(run(client, command))
    client.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
