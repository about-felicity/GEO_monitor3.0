from __future__ import annotations

import base64
import json

import paramiko

from deploy_enterprise_update import load_connection, run


REMOTE_SCRIPT = r'''
import json
import sqlite3

database = sqlite3.connect("/app/runtime/remote_tasks.sqlite3")
database.row_factory = sqlite3.Row
columns = [row["name"] for row in database.execute("pragma table_info(remote_tasks)")]
wanted = [
    name for name in (
        "id", "task_kind", "status", "brand_name", "product_name",
        "worker_id", "message", "error", "customer_slug", "created_at", "updated_at",
        "started_at", "completed_steps", "total_steps", "lease_expires_at",
        "high_probability_prior",
    ) if name in columns
]
tasks = [
    dict(row) for row in database.execute(
        "select " + ",".join(wanted) + " from remote_tasks order by rowid desc limit 12"
    )
]
latest_results = []
latest_events = []
if tasks:
    for row in database.execute(
        "select model_id,created_at,record_json from remote_task_results "
        "where task_id=? order by created_at,request_id",
        (tasks[0]["id"],),
    ):
        try:
            record = json.loads(row["record_json"] or "{}")
        except (TypeError, ValueError):
            record = {}
        latest_results.append({
            "model_id": row["model_id"],
            "round": record.get("round"),
            "created_at": row["created_at"],
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
            "capture_mode": record.get("capture_mode"),
            "body_length": len(str(record.get("web_body") or record.get("reply") or "")),
            "source_count": len(record.get("sources") or []),
        })
    latest_events = [
        dict(row) for row in database.execute(
            "select created_at,level,message from remote_task_events "
            "where task_id=? order by id",
            (tasks[0]["id"],),
        )
    ]
workers = []
for table in ("remote_workers", "workers", "worker_status"):
    try:
        workers = [dict(row) for row in database.execute(
            "select * from " + table + " order by rowid desc limit 8"
        )]
    except sqlite3.Error:
        continue
    if workers:
        break
settings = [dict(row) for row in database.execute(
    "select key,value,updated_at from service_settings order by key"
)]
report_summaries = []
try:
    from monitor_core.remote_tasks import RemoteTaskQueue
    queue = RemoteTaskQueue("/app/runtime/remote_tasks.sqlite3")
    for task in tasks[:2]:
        payload = queue.diagnosis_report(task.get("customer_slug") or "")
        report = payload.get("report") or {}
        report_summaries.append({
            "task_id": task.get("id"),
            "customer_slug": task.get("customer_slug"),
            "overall_rate": report.get("overall_rate"),
            "high_probability_prior_applied": (report.get("probability_adjustment") or {}).get("high_probability_prior_applied"),
            "policy_comparison": report.get("policy_comparison"),
            "models": [
                {"model": item.get("model"), "rate": item.get("recommendation_rate")}
                for item in report.get("models") or []
            ],
        })
except Exception as exc:
    report_summaries = [{"error": f"{type(exc).__name__}: {exc}"}]
print(json.dumps({"columns": columns, "tasks": tasks, "latest_results": latest_results, "latest_events": latest_events, "workers": workers, "settings": settings, "report_summaries": report_summaries}, ensure_ascii=False, default=str))
'''


def main() -> None:
    encoded = base64.b64encode(REMOTE_SCRIPT.encode("utf-8")).decode("ascii")
    command = (
        "container=$(docker ps --filter publish=8877 --format '{{.Names}}' | head -1); "
        "if [ -z \"$container\" ]; then "
        "container=$(docker ps --filter publish=8876 --format '{{.Names}}' | head -1); fi; "
        "test -n \"$container\" && docker exec \"$container\" "
        f"python3 -c \"import base64;exec(base64.b64decode('{encoded}'))\""
    )
    host, port, username, password = load_connection()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=port, username=username, password=password, timeout=20)
    try:
        payload = json.loads(run(client, command))
    finally:
        client.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
