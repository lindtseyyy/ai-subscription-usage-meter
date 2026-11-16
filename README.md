# AI Subscription Usage Meter

A Linux Mint **Cinnamon panel applet** with one taskbar icon. Click it or press **Alt+U** to toggle a right-edge usage sidebar.

Providers have separate padded background cards and highlighted headings, with the plan beside the provider name. Accounts and model groups stack vertically inside them in a scrollable list. The sidebar opens on the first non-primary (extended) monitor when available, otherwise the primary monitor, and fits its work area without covering its panels. Close it with **Escape**, its close button, a click outside, or the shortcut again.

| Service | What it shows | Source |
|---|---|---|
| **Claude** (Claude Code / claude.ai plan) | Plan name, logged-in account, 5-hour and weekly usage %, plus every Claude account logged in to omp | Claude Code's login in `~/.claude/.credentials.json` (account from `~/.claude.json`), and `omp usage --json --provider anthropic` |
| **Codex** (ChatGPT plan) | Plan name, logged-in account, 5-hour and weekly usage % | Codex's login in `~/.codex/auth.json` |
| **CommandCode** (via `omp`) | Logged-in account, 5-hour and weekly usage %, credit limits, and credit balance | `omp usage --json --provider commandcode` |
| **Antigravity** | Every model group's quota and reset, including weekly-only limits | `~/.local/bin/agy -p /usage` (or `agy` on PATH) |
| **Mistral** (via `omp`) | Account status; an explicit unavailable message when omp cannot report usage | `omp usage --json --provider mistral` |

Percentages are colour-coded: 🟢 under 50% · 🟡 50–74% · 🟠 75–89% · 🔴 90%+.
The sidebar shows accounts, usage bars and reset countdowns, with **Refresh now** and **Settings** icon buttons beside **AI Usage Meter** at the top, outside the scrolling list. Hover for their labels.
The update timestamp (or refreshing status) sits just below the top toolbar, above the scrolling provider cards.
CommandCode shows credits used against each limit and a separate remaining balance. omp does not report a monthly limit.
Antigravity keeps model groups separate. Its remaining percentages are converted to **used** percentages; missing windows are omitted.
The Antigravity logo is bundled from the installed application's icon and appears beside its heading.
Mistral API keys currently appear in omp as **no usage data**. The card shows that limitation instead of inventing a usage percentage, plan or reset time.

## Settings

Open them from the sidebar's **Settings** button (or right-click the taskbar icon → **Configure…**):

- **Keyboard shortcut**: defaults to **Alt+U**; change it or clear it to disable the global shortcut. A global binding takes precedence over an application's Alt+U menu shortcut while the applet is enabled. If Cinnamon cannot register the shortcut, use the icon and choose another binding.
- **Subscriptions to show**: switch Claude, Codex, CommandCode, Antigravity and Mistral on or off. Hidden ones disappear from the sidebar and aren't fetched at all.

## Requirements

- Linux Mint / Cinnamon (tested on Cinnamon 6.6)
- Python 3 (standard library)
- Whichever CLIs you want tracked, already logged in: `claude`, `codex`, `omp` (with CommandCode accounts added via `omp login`), `agy`

Services that aren't installed or logged in show a warning in the sidebar.

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
python3 usage-meter@lindtseyxvii/usage_fetch.py codex    # just one
```

## How it works

```
usage-meter@lindtseyxvii/
├── applet.js        # Icon launcher, right-edge sidebar, shortcut and refresh timer
├── usage_fetch.py   # Fetches usage from each service and prints JSON
├── metadata.json    # Applet metadata
├── settings-schema.json  # Keyboard shortcut and subscriptions to show
└── icons/           # Service logos shown in the sidebar
```

The applet runs `usage_fetch.py` every **3 minutes** and renders the JSON.

- **Claude**: `GET api.anthropic.com/api/oauth/usage`, the same endpoint Claude Code's `/usage` uses.
  omp's Claude accounts come from `omp usage --json --provider anthropic`; one with the same email as Claude Code's login isn't listed twice.
- **Codex**: `GET chatgpt.com/backend-api/wham/usage`, the same endpoint Codex's `/status` uses.
- **CommandCode**: runs `~/.local/bin/omp usage --json --provider commandcode`, which reports the quota and credit balance of every CommandCode account logged in to omp.
- **Antigravity**: runs only `~/.local/bin/agy -p /usage`, falling back to `agy` on PATH; no model prompts are sent. Strict tab-separated usage rows supply model group, quota label, remaining percentage and ISO reset time. Empty or malformed output is an error, not zero usage.
- **Mistral**: queries omp's Mistral provider. API-key accounts without reported quotas show their connection status and an unavailable message; no credentials are read or displayed by the applet.
- **Accounts**: read locally from each login: Claude Code's `~/.claude.json`, the `email` claim of the id token Codex stores with its login, and the email omp reports for each CommandCode account. Nothing extra is fetched.

If a fetch fails, the last good values remain available with a warning and the failure reason in the sidebar.

## Caveats

- **Unofficial endpoints.** None of these are public APIs; they're what the official CLIs call themselves. They work today but may change without notice.
- **Tokens stay local.** Credentials are read from each tool's own login storage and only sent to the provider that issued them. Nothing is sent anywhere else.
- **Expired logins.** If Claude or Codex hasn't been used in a while its token may expire. Open the CLI once and the meter recovers.
- **Shared weekly limits.** Claude/Codex 5h/weekly percentages are the shared account quotas; model- or tier-specific sublimits (e.g. omp's per-tier `scope.tier` windows) are excluded. Antigravity instead preserves every model-group limit.

## Uninstall

Right-click the meter → **Remove from panel**, then:

```bash
rm ~/.local/share/cinnamon/applets/usage-meter@lindtseyxvii
rm -rf ~/.cache/usage-meter
```

## Disclaimer

Not affiliated with Anthropic, OpenAI, CommandCode, Google or Mistral. Logos in `icons/` are trademarks of their respective owners and are included only to identify each service.
