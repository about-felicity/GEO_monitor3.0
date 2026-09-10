from __future__ import annotations

import json
import sys
from urllib.request import urlopen

import websocket


DEBUG_TARGETS = "http://127.0.0.1:9223/json/list"
EXTENSION_NAME = "夸克对话监控（本地版）"


def main() -> int:
    with urlopen(DEBUG_TARGETS, timeout=4) as response:
        targets = json.load(response)
    for target in targets:
        if target.get("type") != "service_worker" or not target.get("webSocketDebuggerUrl"):
            continue
        socket = websocket.create_connection(
            target["webSocketDebuggerUrl"], timeout=5, suppress_origin=True
        )
        try:
            request = {
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": (
                        "chrome.runtime.getManifest().name === "
                        + json.dumps(EXTENSION_NAME, ensure_ascii=False)
                        + " ? (chrome.runtime.reload(), 'reloaded') : 'ignored'"
                    ),
                    "returnByValue": True,
                },
            }
            socket.send(json.dumps(request, ensure_ascii=False))
            while True:
                reply = json.loads(socket.recv())
                if reply.get("id") != 1:
                    continue
                value = ((reply.get("result") or {}).get("result") or {}).get("value")
                if value == "reloaded":
                    print("quark_extension_reload_requested")
                    return 0
                break
        finally:
            socket.close()
    print("quark_extension_service_worker_not_found", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
