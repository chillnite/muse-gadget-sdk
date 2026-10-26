# SPDX-License-Identifier: Apache-2.0

"""``gadgethub`` command line."""

from __future__ import annotations

import argparse
import json
import logging
import os
import ssl
import sys
import urllib.error
import urllib.request

from gadgethub import __version__, store
from gadgethub.agent import DEFAULT_EFFORT, DEFAULT_MODEL

log = logging.getLogger("gadgethub")

DEFAULT_PORT = 8080
HUB_URL_ENV = "GADGETHUB_URL"
PUBLIC_URL_ENV = "GADGETHUB_PUBLIC_URL"


def cmd_serve(args: argparse.Namespace) -> int:
    import anthropic
    from aiohttp import web

    from gadgethub.agent import Agent
    from gadgethub.chat import Chat
    from gadgethub.server import Hub

    if not args.public_url or not args.public_url.startswith("https://"):
        print("Pass --public-url https://HOST: the address gadgets reach this hub at. "
              "Gadgets only connect over TLS.", file=sys.stderr)
        return 1
    if bool(args.tls_cert) != bool(args.tls_key):
        print("Pass both --tls-cert and --tls-key, or neither.", file=sys.stderr)
        return 1
    state = store.Store.load()
    hub = None
    agent = Agent(anthropic.AsyncAnthropic(), lambda: hub.gadgets(),
                  model=args.model, effort=args.effort)
    hub = Hub(state, Chat(agent), args.public_url)
    tls = None
    if args.tls_cert:
        tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        tls.load_cert_chain(args.tls_cert, args.tls_key)
    state.admin_token()  # create it now, so `gadgethub chat` works from the start
    log.info("gadgethub %s: %s, gadgets connect to %s", __version__, args.model, args.public_url)
    web.run_app(hub.app(), host=args.host, port=args.port, ssl_context=tls,
                print=None, access_log=None)
    return 0


def _admin(args: argparse.Namespace, method: str, path: str, body: dict | None = None):
    token = store.Store.load().admin_token()
    request = urllib.request.Request(
        args.hub.rstrip("/") + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    return urllib.request.urlopen(request, timeout=600)


def cmd_pair_code(args: argparse.Namespace) -> int:
    with _admin(args, "POST", "/api/pair-codes", {}) as response:
        data = json.loads(response.read())
    print(f"Pairing code: {data['code']} (valid for 10 minutes, once)")
    print("On a Linux gadget, run:")
    print(f"  sudo musegadget pair-hub {data['hub_url']} {data['code']}")
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    with _admin(args, "GET", "/api/devices") as response:
        devices = json.loads(response.read())["devices"]
    if not devices:
        print("No gadgets paired. Run `gadgethub pair-code` to pair one.")
    for device in devices:
        state = "connected" if device["connected"] else "offline"
        print(f"{device['node_id']}  {device['name']}  {state}")
        if device["commands"]:
            print("    " + ", ".join(device["commands"]))
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    with _admin(args, "DELETE", f"/api/devices/{args.node_id}"):
        pass
    print(f"Removed {args.node_id}. It has to be paired again to reconnect.")
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    def turn(message: str) -> None:
        body = {"message": message}
        if args.session_id:
            body["session_id"] = args.session_id
        with _admin(args, "POST", "/api/chat", body) as response:
            for line in response:
                event = json.loads(line)
                payload = event.get("payload") or {}
                if event.get("event") == "delta.message_done":
                    print(payload.get("display_text", ""), flush=True)
                elif event.get("event") == "agent.status" and payload.get("activity_code") == "executing":
                    print(f"  ({payload.get('activity_text')})", file=sys.stderr, flush=True)

    if args.message:
        turn(" ".join(args.message))
        return 0
    print("Chatting with Claude through the hub. Ctrl-D to quit.", file=sys.stderr)
    while True:
        try:
            message = input("> ")
        except EOFError:
            print(file=sys.stderr)
            return 0
        if message.strip():
            turn(message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gadgethub")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--hub", default=os.environ.get(HUB_URL_ENV) or f"http://127.0.0.1:{DEFAULT_PORT}",
                        help="where the running hub listens, for the admin commands "
                             "(default: $GADGETHUB_URL or %(default)s)")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the hub")
    serve.add_argument("--public-url", default=os.environ.get(PUBLIC_URL_ENV),
                       help="https://HOST[:PORT] that gadgets reach this hub at "
                            "(default: $GADGETHUB_PUBLIC_URL)")
    serve.add_argument("--host", default="127.0.0.1", help="address to listen on (default: %(default)s)")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help="port to listen on (default: %(default)s)")
    serve.add_argument("--tls-cert", help="serve TLS with this certificate chain (PEM)")
    serve.add_argument("--tls-key", help="the certificate's private key (PEM)")
    serve.add_argument("--model", default=DEFAULT_MODEL, help="Claude model (default: %(default)s)")
    serve.add_argument("--effort", default=DEFAULT_EFFORT,
                       choices=["low", "medium", "high", "xhigh", "max"],
                       help="how hard Claude thinks (default: %(default)s)")
    serve.set_defaults(func=cmd_serve)

    sub.add_parser("pair-code", help="make a one-time code to pair a gadget").set_defaults(func=cmd_pair_code)
    sub.add_parser("devices", help="list paired gadgets").set_defaults(func=cmd_devices)
    remove = sub.add_parser("remove", help="unpair a gadget")
    remove.add_argument("node_id")
    remove.set_defaults(func=cmd_remove)

    chat = sub.add_parser("chat", help="chat with Claude, which can use your gadgets")
    chat.add_argument("message", nargs="*", help="send one message and print the reply")
    chat.add_argument("--session-id", help="use this side chat instead of the main chat")
    chat.set_defaults(func=cmd_chat)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except urllib.error.HTTPError as exc:
        print(f"The hub refused the request: HTTP {exc.code}", file=sys.stderr)
    except urllib.error.URLError as exc:
        print(f"Could not reach the hub at {args.hub}: {exc.reason}. Is `gadgethub serve` running?",
              file=sys.stderr)
    except PermissionError as exc:
        print(f"Can't access {exc.filename or store.state_dir()}; run this with sudo.", file=sys.stderr)
    return 1
