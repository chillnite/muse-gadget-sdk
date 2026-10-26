# Gadget Hub: Muse gadgets with Claude

A small server you run yourself (on a Raspberry Pi, a PC or a VPS) that takes
the place of the Muse cloud: gadgets pair with it and connect to it, and
Claude, through the Claude API, uses their commands as tools. You chat with
Claude in the hub's web chat or from the command line, and gadgets can send
Claude messages too.

```
 you ──web chat / gadgethub chat──▶ ┌──────────────┐ ──Claude API──▶ Claude
                                    │  gadget hub  │
 gadgets ◀──Noise over WebSocket──▶ └──────────────┘
```

The hub speaks the protocol the gadgets already use (`/fetch_vms`,
`/device_token/refresh`, Noise XX over `/v1/noise`, `/link-control`,
`/chat/stream`, `/chat/subscribe`), so the gadget firmware and the Linux
client need no changes beyond where they connect and how they pair.

## What works so far

| | |
|---|---|
| Linux gadgets (`../linux`) | Pair with `musegadget pair-hub`; Claude can run their commands (`system.run`, `file.read`, `file.write`, `device.health`). `musegadget send-user-msg` reaches Claude. |
| Chat | Web chat at the hub's address, `gadgethub chat`, and messages from gadgets. One main conversation, plus side chats that gadgets name. |
| ESP32 gadgets (`../esp32`) | Not yet: they pair over Bluetooth through the Muse app, and the hub has no stand-in for the app's side of that pairing yet. |
| Voice | Not yet. Claude takes text and images, not audio, so voice notes need a speech-to-text step and spoken replies a text-to-speech one. A voice note gets a reply saying so. |

Conversations live in memory and start fresh when the hub restarts. Paired
gadgets are kept.

## Set up

You need Python 3.10 or later, [uv](https://docs.astral.sh/uv/), and a Claude
API key from the [Claude Console](https://console.anthropic.com/). Check that
the Claude API is available where you are.

```sh
cd hub
export ANTHROPIC_API_KEY=sk-ant-…
export GADGETHUB_STATE_DIR=~/.local/share/gadgethub   # default: /var/lib/gadgethub
uv run --with . gadgethub serve --public-url https://hub.example.com
```

`--public-url` is the address gadgets reach the hub at. It must be `https://`:
gadgets only connect over TLS and check the certificate. The hub listens on
`127.0.0.1:8080` by default, for a reverse proxy or tunnel in front of it (see
below). To serve TLS itself, pass `--tls-cert` and `--tls-key` with `--host
0.0.0.0 --port 443`.

Other options: `--model` (default `claude-opus-5-5`) and `--effort` (`low` to
`max`, default `medium`). The hub opts in to server-side fallback (a request
Claude's safeguards decline is retried on a fallback model) and to
compaction for long conversations.

### Reaching the hub over TLS

Any of these works; pick one:

- **Tailscale Funnel**: `tailscale funnel 8080` gives you
  `https://<machine>.<tailnet>.ts.net` with a trusted certificate. Use that as
  `--public-url`.
- **Cloudflare Tunnel**: point a tunnel at `http://localhost:8080`.
  WebSockets pass through.
- **Caddy** with your own domain: `reverse_proxy localhost:8080`.

The Linux client accepts any port (`https://host:8443`). ESP32 boards will
need port 443 and a certificate from a public CA, so a tunnel or a proxy on
443 is the way to go if you plan to add them.

## Pair a Linux gadget

On the hub:

```sh
gadgethub pair-code        # prints a one-time code, valid for 10 minutes
```

On the gadget, after installing `../linux` (`bash install.sh --from . --no-pair`):

```sh
sudo musegadget pair-hub https://hub.example.com ABCD-EF01-2345 --name kitchen-pi
sudo systemctl restart musegadget
```

`gadgethub devices` lists paired gadgets and their commands, and
`gadgethub remove NODE_ID` unpairs one.

## Chat

Open the hub's address in a browser and paste the admin token, which is in
`$GADGETHUB_STATE_DIR/admin_token`. Or, on the hub:

```sh
gadgethub chat                          # interactive
gadgethub chat "is the kitchen pi low on disk?"
```

The admin commands talk to the hub at `http://127.0.0.1:8080`; pass `--hub`
or set `GADGETHUB_URL` if it listens elsewhere.

## Security

- Claude can do whatever a gadget's commands allow. On a Linux gadget,
  `system.run` is a shell as the account the gadget runs commands as
  (`--run-as`), so pick that account accordingly.
- The admin token opens the chat and the device list. Keep it private, and
  don't run the hub where other people can read its state directory.
- Gadgets authenticate with tokens the hub issued at pairing. The hub stores
  only their hashes. Pairing codes are single-use and expire after 10
  minutes.

## Develop

```sh
uv run --with pytest --with . pytest
```

`tests/test_end_to_end.py` runs the real Linux client (`../linux`) against the
real server, with a scripted stand-in for Claude, so it needs no API key.

| Module | Role |
|---|---|
| `server.py` | aiohttp routes: the gadget API, the Noise WebSocket, the admin API and the web chat |
| `link.py` | One gadget's Noise session: handshake, streams, `link.register`, `link.invoke` |
| `agent.py` | The Claude tool-use loop; each connected gadget's commands become tools |
| `chat.py` | Conversations, turns and the chat events gadgets and the web chat follow |
| `store.py` | Paired gadgets, token hashes, pairing codes and the admin token |
