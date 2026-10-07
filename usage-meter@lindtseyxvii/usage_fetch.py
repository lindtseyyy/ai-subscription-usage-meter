#!/usr/bin/env python3
"""Fetch Claude, Codex and Antigravity 5-hour / weekly usage, plus Muse API spend, as JSON.

Uses the same endpoints the official CLIs use for their own /usage and /status
displays. Tokens are read from the CLIs' local login files and only sent to
the provider that issued them. On failure the last good values are returned
with "stale": true so the panel keeps showing something useful.
"""
import glob
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime

HOME = os.path.expanduser("~")
CACHE_DIR = os.path.join(HOME, ".cache", "usage-meter")
CACHE_FILE = os.path.join(CACHE_DIR, "last.json")
TIMEOUT = 15

AGY_BIN = os.path.join(HOME, ".local", "bin", "agy")
AGY_PROJECT_FILE = os.path.join(HOME, ".gemini", "antigravity-cli", "cache", "default_project_id.txt")
AGY_TOKEN_CACHE = os.path.join(CACHE_DIR, "agy_token.json")
AGY_API = "https://daily-cloudcode-pa.googleapis.com/v1internal:"


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
    return {
        "plan": plan,
        "five_hour": window(fh.get("utilization"), iso_to_epoch(fh.get("resets_at"))),
        "weekly": window(wk.get("utilization"), iso_to_epoch(wk.get("resets_at"))),
    }


def fetch_codex():
    path = os.path.join(HOME, ".codex", "auth.json")
    with open(path) as f:
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
    return {
        "plan": plan_label(d.get("plan_type")),
        "five_hour": window(p.get("used_percent"), p.get("reset_at")),
        "weekly": window(s.get("used_percent"), s.get("reset_at")),
    }


def write_private(path, obj):
    os.makedirs(CACHE_DIR, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)


def agy_keyring_token():
    import secretstorage

    col = secretstorage.get_default_collection(secretstorage.dbus_init())
    if col.is_locked():
        raise RuntimeError("keyring is locked")
    item = next(col.search_items({"service": "gemini", "username": "antigravity"}), None)
    if item is None:
        raise FileNotFoundError("no agy login in keyring")
    return json.loads(item.get_secret())["token"]


def agy_refresh(refresh_token, cached):
    """Get a fresh access token using agy's own OAuth client.

    Google doesn't rotate refresh tokens, so this never disturbs agy's login;
    the new access token is only kept in our private cache file.
    """
    pairs = [cached["client"]] if cached.get("client") else []
    if not pairs:
        with open(AGY_BIN, "rb") as f:
            blob = f.read()
        ids = set(re.findall(rb"\d{6,}-[a-z0-9]{20,}\.apps\.googleusercontent\.com", blob))
        secrets = set(re.findall(rb"GOCSPX-[A-Za-z0-9_-]{28}", blob))
        pairs = [[i.decode(), s.decode()] for i in ids for s in secrets]
    for cid, secret in pairs:
        body = urllib.parse.urlencode({
            "client_id": cid, "client_secret": secret,
            "refresh_token": refresh_token, "grant_type": "refresh_token",
        }).encode()
        try:
            r = http_json("https://oauth2.googleapis.com/token", {}, data=body)
        except urllib.error.HTTPError as e:
            if e.code in (400, 401):
                continue
            raise
        return r["access_token"], int(time.time()) + int(r.get("expires_in", 3600)), [cid, secret]
    raise RuntimeError("could not refresh agy login - run agy once")


def agy_access_token():
    tok = agy_keyring_token()
    now = time.time()
    if (iso_to_epoch(tok.get("expiry")) or 0) > now + 120:
        return tok["access_token"]

    # Keyed to the refresh token so a re-login under another account isn't mixed up.
    rt_hash = hashlib.sha256(tok["refresh_token"].encode()).hexdigest()
    try:
        with open(AGY_TOKEN_CACHE) as f:
            cached = json.load(f)
    except (OSError, ValueError):
        cached = {}
    if cached.get("rt") == rt_hash and cached.get("expires", 0) > now + 120:
        return cached["access_token"]

    access, expires, client = agy_refresh(tok["refresh_token"], cached)
    write_private(AGY_TOKEN_CACHE, {"rt": rt_hash, "access_token": access, "expires": expires, "client": client})
    return access


def agy_plan(headers):
    """Subscription name from the account info agy loads at startup, e.g. "Pro"."""
    d = http_json(AGY_API + "loadCodeAssist", headers,
                  data=json.dumps({"metadata": {"ideType": "ANTIGRAVITY", "pluginType": "GEMINI"}}).encode())
    paid = (d.get("paidTier") or {}).get("name")
    if paid:
        return re.sub(r"^Google AI\s+", "", paid)  # "Google AI Pro" -> "Pro"
    return "Free" if (d.get("currentTier") or {}).get("id") == "free-tier" else None


def fetch_agy():
    try:
        with open(AGY_PROJECT_FILE) as f:
            body = {"project": f.read().strip()}
    except OSError:
        body = {}
    headers = {
        "Authorization": "Bearer " + agy_access_token(),
        "Content-Type": "application/json",
        "User-Agent": "antigravity",  # the endpoint answers 403 without it
    }
    d = http_json(AGY_API + "retrieveUserQuotaSummary", headers, data=json.dumps(body).encode())
    groups = []
    for g in d.get("groups") or []:
        grp = {"name": g.get("displayName", "?")}
        for b in g.get("buckets") or []:
            key = {"5h": "five_hour", "weekly": "weekly"}.get(b.get("window"))
            if key:
                used = (1 - float(b.get("remainingFraction", 1))) * 100
                grp[key] = window(used, iso_to_epoch(b.get("resetTime")))
        if "five_hour" in grp and "weekly" in grp:
            groups.append(grp)
    if not groups:
        raise RuntimeError("no quota groups in response")
    # The panel shows the Gemini group; every group is listed in the menu.
    main = next((g for g in groups if g["name"].lower().startswith("gemini")), groups[0])
    try:
        plan = agy_plan(headers)
    except Exception:  # the plan is a nice-to-have; never lose the quota over it
        plan = None
    return {"plan": plan, "five_hour": main["five_hour"], "weekly": main["weekly"], "groups": groups}


MUSE_DIR = os.path.join(HOME, ".local", "share", "muse")
MUSE_SETTINGS = os.path.join(HOME, ".config", "muse", "settings.json")
MUSE_LOCK_RE = re.compile(r"/sessions/\d{4}/\d\d/\d\d/([0-9a-f-]{36})/\.session\.lock$")


def muse_prices():
    """Per-1M-token USD prices by model id, from Muse's own model catalog."""
    prices = {}
    for path in glob.glob(os.path.join(MUSE_DIR, "model-catalog", "*.json")):
        with open(path) as f:
            for row in json.load(f).get("rows", []):
                c = row.get("cost") or {}
                if c.get("input") is not None:
                    prices[row["model_id"]] = tuple(float(c.get(k) or 0) for k in ("input", "output", "cached"))
    return prices


def muse_running_sessions():
    """{session_dir: {"pid", "cwd"}} for sessions a live Muse process holds the lock of."""
    found = {}
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            if not os.path.basename(os.readlink(f"/proc/{pid}/exe")).startswith("muse"):
                continue
            cwd = os.readlink(f"/proc/{pid}/cwd")
            fds = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if MUSE_LOCK_RE.search(target):
                found[os.path.dirname(target)] = {"pid": int(pid), "cwd": cwd}
    return found


def muse_session_usage(session_dir, default_model):
    """Sum the session's own model calls, the same totals Muse's /usage reports.

    Only session.jsonl counts: approval reviews live in side logs and aren't
    part of the session total. Compaction calls are logged as attributions.
    """
    n = inp = cached = outp = 0
    model, started = default_model, None
    with open(os.path.join(session_dir, "session.jsonl"), errors="replace") as f:
        for line in f:
            if started is None:
                try:
                    started = int(json.loads(line).get("recorded_at", 0) / 1e6) or None
                except ValueError:
                    pass
            if '"model_completed"' not in line and '"compaction"' not in line:
                continue
            try:
                ev = (json.loads(line).get("payload") or {}).get("event") or {}
            except ValueError:
                continue
            if ev.get("kind") == "model_completed" and ev.get("usage"):
                model = ev.get("model") or model
                u = ev["usage"]
            elif ev.get("kind") == "goal_usage_attribution" and (ev.get("record") or {}).get("usage_family") == "compaction":
                u = ev["record"].get("quantity") or {}
            else:
                continue
            n += 1
            inp += int(u.get("input_tokens") or 0)
            cached += int(u.get("cached_tokens") or u.get("cache_read_tokens") or 0)
            outp += int(u.get("output_tokens") or 0)
    return {"calls": n, "input": inp, "cached": cached, "output": outp, "model": model, "started": started}


def fetch_muse():
    try:
        with open(MUSE_SETTINGS) as f:
            default_model = json.load(f).get("model") or "unknown"
    except (OSError, ValueError):
        default_model = "unknown"
    prices = muse_prices()
    sessions = []
    for sdir, proc in muse_running_sessions().items():
        u = muse_session_usage(sdir, default_model)
        p = prices.get(u["model"])
        # input_tokens includes the cached part, which is billed at the cached rate.
        u["usd"] = None if p is None else round(
            ((u["input"] - u["cached"]) * p[0] + u["cached"] * p[2] + u["output"] * p[1]) / 1e6, 4)
        u.update(proc, id=os.path.basename(sdir))
        sessions.append(u)
    sessions.sort(key=lambda x: x["started"] or 0, reverse=True)
    return {"sessions": sessions}


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

    fetchers = {"claude": fetch_claude, "codex": fetch_codex, "agy": fetch_agy, "muse": fetch_muse}
    # `usage_fetch.py muse` refreshes just that source (the applet polls Muse more often).
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
