"""Local outbound worker for tasks created on the public dashboard server."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import signal
import socket
import ssl
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from monitor_core.plugins import ROOT
from monitor_core.quality import answer_quality_reason
from monitor_core.scheduling import build_question_schedule
from web_collectors.browser import cookie_env_name
from web_collectors.collector import BrowserCollector, create_collector
from web_collectors.loop import append_jsonl, now, persist_database


MODEL_ORDER = ("doubao", "yuanbao", "wenxin", "deepseek", "quark")
DIAGNOSIS_MODELS = ("doubao", "yuanbao", "wenxin")
MODEL_NAMES = {"doubao": "豆包", "yuanbao": "腾讯元宝", "wenxin": "文心一言"}


def worker_readiness() -> dict[str, dict[str, Any]]:
    status: dict[str, dict[str, Any]] = {}
    for model in DIAGNOSIS_MODELS:
        raw = os.environ.get(cookie_env_name(model), "").strip()
        count = 0
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    parsed = parsed.get("cookies")
                count = len(parsed) if isinstance(parsed, list) else 0
            except (TypeError, ValueError, json.JSONDecodeError):
                count = 0
        status[model] = {
            "ready": count > 0,
            "message": "临时凭证已加载" if count > 0 else "临时凭证未加载",
        }
    return status


def load_worker_env() -> None:
    path = ROOT / "config" / "remote_worker.env"
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in {"MONITOR_TASK_SERVER", "MONITOR_WORKER_TOKEN", "MONITOR_WORKER_ID"}:
            os.environ.setdefault(key, value)


class WorkerAPI:
    def __init__(self, base_url: str, token: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            self.base_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "GEO-Monitor-Local-Worker/1.0",
            },
            method="POST",
        )
        try:
            context = ssl.create_default_context()
            try:
                import certifi
                context = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                pass
            with urlopen(request, timeout=self.timeout, context=context) as response:
                value = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:800]
            raise RuntimeError(f"服务器返回 HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise ConnectionError(f"无法连接任务服务器：{exc.reason}") from exc
        if not isinstance(value, dict) or not value.get("ok"):
            raise RuntimeError(str(value.get("error") if isinstance(value, dict) else value))
        return value


def result_record(model: str, question: str, round_number: int, started: str,
                  result: dict[str, Any], task_id: str) -> dict[str, Any]:
    body = str(result.get("body") or result.get("page_body") or "").strip()
    reason = answer_quality_reason(question, body)
    if reason:
        raise RuntimeError(f"回答质量校验失败：{reason}")
    sources = [dict(item) for item in result.get("sources") or [] if isinstance(item, dict)]
    capture_mode = str(result.get("capture_mode") or "headless_web")
    return {
        "collector_model": model,
        "model_id": model,
        "serial": f"{model}-web",
        "task_id": 1,
        "round": round_number,
        "question": question,
        "prompt": question,
        "reply": body,
        "web_body": body,
        "sources": sources,
        "status": "success",
        "started_at": started,
        "finished_at": now(),
        "capture_mode": capture_mode,
        "capture_label": (
            "Scrapling 隐身浏览器网页直采"
            if capture_mode.startswith("scrapling") else "隐身无头浏览器网页直采"
        ),
        "body_capture_complete": True,
        "expected_source_count": int(result.get("expected_source_count") or len(sources)),
        "source_capture_complete": bool(result.get("source_capture_complete", True)),
        "page_url": str(result.get("url") or ""),
        "remote_task_id": task_id,
    }


def analyze_record(record: dict[str, Any], log: logging.Logger) -> None:
    """Run the existing grounded product analysis locally before upload."""
    try:
        import doubao_env_loader  # noqa: F401
        import save_doubao_refs as saver
        question = str(record.get("question") or "")
        if not saver.is_recommendation_question(question):
            return
        products, status, method, analysis_model = saver.review_products_with_ai(
            str(record.get("web_body") or record.get("reply") or ""), question
        )
        brands = []
        for item in products:
            brand = str(item.get("brand_name") or item.get("brand") or "").strip()
            if brand and brand not in brands:
                brands.append(brand)
        record.update({
            "products": products,
            "brands": brands,
            "product_review_status": status,
            "product_extraction_method": method,
            "product_analysis_model": analysis_model,
        })
    except Exception as exc:
        record.update({
            "products": [], "brands": [], "product_review_status": "ai_pending",
            "product_extraction_method": "pending",
        })
        log.warning("本轮回答已采集，但产品分析暂时失败：%s", exc)


def run_login(api: WorkerAPI, login: dict[str, Any], log: logging.Logger,
              *, timeout: int = 600) -> None:
    login_id = str(login["id"])
    lease = str(login["lease_token"])
    model = str(login.get("model_id") or "")
    name = MODEL_NAMES.get(model, model)
    collector: BrowserCollector | None = None

    def heartbeat(message: str) -> None:
        api.post(
            f"/api/worker/logins/{login_id}/heartbeat",
            {"lease_token": lease, "message": message},
        )

    try:
        os.environ.pop(cookie_env_name(model), None)
        heartbeat(f"正在本机打开 {name} 隐身登录窗口")
        collector = BrowserCollector(model, headless=False)
        cookies = collector.wait_for_login_cookies(timeout=timeout, progress=heartbeat)
        if not cookies:
            raise RuntimeError(f"{name} 登录完成后未检测到有效会话")
        os.environ[cookie_env_name(model)] = json.dumps(
            cookies, ensure_ascii=False, separators=(",", ":")
        )
        api.post(
            f"/api/worker/logins/{login_id}/finish",
            {"lease_token": lease, "status": "ready"},
        )
        log.info("%s 隐身登录已就绪，临时凭证仅保存在 Worker 内存", name)
    except Exception as exc:
        os.environ.pop(cookie_env_name(model), None)
        log.exception("%s 隐身登录失败", name)
        try:
            api.post(
                f"/api/worker/logins/{login_id}/finish",
                {"lease_token": lease, "status": "failed",
                 "error": f"{type(exc).__name__}: {exc}"[:900]},
            )
        except Exception:
            log.exception("登录失败状态回传失败")
    finally:
        if collector is not None:
            collector.close()


def run_task(api: WorkerAPI, task: dict[str, Any], log: logging.Logger,
             *, timeout: int, stable_seconds: int, min_interval: float,
             max_interval: float, attempts: int) -> None:
    task_id = str(task["id"])
    lease = str(task["lease_token"])
    questions = [str(item) for item in task.get("questions") or []]
    rounds = int(task.get("rounds") or 1)
    mode = str(task.get("question_mode") or "interleaved")
    schedule = build_question_schedule(questions, rounds, mode)
    selected = set(task.get("models") or [])
    models = [item for item in MODEL_ORDER if item in selected]
    try:
        for model in models:
            heartbeat = api.post(
                f"/api/worker/tasks/{task_id}/heartbeat",
                {"lease_token": lease, "model": model, "message": f"正在检查 {model} 登录状态"},
            )
            if heartbeat.get("cancel_requested"):
                api.post(f"/api/worker/tasks/{task_id}/finish",
                         {"lease_token": lease, "status": "cancelled"})
                return
            collector = create_collector(model, headless=True)
            try:
                ready = collector.check_ready()
                if not ready.get("ok"):
                    log.error("%s 网页登录检查失败：%s", model, ready.get("message"))
                    raise RuntimeError(
                        f"{MODEL_NAMES.get(model, model)}隐身登录未就绪，请联系管理员完成登录"
                    )
                for index, question in enumerate(schedule, 1):
                    heartbeat = api.post(
                        f"/api/worker/tasks/{task_id}/heartbeat",
                        {"lease_token": lease, "model": model, "question": question,
                         "message": f"{model} 正在执行第 {index}/{len(schedule)} 轮"},
                    )
                    if heartbeat.get("cancel_requested"):
                        api.post(f"/api/worker/tasks/{task_id}/finish",
                                 {"lease_token": lease, "status": "cancelled"})
                        return
                    last_error: Exception | None = None
                    record: dict[str, Any] | None = None
                    for attempt in range(1, attempts + 1):
                        started = now()
                        try:
                            captured = collector.collect(
                                question, timeout=timeout, stable_seconds=stable_seconds
                            )
                            record = result_record(
                                model, question, index, started, captured, task_id
                            )
                            record.update({
                                "customer_slug": str(task.get("customer_slug") or ""),
                                "brand_name": str(task.get("brand_name") or ""),
                                "product_name": str(task.get("product_name") or ""),
                                "task_kind": str(task.get("task_kind") or ""),
                            })
                            analyze_record(record, log)
                            local_path = ROOT / "runtime" / "web_results" / f"{model}_results.jsonl"
                            append_jsonl(local_path, record)
                            persist_database(model, record, log)
                            last_error = None
                            break
                        except Exception as exc:
                            last_error = exc
                            log.exception("%s 第 %d 轮第 %d 次尝试失败", model, index, attempt)
                            try:
                                api.post(
                                    f"/api/worker/tasks/{task_id}/heartbeat",
                                    {
                                        "lease_token": lease,
                                        "model": model,
                                        "question": question,
                                        "message": (
                                            f"{model} 第 {index} 轮第 {attempt} 次尝试失败"
                                            + ("，准备重试" if attempt < attempts else "")
                                        ),
                                    },
                                )
                            except Exception:
                                log.warning("采集失败日志暂未回传服务器")
                            if attempt < attempts:
                                time.sleep(min(30, max(5, min_interval)))
                    if last_error is not None or record is None:
                        raise last_error or RuntimeError("采集未生成结果")
                    identity = "\0".join((task_id, model, str(index), question))
                    request_id = "task-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
                    upload_error: Exception | None = None
                    for upload_attempt in range(1, 4):
                        try:
                            api.post(
                                f"/api/worker/tasks/{task_id}/result",
                                {"lease_token": lease, "model_id": model,
                                 "request_id": request_id, "record": record},
                            )
                            upload_error = None
                            break
                        except Exception as exc:
                            upload_error = exc
                            log.warning("结果上传第 %d 次失败，将重试：%s", upload_attempt, exc)
                            if upload_attempt < 3:
                                time.sleep(5 * upload_attempt)
                    if upload_error is not None:
                        raise upload_error
                    log.info("%s 第 %d/%d 轮完成", model, index, len(schedule))
                    if index < len(schedule):
                        time.sleep(random.uniform(max(1, min_interval), max(min_interval, max_interval)))
            finally:
                collector.close()
        api.post(f"/api/worker/tasks/{task_id}/finish",
                 {"lease_token": lease, "status": "completed"})
    except Exception as exc:
        log.exception("任务 %s 执行失败", task_id)
        try:
            api.post(
                f"/api/worker/tasks/{task_id}/finish",
                {"lease_token": lease, "status": "failed",
                 "error": f"{type(exc).__name__}: {exc}"[:1800]},
            )
        except Exception:
            log.exception("任务失败状态回传失败")


def main() -> int:
    load_worker_env()
    def stop_worker(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop_worker)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, stop_worker)
    parser = argparse.ArgumentParser(description="本机五模型远程任务采集代理")
    parser.add_argument("--server", default=os.environ.get("MONITOR_TASK_SERVER", ""))
    parser.add_argument("--token", default=os.environ.get("MONITOR_WORKER_TOKEN", ""))
    parser.add_argument("--worker-id", default=os.environ.get("MONITOR_WORKER_ID", socket.gethostname()))
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--stable-seconds", type=int, default=6)
    parser.add_argument("--min-interval", type=float, default=30)
    parser.add_argument("--max-interval", type=float, default=60)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--login-timeout", type=int, default=600)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not args.server or not args.token:
        raise SystemExit("请配置 MONITOR_TASK_SERVER 和 MONITOR_WORKER_TOKEN")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    log = logging.getLogger("remote-worker")
    api = WorkerAPI(args.server, args.token)
    log.info("本机任务代理已启动：%s", args.worker_id)
    while True:
        try:
            response = api.post(
                "/api/worker/claim",
                {"worker_id": args.worker_id, "readiness": worker_readiness()},
            )
            login = response.get("login")
            task = response.get("task")
            if login:
                log.info("领取登录请求：%s · %s", login.get("model_id"), login.get("id"))
                run_login(api, login, log, timeout=max(60, args.login_timeout))
            elif task:
                log.info("领取任务：%s", task.get("id"))
                run_task(
                    api, task, log, timeout=max(30, args.timeout),
                    stable_seconds=max(2, args.stable_seconds),
                    min_interval=max(1, args.min_interval),
                    max_interval=max(args.min_interval, args.max_interval),
                    attempts=max(1, min(args.attempts, 3)),
                )
            elif args.once:
                return 0
        except KeyboardInterrupt:
            return 0
        except Exception:
            log.exception("任务服务器通信失败")
            if args.once:
                return 1
        time.sleep(max(2, args.poll_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
