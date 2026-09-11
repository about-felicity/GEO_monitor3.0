from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .collectors import CREATE_NO_WINDOW, DEFAULT_ADB, ROOT, _physical_memory


DEVICES = {
    "yuanbao": (os.environ.get("GEO_YUANBAO_SERIAL", "127.0.0.1:21503"), "com.tencent.hunyuan.app.chat", 0, 850),
    "doubao": (os.environ.get("GEO_DOUBAO_SERIAL", "127.0.0.1:21513"), "com.larus.nova", 1, 1300),
}

MANAGED_BROWSER_PORTS = {9222: "yuanbao", 9227: "kimi", 9301: "doubao"}


def parse_listening_pids(text: str, ports: set[int] | None = None) -> dict[int, int]:
    """Return listening TCP port -> PID mappings from Windows netstat output."""
    wanted = ports or set(MANAGED_BROWSER_PORTS)
    found: dict[int, int] = {}
    for line in str(text or "").splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0].upper() != "TCP" or fields[3].upper() != "LISTENING":
            continue
        local = fields[1].rsplit(":", 1)
        try:
            port, pid = int(local[-1]), int(fields[-1])
        except (TypeError, ValueError):
            continue
        if port in wanted and pid > 0:
            found[port] = pid
    return found


def parse_meminfo(text: str) -> tuple[int, int]:
    pss = (
        re.search(r"TOTAL PSS:\s*([0-9,]+)", text, re.I)
        or re.search(r"^\s*TOTAL\s+([0-9,]+)", text, re.I | re.M)
        or re.search(r"^\s*TOTAL:\s*([0-9,]+)", text, re.I | re.M)
    )
    java = re.search(r"Java Heap:\s*([0-9,]+)", text, re.I)
    return (
        int(pss.group(1).replace(",", "")) if pss else 0,
        int(java.group(1).replace(",", "")) if java else 0,
    )


class ResourceSupervisor:
    """Bounded always-on host/emulator health sampler and conservative recovery."""

    def __init__(self) -> None:
        self.adb = Path(os.environ.get("GEO_ADB_PATH", DEFAULT_ADB))
        self.console = Path(r"D:\Program Files\Microvirt\MEmu\memuc.exe")
        self.interval = max(10, int(os.environ.get("GEO_HEALTH_INTERVAL", "15")))
        self._active: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._failures = {model: 0 for model in DEVICES}
        self._last_restart = {model: 0.0 for model in DEVICES}
        self._last_browser_reclaim = 0.0
        self.latest: dict = {}
        self._thread = threading.Thread(target=self._loop, name="geo-resource-supervisor", daemon=True)
        self._thread.start()

    @contextmanager
    def activity(self, model: str) -> Iterator[None]:
        activity_dir = ROOT / "runtime" / "activity"
        marker = activity_dir / f"{model}-{os.getpid()}.json"
        with self._lock:
            self._active.add(model)
        try:
            activity_dir.mkdir(parents=True, exist_ok=True)
            temporary = marker.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"model": model, "pid": os.getpid(), "started_at": time.time()}),
                encoding="utf-8",
            )
            temporary.replace(marker)
        except OSError:
            pass
        try:
            yield
        finally:
            with self._lock:
                self._active.discard(model)
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                pass

    def _external_activity(self) -> set[str]:
        """Read bounded cross-process activity leases left by collectors."""
        active: set[str] = set()
        activity_dir = ROOT / "runtime" / "activity"
        cutoff = time.time() - max(900, int(os.environ.get("GEO_ACTIVITY_LEASE_SECONDS", "1200")))
        try:
            markers = list(activity_dir.glob("*.json"))
        except OSError:
            return active
        for marker in markers:
            try:
                if marker.stat().st_mtime >= cutoff:
                    model = marker.stem.rsplit("-", 1)[0]
                    if model:
                        active.add(model)
                else:
                    marker.unlink(missing_ok=True)
            except OSError:
                continue
        return active

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)

    def _adb(self, *args: str, timeout: int = 12) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.adb), *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, creationflags=CREATE_NO_WINDOW, check=False,
        )

    def _sample_device(self, model: str) -> dict:
        serial, package, instance, limit_mb = DEVICES[model]
        state = self._adb("-s", serial, "get-state", timeout=8) if self.adb.is_file() else None
        online = bool(state and state.returncode == 0 and "device" in state.stdout)
        if not online:
            self._failures[model] += 1
            try:
                self._adb("connect", serial, timeout=10)
            except Exception:
                pass
            if (
                self._failures[model] >= 3
                and os.environ.get("GEO_AUTO_RESTART_EMULATOR", "1") == "1"
                and self.console.is_file()
                and time.monotonic() - self._last_restart[model] > 300
            ):
                # A VM can remain marked running while its ADB transport is
                # permanently offline. Restart the affected instance only;
                # keep the other production emulator and web collectors live.
                subprocess.run(
                    [str(self.console), "stop", "-i", str(instance)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=45, creationflags=CREATE_NO_WINDOW, check=False,
                )
                subprocess.run(
                    [str(self.console), "start", "-i", str(instance)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=45, creationflags=CREATE_NO_WINDOW, check=False,
                )
                self._last_restart[model] = time.monotonic()
            return {"online": False, "consecutive_failures": self._failures[model], "pss_mb": 0, "java_heap_mb": 0}
        self._failures[model] = 0
        info = self._adb("-s", serial, "shell", "dumpsys", "meminfo", package, timeout=15)
        pss_kb, java_kb = parse_meminfo(info.stdout if info.returncode == 0 else "")
        with self._lock:
            active = model in self._active
        active = active or model in self._external_activity()
        if pss_kb >= limit_mb * 1024 and not active:
            self._adb("-s", serial, "shell", "am", "force-stop", package, timeout=10)
        return {
            "online": True, "consecutive_failures": 0,
            "pss_mb": round(pss_kb / 1024, 1), "java_heap_mb": round(java_kb / 1024, 1),
            "limit_mb": limit_mb, "active": active,
        }

    def _reclaim_idle_managed_browsers(self, memory: dict[str, int]) -> list[str]:
        """Close only our persistent Chrome bridges under critical host pressure.

        Their user-data directories remain on disk, so authenticated sessions are
        retained and the next collector run can relaunch them automatically.
        """
        if os.name != "nt" or os.environ.get("GEO_AUTO_RECLAIM_BROWSERS", "1") != "1":
            return []
        free_ratio = memory["available"] / max(1, memory["total"])
        # Reclaim persistent automation browsers before the hard collection
        # gate (88%) is reached, leaving headroom for the next model process.
        if memory["load"] < 84 and free_ratio >= 0.16:
            return []
        if time.monotonic() - self._last_browser_reclaim < 300:
            return []
        with self._lock:
            active = set(self._active)
        active.update(self._external_activity())
        try:
            listing = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=12,
                creationflags=CREATE_NO_WINDOW, check=False,
            )
        except Exception:
            return []
        reclaimed: list[str] = []
        for port, pid in parse_listening_pids(listing.stdout).items():
            model = MANAGED_BROWSER_PORTS[port]
            if model in active:
                continue
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=20, creationflags=CREATE_NO_WINDOW, check=False,
            )
            if result.returncode == 0:
                reclaimed.append(model)
        if reclaimed:
            self._last_browser_reclaim = time.monotonic()
        return reclaimed

    def sample(self) -> dict:
        memory = _physical_memory()
        reclaimed = self._reclaim_idle_managed_browsers(memory)
        if reclaimed:
            memory = _physical_memory()
        devices = {}
        for model in DEVICES:
            try:
                devices[model] = self._sample_device(model)
            except Exception as exc:
                devices[model] = {"online": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
        value = {
            "timestamp": time.time(), "host_memory_load": memory["load"],
            "host_available_mb": memory["available"] // 1048576, "devices": devices,
            "reclaimed_managed_browsers": reclaimed,
            "pressure": "critical" if memory["load"] >= 88 else "warning" if memory["load"] >= 82 else "normal",
        }
        self.latest = value
        target = ROOT / "runtime" / "health" / "latest.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)
        return value

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample()
            except Exception:
                pass
            self._stop.wait(self.interval)


SUPERVISOR = ResourceSupervisor()
