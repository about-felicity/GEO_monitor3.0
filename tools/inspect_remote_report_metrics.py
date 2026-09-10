from __future__ import annotations

import base64
import json

import paramiko

from deploy_enterprise_update import load_connection, run


REMOTE_SCRIPT = r'''
import json
import sqlite3

from monitor_core.remote_tasks import RemoteTaskQueue

path = "/app/runtime/remote_tasks.sqlite3"
database = sqlite3.connect(path)
slugs = [
    row[0] for row in database.execute(
        "select customer_slug from remote_tasks "
        "where task_kind='diagnosis' and status='completed' "
        "order by rowid desc limit 2"
    )
]
queue = RemoteTaskQueue(path)
reports = []
navigation_markers = (
    "近期对话", "最近对话", "历史对话", "全部对话", "对话历史", "我的对话",
    "历史会话", "最近会话", "我的会话", "新对话", "新建对话", "新会话", "新建会话",
)
for slug in slugs:
    payload = queue.diagnosis_report(slug)
    report = payload.get("report") or {}
    reports.append({
        "slug": slug,
        "overall_rate": report.get("overall_rate"),
        "probability_method": (report.get("probability_adjustment") or {}).get("method"),
        "source_title_leaks": sum(
            1 for answer in report.get("answers") or []
            if any(
                str(source.get("title") or "").strip()
                and str(source.get("title") or "").strip() in str(answer.get("answer") or "")
                for source in answer.get("sources") or []
            )
        ),
        "source_title_leak_details": [
            {"model": answer.get("model"), "title": source.get("title")}
            for answer in report.get("answers") or []
            for source in answer.get("sources") or []
            if str(source.get("title") or "").strip()
            and str(source.get("title") or "").strip() in str(answer.get("answer") or "")
        ],
        "brand_source_title_leaks": [
            {"model": answer.get("model"), "title": source.get("title")}
            for answer in report.get("answers") or []
            for source in answer.get("sources") or []
            if any(
                len(token) >= 2 and token.casefold() in str(source.get("title") or "").casefold()
                for token in str(report.get("brand_name") or "").replace("-", " ").split()
            )
            and str(source.get("title") or "").strip() in str(answer.get("answer") or "")
        ],
        "navigation_leaks": [
            {"model": answer.get("model"), "markers": [
                marker for marker in navigation_markers
                if marker in str(answer.get("answer") or "")
            ]}
            for answer in report.get("answers") or []
            if any(marker in str(answer.get("answer") or "") for marker in navigation_markers)
        ],
        "models": [
            {
                "model": item.get("id"),
                "rate": item.get("recommendation_rate"),
                "raw_rate": item.get("raw_recommendation_rate"),
                "rank_quality": item.get("rank_quality"),
                "evidence_quality": item.get("evidence_quality"),
            }
            for item in report.get("models") or []
        ],
    })
print(json.dumps({"readiness": queue.diagnosis_readiness(), "reports": reports}, ensure_ascii=False))
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
