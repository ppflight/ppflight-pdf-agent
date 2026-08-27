#!/usr/bin/env python3
"""PPFlight PDF Agent command-line entry point."""
import argparse
import json
import signal
import sys
import threading

from pdf_agent.core import Agent, AgentConfig, AgentError, DownloadServer, VERSION


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ppflight-pdf-agent")
    parser.add_argument("--config", required=False, default="agent.json", help="absolute-path JSON configuration")
    subs = parser.add_subparsers(dest="command", required=True)
    bind = subs.add_parser("bind", help="bind to ADMIN once")
    bind.add_argument("--replace", action="store_true", help="replace local state only after ADMIN accepts")
    code = bind.add_mutually_exclusive_group(required=True)
    code.add_argument("--code", help="one-time binding code (avoid shell history where possible)")
    code.add_argument("--code-stdin", action="store_true", help="read one-time binding code from standard input")
    subs.add_parser("check", help="validate binding and heartbeat")
    subs.add_parser("run", help="run the single-worker polling agent")
    subs.add_parser("version", help="print version")
    args = parser.parse_args(argv)
    if args.command == "version":
        print(VERSION)
        return 0
    try:
        agent = Agent(AgentConfig.load(args.config))
        try:
            if args.command == "bind":
                binding_code = sys.stdin.readline().rstrip("\r\n") if args.code_stdin else args.code
                print(json.dumps({"agent_id": agent.bind(binding_code, args.replace)}, sort_keys=True))
            elif args.command == "check":
                print(json.dumps(agent.check(), sort_keys=True))
            else:
                server = DownloadServer(agent)
                stop = threading.Event()
                signal.signal(signal.SIGTERM, lambda *_: stop.set())
                signal.signal(signal.SIGINT, lambda *_: stop.set())
                server.start()
                try:
                    agent.run(stop)
                finally:
                    server.close()
            return 0
        finally:
            agent.close()
    except AgentError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
