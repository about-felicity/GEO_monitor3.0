from __future__ import annotations


LEGACY_EMULATOR_GUARD_PORT = 18800
WEB_ONLY_GUARD_PORTS = {"wenxin": 18803}


def collector_guard_port(model: str) -> int:
    """Web-only Wenxin can coexist with the single emulator-based collector."""
    return WEB_ONLY_GUARD_PORTS.get(str(model or "").strip(), LEGACY_EMULATOR_GUARD_PORT)
