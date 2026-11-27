"""Mock ACP stdio server for unit testing."""

import json
import sys
import time


def main() -> None:
    model_id = None
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue

        try:
            req = json.loads(line)
        except Exception:
            continue

        method = req.get("method")
        req_id = req.get("id")
        params = req.get("params", {})

        if method == "initialize":
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "1.0",
                    "serverInfo": {"name": "mock-acp-server", "version": "0.1.0"},
                },
            }
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()

        elif method == "session/new":
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"sessionId": "mock-session-xyz", "cwd": params.get("cwd")},
            }
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()

        elif method == "session/set_config_option" and params.get("configId") == "model":
            model_id = params.get("value")
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {}}) + "\n")
            sys.stdout.flush()

        elif method == "session/prompt":
            sess_id = params.get("sessionId")
            raw_prompt = params.get("prompt", "")
            if isinstance(raw_prompt, list):
                prompt = " ".join(b.get("text", "") for b in raw_prompt if isinstance(b, dict))
            else:
                prompt = str(raw_prompt)

            # Emit notifications before returning response
            notif1 = {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"sessionId": sess_id, "delta": "Analyzing prompt...", "status": "running"},
            }
            sys.stdout.write(json.dumps(notif1) + "\n")
            sys.stdout.flush()

            notif2 = {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"sessionId": sess_id, "delta": "Execution finished.", "status": "completed"},
            }
            sys.stdout.write(json.dumps(notif2) + "\n")
            sys.stdout.flush()

            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"status": "completed", "output": f"Executed: {prompt}"},
            }
            if model_id:
                resp["result"]["output"] += f" with {model_id}"
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()

        elif method == "session/cancel":
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"cancelled": True, "sessionId": params.get("sessionId")},
            }
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()

        elif method == "error_trigger":
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32600, "message": "Invalid request triggered", "data": {"detail": "test"}},
            }
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()

        elif method == "sleep_long":
            time.sleep(1.5)
            resp = {"jsonrpc": "2.0", "id": req_id, "result": "woke up"}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()

        elif method == "crash":
            sys.exit(1)

        elif req_id is not None:
            # Unknown method response
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method '{method}' not found"},
            }
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
