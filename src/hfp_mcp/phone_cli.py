"""Administrative routing, caller context and exact-run approval commands."""

import argparse
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode

from .caller_context import CallerStore
from .contracts import normalize_phone_number
from .routing import _NAME, RoutingConfig
from .settings import RuntimeConfig


def control(path, body=None):
    config = RuntimeConfig.load()
    token = config.bearer_token or config.bearer_token_file.read_text().strip()
    base = config.daemon_url or f"http://127.0.0.1:{config.port}"
    base = base.removesuffix("/mcp").rstrip("/")
    request = Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    with urlopen(request, timeout=100) as response:
        return json.load(response)


def main(argv):
    parser = argparse.ArgumentParser(prog="hfp-mcp " + argv[0])
    if argv[0] == "route":
        parser.add_argument("action", choices=["explain", "validate"])
        parser.add_argument("number", nargs="?")
        parser.add_argument("--config", type=Path)
        parser.add_argument("--outgoing", action="store_true", help="Explain an owner-requested outgoing destination")
        args = parser.parse_args(argv[1:])
        config = RoutingConfig.load(args.config)
        print(
            json.dumps(
                config.explain(args.number, outbound=args.outgoing)
                if args.action == "explain"
                else {"valid": True, "enabled": config.enabled},
                indent=2,
            )
        )
    elif argv[0] == "caller":
        parser.add_argument("action", choices=["inspect", "forget"])
        parser.add_argument("--profile", required=True)
        parser.add_argument("--number", required=True)
        parser.add_argument("--store", type=Path)
        args = parser.parse_args(argv[1:])
        if not _NAME.fullmatch(args.profile):
            parser.error("invalid profile name")
        home = Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser()
        if args.profile != "default":
            home = home / "profiles" / args.profile
        path = args.store or home / "hfp-phone" / "callers.sqlite3"
        if not path.exists():
            print(json.dumps({"notes": "", "exists": False}))
            return 0
        store = CallerStore(path)
        try:
            identity = store.caller_id(
                normalize_phone_number(args.number, RoutingConfig.load().region)
            )
            if args.action == "forget":
                store.forget(args.profile, identity)
                print(json.dumps({"forgotten": True}))
            else:
                print(
                    json.dumps({"notes": store.read(args.profile, identity)}, indent=2)
                )
        finally:
            store.close()
    elif argv[0] == "transcript":
        parser.add_argument("action", choices=["list", "show"])
        parser.add_argument("--call-id")
        parser.add_argument("--output", type=Path, help="Save private JSON; refuses to overwrite an existing file")
        args = parser.parse_args(argv[1:])
        if args.action == "list":
            result = control("/v1/phone/transcripts/calls")
        else:
            query = {"call_id": args.call_id} if args.call_id else {}
            result = control("/v1/phone/transcripts?" + urlencode(query))
            # Pin the selected call across pages even if another call arrives.
            while result.get("has_more"):
                page = control("/v1/phone/transcripts?" + urlencode({
                    "call_id": result["session_id"], "after_id": result["next_after_id"],
                }))
                result["events"].extend(page["events"])
                result.update(has_more=page["has_more"], next_after_id=page["next_after_id"])
        output = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
        if args.output:
            fd = os.open(args.output.expanduser(), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as file:
                file.write(output)
            print(args.output.expanduser().resolve())
        else:
            print(output, end="")
    else:
        parser.add_argument("action", choices=["status", "approve", "deny"])
        parser.add_argument("--request-id")
        args = parser.parse_args(argv[1:])
        if args.action != "status" and not args.request_id:
            parser.error("--request-id is required")
        result = (
            control("/v1/phone")
            if args.action == "status"
            else control(
                "/v1/phone/approval",
                {
                    "request_id": args.request_id,
                    "choice": "once" if args.action == "approve" else "deny",
                },
            )
        )
        print(json.dumps(result, indent=2))
    return 0
