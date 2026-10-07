# AI Subscription Usage Meter

A Linux Mint **Cinnamon panel applet** that shows how much of your AI coding subscriptions you've used, right in the taskbar.

```
[Claude] 5h 22% · W 29%  │  [Codex] 5h 0% · W 0%  │  [Antigravity] 5h 0% · W 1%  │  [Muse] $0.04
```

| Service | What it shows | Source |
|---|---|---|
| **Claude** (Claude Code / claude.ai plan) | Plan name, 5-hour and weekly usage % | Claude Code's login in `~/.claude/.credentials.json` |
| **Codex** (ChatGPT plan) | Plan name, 5-hour and weekly usage % | Codex's login in `~/.codex/auth.json` |
| **Antigravity** (Google AI Pro, `agy` CLI) | Plan name, 5-hour and weekly usage % per model group (Gemini; Claude & GPT) | `agy`'s login in the GNOME keyring |
| **Muse Code** (Meta, API-billed) | Cost of the currently running session(s) | Muse's local session logs + model catalog prices |

Percentages are colour-coded: 🟢 under 50% · 🟡 50–74% · 🟠 75–89% · 🔴 90%+.
Click the meter for a dropdown with usage bars, reset countdowns, and **Refresh now** / **Settings** buttons.

## Settings

Open them from the dropdown's **Settings** button (or right-click the meter → **Configure…**):

- **Arrange subscriptions**: *Vertical* stacks each subscription in the dropdown; *Horizontal* gives each one its own column side by side.
- **Subscriptions to show**: switch Claude, Codex, Antigravity and Muse on or off. Hidden ones disappear from the panel and dropdown and aren't fetched at all.

## Requirements

- Linux Mint / Cinnamon (tested on Cinnamon 6.6)
- Python 3 (standard library) and `python3-secretstorage` (for Antigravity; preinstalled on Mint)
- Whichever CLIs you want tracked, already logged in: `claude`, `codex`, `agy`, `muse`

Services that aren't installed or logged in just show a ⚠ and are otherwise ignored.

## Install

```bash
git clone https://github.com/lindtseyyy/ai-subscription-usage-meter.git
mkdir -p ~/.local/share/cinnamon/applets
ln -s "$PWD/ai-subscription-usage-meter/usage-meter@lindtseyxvii" ~/.local/share/cinnamon/applets/
```

Then right-click the panel → **Applets** → find **AI Usage Meter** → **Add to panel**.

To check the fetcher on its own:

```bash
python3 usage-meter@lindtseyxvii/usage_fetch.py          # all services, as JSON
python3 usage-meter@lindtseyxvii/usage_fetch.py muse     # just one
```

## How it works

```
usage-meter@lindtseyxvii/
├── applet.js        # Cinnamon applet: panel text, colours, dropdown menu, timers
├── usage_fetch.py   # Fetches usage from each service and prints JSON
├── metadata.json    # Applet metadata
├── settings-schema.json  # Settings page (layout, which subscriptions to show)
└── icons/           # Service logos shown in the panel
```

The applet runs `usage_fetch.py` every **3 minutes** (Muse every **30 seconds**, since it only reads local files) and renders the JSON.

- **Claude**: `GET api.anthropic.com/api/oauth/usage`, the same endpoint Claude Code's `/usage` uses.
- **Codex**: `GET chatgpt.com/backend-api/wham/usage`, the same endpoint Codex's `/status` uses.
- **Antigravity**: `POST daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary`, the data behind `agy`'s *Models & Quota* screen. Google access tokens expire hourly, so when the keyring token is stale the script refreshes it with `agy`'s own OAuth client (read from the `agy` binary at runtime). The new token is kept only in `~/.cache/usage-meter/` (mode 600). The keyring is never modified.
- **Muse Code**: finds running sessions by looking for live `muse` processes holding a session's `.session.lock`, sums the token usage in that session's `session.jsonl` (the same totals as Muse's `/usage`), and prices it with Muse's own model catalog.

If a fetch fails, the last good values stay on screen, dimmed, with a ⚠. Hover or open the menu to see why.

## Caveats

- **Unofficial endpoints.** None of these are public APIs; they're what the official CLIs call themselves. They work today but may change without notice.
- **Tokens stay local.** Credentials are read from each tool's own login storage and only sent to the provider that issued them. Nothing is sent anywhere else.
- **Expired logins.** If Claude or Codex hasn't been used in a while its token may expire. Open the CLI once and the meter recovers.
- **Muse cost is an estimate**, computed from token counts at catalog prices, matching what `/usage` reports. Approval-review calls aren't part of the session total, so your actual bill can be slightly higher. Check Meta's Accounts Center for real billing.

## Uninstall

Right-click the meter → **Remove from panel**, then:

```bash
rm ~/.local/share/cinnamon/applets/usage-meter@lindtseyxvii
rm -rf ~/.cache/usage-meter
```

## Disclaimer

Not affiliated with Anthropic, OpenAI, Google or Meta. Logos in `icons/` are trademarks of their respective owners and are included only to identify each service.
