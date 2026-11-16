#!/usr/bin/env python3
"""Fetch Claude, Codex, CommandCode, AGY and Mistral usage as JSON.

Uses the same endpoints the official CLIs use for their own /usage and /status
displays. Tokens are read from the CLIs' local login files and only sent to
the provider that issued them. On failure the last good values are returned
with "stale": true so the panel keeps showing something useful.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

HOME = os.path.expanduser("~")
CACHE_DIR = os.path.join(HOME, ".cache", "usage-meter")
CACHE_FILE = os.path.join(CACHE_DIR, "last.json")
TIMEOUT = 15

OMP_BIN = os.path.join(HOME, ".local", "bin", "omp")


def http_json(url, headers, retries=1, data=None):
    req = urllib.request.Request(url, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r)
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, TimeoutError):
        if retries <= 0:
            raise
        time.sleep(2)
        return http_json(url, headers, retries - 1, data)


def iso_to_epoch(s):
    if not s:
        return None
    # Go timestamps carry nanoseconds; Python only parses up to microseconds.
    s = re.sub(r"(\.\d{6})\d+", r"\1", s.replace("Z", "+00:00"))
    return int(datetime.fromisoformat(s).timestamp())


def window(pct, resets_at):
    return {"pct": round(float(pct or 0), 1), "resets_at": resets_at}


PLAN_NAMES = {"free": "Free", "plus": "Plus", "pro": "Pro", "max": "Max", "team": "Team",
              "business": "Business", "enterprise": "Enterprise", "edu": "Edu"}


def plan_label(raw):
    if not raw:
        return None
    return PLAN_NAMES.get(raw.lower(), raw.replace("_", " ").title())


def jwt_email(token):
    """The "email" claim of a login's id_token (read locally, not verified)."""
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("email")
    except (AttributeError, IndexError, ValueError):
        return None


def claude_account():
    try:
        with open(os.path.join(HOME, ".claude.json")) as f:
            return (json.load(f).get("oauthAccount") or {}).get("emailAddress")
    except (OSError, ValueError):
        return None


def omp_payload(provider):
    """omp's full usage response for one provider."""
    out = subprocess.run([OMP_BIN, "usage", "--json", "--provider", provider],
                         capture_output=True, text=True, timeout=TIMEOUT, check=True).stdout
    return json.loads(out)


def omp_reports(provider):
    """omp's usage reports for one provider; omp keeps its own logins' tokens fresh."""
    return omp_payload(provider).get("reports") or []


def fetch_claude():
    path = os.path.join(HOME, ".claude", ".credentials.json")
    with open(path) as f:
        creds = json.load(f)["claudeAiOauth"]
    tok = creds["accessToken"]
    plan = plan_label(creds.get("subscriptionType"))
    # Max comes in 5x / 20x variants, only visible in the rate-limit tier name.
    mult = re.search(r"(\d+x)", creds.get("rateLimitTier") or "")
    if plan == "Max" and mult:
        plan += " " + mult[1]
    d = http_json(
        "https://api.anthropic.com/api/oauth/usage",
        {
            "Authorization": "Bearer " + tok,
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "usage-meter/1.0",
        },
    )
    fh, wk = d.get("five_hour") or {}, d.get("seven_day") or {}
    out = {
        "plan": plan,
        "account": claude_account(),
        "five_hour": window(fh.get("utilization"), iso_to_epoch(fh.get("resets_at"))),
        "weekly": window(wk.get("utilization"), iso_to_epoch(wk.get("resets_at"))),
    }
    # Claude accounts logged in to omp are listed next to Claude Code's own login.
    accounts = [dict(out, account="%s (Claude Code)" % out["account"] if out["account"] else "Claude Code")]
    try:
        accounts += omp_accounts("anthropic", skip=out["account"])
    except FileNotFoundError:
        pass  # omp not installed
    except Exception as e:
        out["error"] = "omp accounts: " + describe(e)
    if len(accounts) > 1:
        out["accounts"] = accounts
    return out


def omp_accounts(provider, skip=None):
    """omp's logins for a 5h/7d provider as account entries, leaving out the `skip` email."""
    accounts = []
    for r in omp_reports(provider):
        meta = r.get("metadata") or {}
        if meta.get("email") and meta.get("email") == skip:
            continue  # same login as the CLI's own, already shown
        acct = {"account": "%s (omp)" % (meta.get("email") or "?"), "plan": plan_label(meta.get("planType"))}
        for lim in r.get("limits") or []:
            # scope.tier marks a model/tier-specific sublimit; the generic
            # 5h/weekly summaries must use the shared quota, not the last sublimit
            if (lim.get("scope") or {}).get("tier"):
                continue
            key = {"5h": "five_hour", "7d": "weekly"}.get((lim.get("window") or {}).get("id"))
            resets = (lim.get("window") or {}).get("resetsAt")
            if key:
                acct[key] = window(float((lim.get("amount") or {}).get("usedFraction") or 0) * 100,
                                   resets // 1000 if resets else None)
        if "five_hour" not in acct or "weekly" not in acct:
            acct["error"] = "no usage reported"
        accounts.append(acct)
    return accounts


def fetch_codex():
    """The Codex CLI's login (if any) plus every ChatGPT account logged in to omp."""
    accounts, cli, err = [], None, None
    try:
        with open(os.path.join(HOME, ".codex", "auth.json")) as f:
            tokens = json.load(f)["tokens"]
        d = http_json(
            "https://chatgpt.com/backend-api/wham/usage",
            {
                "Authorization": "Bearer " + tokens["access_token"],
                "ChatGPT-Account-Id": tokens["account_id"],
                "User-Agent": "usage-meter/1.0",
            },
        )
        rl = d.get("rate_limit") or {}
        p, s = rl.get("primary_window") or {}, rl.get("secondary_window") or {}
        cli = {
            "plan": plan_label(d.get("plan_type")),
            "account": jwt_email(tokens.get("id_token")),
            "five_hour": window(p.get("used_percent"), p.get("reset_at")),
            "weekly": window(s.get("used_percent"), s.get("reset_at")),
        }
        accounts.append(dict(cli, account="%s (Codex CLI)" % cli["account"] if cli["account"] else "Codex CLI"))
    except FileNotFoundError:
        pass  # Codex CLI not logged in; omp may still have accounts
    except Exception as e:
        err = "Codex CLI: " + describe(e)
    try:
        accounts += omp_accounts("openai-codex", skip=cli and cli["account"])
    except FileNotFoundError:
        pass  # omp not installed
    except Exception as e:
        err = "omp accounts: " + describe(e)
    usable = [a for a in accounts if "five_hour" in a]
    if not usable:
        raise RuntimeError(err) if err else FileNotFoundError("no Codex login")
    # The panel shows the CLI login, else the omp account with the most headroom.
    best = accounts[0] if cli else min(usable, key=lambda a: max(a["five_hour"]["pct"], a["weekly"]["pct"]))
    out = {k: best.get(k) for k in ("plan", "account", "five_hour", "weekly")}
    if cli:
        out["account"] = cli["account"]
    if err:
        out["error"] = err
    if len(accounts) > 1:
        out["accounts"] = accounts
    return out


def fetch_commandcode():
    """Every CommandCode account logged in to omp."""
    accounts = []
    for r in omp_reports("commandcode"):
        meta = r.get("metadata") or {}
        acct = {"account": meta.get("email") or meta.get("accountId") or "?"}
        for lim in r.get("limits") or []:
            wid = (lim.get("window") or {}).get("id") or (lim.get("scope") or {}).get("windowId")
            amt = lim.get("amount") or {}
            key = {"5h": "five_hour", "7d": "weekly"}.get(wid)
            if key:
                resets = (lim.get("window") or {}).get("resetsAt")
                w = window(float(amt.get("usedFraction") or 0) * 100, resets // 1000 if resets else None)
                for f in ("used", "limit", "unit"):
                    if f in amt:
                        w[f] = amt[f]
                acct[key] = w
            elif wid == "balance":
                rem = amt.get("remaining")
                if rem is not None:
                    resets = (lim.get("window") or {}).get("resetsAt")
                    bal = {"remaining": rem, "unit": amt.get("unit") or "credits"}
                    if resets:
                        bal["resets_at"] = resets // 1000
                    acct["balance"] = bal
        if "five_hour" not in acct or "weekly" not in acct:
            acct["error"] = "no usage reported"
        accounts.append(acct)
    usable = [a for a in accounts if "five_hour" in a and "weekly" in a]
    if not usable:
        raise FileNotFoundError("no CommandCode accounts in omp")
    best = min(usable, key=lambda a: max(a["five_hour"]["pct"], a["weekly"]["pct"]))
    out = {k: best[k] for k in ("account", "five_hour", "weekly", "balance") if k in best}
    if len(accounts) > 1:
        out["accounts"] = accounts
    return out


def fetch_mistral():
    payload = omp_payload("mistral")
    reports = [r for r in payload.get("reports") or [] if r.get("provider") == "mistral"]
    credentials = [a for a in payload.get("accountsWithoutUsage") or []
                   if a.get("provider") == "mistral" and a.get("type") == "api_key"]
    if not reports and not credentials:
        raise FileNotFoundError("no Mistral account in omp")
    # ponytail: add quota parsing when omp reports Mistral usage with a known schema.
    return {"account": "API key (omp)",
            "note": "Usage unavailable — omp does not report quota for this Mistral API key."}


def fetch_agy():
    """Read AGY's usage command only; percentages reported by AGY are remaining."""
    binary = os.path.join(HOME, ".local", "bin", "agy")
    if not os.access(binary, os.X_OK):
        binary = shutil.which("agy")
    if not binary:
        raise RuntimeError("AGY CLI not found (~/.local/bin/agy or PATH)")
    try:
        result = subprocess.run([binary, "-p", "/usage"], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=TIMEOUT, check=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError("AGY /usage failed: " + (e.stderr.strip() or "exit %d" % e.returncode)) from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("AGY /usage timed out after %ds" % TIMEOUT) from e
    out = {"groups": []}
    groups = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 4 or any(not f.strip() or f != f.strip() for f in fields):
            raise ValueError("AGY /usage: expected group, label, remaining percentage and ISO reset (TSV)")
        name, label, remaining, reset = fields
        if not re.fullmatch(r"(?:\d+(?:\.\d+)?|\.\d+)%", remaining):
            raise ValueError("AGY /usage: invalid remaining percentage")
        pct = float(remaining[:-1])
        if not 0 <= pct <= 100:
            raise ValueError("AGY /usage: remaining percentage outside 0–100")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", reset):
            raise ValueError("AGY /usage: invalid ISO reset")
        used_label = re.sub(r"\s+remaining$", "", label, flags=re.IGNORECASE) + " (used)"
        quota = dict(window(100 - pct, iso_to_epoch(reset)), label=used_label)
        if name not in groups:
            groups[name] = {"name": name, "quotas": []}
            out["groups"].append(groups[name])
        groups[name]["quotas"].append(quota)
        key = ("five_hour" if "five hour" in label.lower() else
               "weekly" if "weekly" in label.lower() else None)
        if key and (key not in out or quota["pct"] > out[key]["pct"]):
            out[key] = quota
    if not groups:
        raise ValueError("AGY /usage: empty response")
    return out


def describe(e):
    if isinstance(e, urllib.error.HTTPError):
        if e.code in (401, 403):
            return "login expired - open the CLI once to refresh it"
        return "HTTP %d" % e.code
    if isinstance(e, FileNotFoundError):
        return "not logged in (no saved login found)"
    if isinstance(e, urllib.error.URLError):
        return "network error"
    return type(e).__name__ + ": " + str(e)


def main():
    try:
        with open(CACHE_FILE) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}

    fetchers = {"claude": fetch_claude, "codex": fetch_codex, "commandcode": fetch_commandcode,
                "agy": fetch_agy, "mistral": fetch_mistral}
    # `usage_fetch.py claude codex` fetches just those sources (the applet skips hidden ones).
    wanted = [a for a in sys.argv[1:] if a in fetchers] or list(fetchers)
    out = {"updated": int(time.time())}
    for name, fn in ((n, fetchers[n]) for n in wanted):
        try:
            out[name] = fn()
            out[name]["fetched"] = out["updated"]
            cache[name] = out[name]
        except Exception as e:  # keep the panel alive whatever goes wrong
            prev = cache.get(name)
            out[name] = dict(prev, stale=True) if prev else {}
            out[name]["error"] = describe(e)

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
