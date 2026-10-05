# unstream-proxy

Some Anthropic-compatible API relays are broken. They don't stream responses
properly, tool calls come back garbled, and the token counts they report are
inflated enough to wreck your client's context window. `unstream-proxy` sits
between your client (Claude Code, for example) and the relay, and fixes that.

It's a single Python file with no dependencies. If you have Python, you can run it.

## What it fixes

- Streaming: rebuilds broken or missing streams into a proper Anthropic event stream
- Tool calls: routes them natively where the relay supports it, text fallback otherwise
- Token counts: corrects the relay's inflated numbers so your context window stays honest
- Your API key passes straight through, so nothing secret is stored

## Requirements

- Python 3.8 or newer (check with `python3 --version`)
- An upstream relay URL and a working API key for it

## Setup

**1. Get the code**

```bash
git clone https://github.com/Fanorisky/unstream-proxy.git
cd unstream-proxy
```

**2. Point it at your relay**

Open `config.json` and set `TARGET_URL` to your relay's base URL:

```json
"TARGET_URL": "https://your-relay-url.example.com"
```

Leave `API_KEY` empty. Your client sends its own key and the proxy forwards it.

**3. Start it**

The simplest way, which runs until you press Ctrl+C:

```bash
python3 Proxy.py
```

You should see:

```
proxy listening on http://127.0.0.1:8181 -> https://your-relay-url.example.com
```

The proxy is now running on port `8181`.

## Connect your client

Tell your client to talk to the proxy instead of the relay directly. For Claude
Code, add this to `~/.claude/settings.json`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8181",
    "ANTHROPIC_MODEL": "claude-opus-4-8",
    "ANTHROPIC_API_KEY": "your-real-relay-key"
  }
}
```

- `ANTHROPIC_BASE_URL` is the proxy's address. Keep it as is.
- `ANTHROPIC_MODEL` is whatever model name your relay serves.
- `ANTHROPIC_API_KEY` is your real key for the relay. The proxy forwards it.

Restart your client and you're done.

## Keep it running in the background (optional)

If you want the proxy to stay up and restart on reboot, use [pm2](https://pm2.keymetrics.io/):

```bash
npm install -g pm2       # one-time
pm2 start config.json    # start
pm2 logs anthropic-proxy # watch logs
pm2 stop anthropic-proxy # stop
```

## Options

Set these as environment variables, or in `config.json` under `env`:

| variable | default | meaning |
| --- | --- | --- |
| `PORT` | `8181` | port the proxy listens on |
| `HOST` | `127.0.0.1` | bind address (keep it local unless you know what you're doing) |
| `TARGET_URL` | *(none)* | your relay's base URL |
| `API_KEY` | *(empty)* | optional fallback key, used only if the client sends none |

## Troubleshooting

- **`401` or "invalid key"**: the key in your client config is wrong or empty. The
  proxy forwards whatever the client sends, so that key has to be valid for the relay.
- **Everything goes to the wrong server**: you forgot to change `TARGET_URL`.
- **Client ignores the proxy**: `env` in `settings.json` overrides your shell, so
  make sure `ANTHROPIC_BASE_URL` there points at `http://127.0.0.1:8181`.

## Security

The proxy has no authentication of its own. Keep `HOST` set to `127.0.0.1` so only
your own machine can reach it. Anyone who can reach the port can use your relay key.
