#!/usr/bin/env python3
"""Deterministic tests for provider usage outcomes."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usage_fetch


def report(email, limits):
    return {"metadata": {"email": email, "planType": "max"}, "limits": limits}


SHARED_100 = {"window": {"id": "7d", "resetsAt": 1763000000000}, "amount": {"usedFraction": 1.0}}
TIER_0 = {"window": {"id": "7d", "resetsAt": 1763000000000},
          "scope": {"tier": "fable"}, "amount": {"usedFraction": 0.0}}


class OmpAccountsTest(unittest.TestCase):
    def run_accounts(self, reports):
        orig = usage_fetch.omp_reports
        usage_fetch.omp_reports = lambda provider: reports
        try:
            return usage_fetch.omp_accounts("anthropic")
        finally:
            usage_fetch.omp_reports = orig

    def test_shared_weekly_wins_over_later_tier(self):
        # shared 100% first, tier 0% after: must not be overwritten
        [acct] = self.run_accounts([report("a@x.com", [SHARED_100, TIER_0])])
        self.assertEqual(acct["weekly"]["pct"], 100.0)

    def test_shared_weekly_wins_over_earlier_tier(self):
        # tier 0% first, shared 100% after: shared must win
        [acct] = self.run_accounts([report("a@x.com", [TIER_0, SHARED_100])])
        self.assertEqual(acct["weekly"]["pct"], 100.0)

    def test_tier_scoped_five_hour_ignored(self):
        lim5 = {"window": {"id": "5h", "resetsAt": 1762900000000}, "amount": {"usedFraction": 0.25}}
        [acct] = self.run_accounts([report("a@x.com", [lim5, dict(TIER_0, window={"id": "5h"})])])
        self.assertEqual(acct["five_hour"]["pct"], 25.0)

    def test_reports_without_scope_still_work(self):
        # Codex-style limits carry no scope at all
        lim = {"window": {"id": "7d", "resetsAt": 1763000000000}, "amount": {"usedFraction": 0.42}}
        [acct] = self.run_accounts([report("c@x.com", [lim])])
        self.assertEqual(acct["weekly"]["pct"], 42.0)
        self.assertEqual(acct["account"], "c@x.com (omp)")


class AgyTest(unittest.TestCase):
    def fetch(self, text):
        with patch.object(usage_fetch.os, "access", return_value=True), \
                patch.object(usage_fetch.subprocess, "run") as run:
            run.return_value.stdout = text
            result = usage_fetch.fetch_agy()
            run.assert_called_once_with(
                [os.path.join(usage_fetch.HOME, ".local", "bin", "agy"), "-p", "/usage"],
                stdin=usage_fetch.subprocess.DEVNULL, capture_output=True, text=True,
                timeout=usage_fetch.TIMEOUT, check=True)
            return result

    def test_groups_inversion_resets_and_independent_worst_windows(self):
        result = self.fetch(
            "Gemini Models\tWeekly Limit Remaining\t80%\t2026-11-23T09:02:13Z\n"
            "Gemini Models\tFive Hour Limit Remaining\t12.5%\t2026-11-16T14:02:13Z\n"
            "Claude and GPT models\tWeekly Limit Remaining\t30%\t2026-11-23T10:02:13+01:00\n"
            "Claude and GPT models\tFive Hour Limit Remaining\t90%\t2026-11-16T14:02:13Z\n"
            "Gemini 4 Argon\tGemini 4 Argon Weekly Limit\t5%\t2026-11-23T09:02:13Z\n")
        self.assertEqual([g["name"] for g in result["groups"]],
                         ["Gemini Models", "Claude and GPT models", "Gemini 4 Argon"])
        self.assertEqual([len(g["quotas"]) for g in result["groups"]], [2, 2, 1])
        self.assertEqual(result["five_hour"]["pct"], 87.5)
        self.assertEqual(result["weekly"]["pct"], 95)
        self.assertEqual(result["weekly"]["label"], "Gemini 4 Argon Weekly Limit (used)")
        self.assertEqual(result["weekly"]["resets_at"], 1795424533)
        self.assertEqual(result["groups"][1]["quotas"][0]["resets_at"], 1795424533)
        self.assertEqual(result["groups"][0]["quotas"][0]["label"], "Weekly Limit (used)")

    def test_missing_windows_are_not_fabricated(self):
        for label, present, absent in [
                ("Weekly Limit Remaining", "weekly", "five_hour"),
                ("Five Hour Limit Remaining", "five_hour", "weekly"),
                ("Other Limit", None, "weekly")]:
            with self.subTest(label=label):
                result = self.fetch("Group\t%s\t75%%\t2026-11-23T09:02:13Z\n" % label)
                self.assertNotIn(absent, result)
                if present:
                    self.assertEqual(result[present]["pct"], 25)
                else:
                    self.assertNotIn("five_hour", result)
                self.assertEqual(result["groups"][0]["quotas"][0]["pct"], 25)

    def test_rejects_empty_malformed_and_invalid_values(self):
        for text in ["", "\n", "Group\tWeekly\t100%",
                     "\tWeekly\t100%\t2026-11-23T09:02:13Z",
                     "Group\tWeekly\t100%\tnot-a-date",
                     "Group\tWeekly\t100%\t2026-11-23",
                     "Group\tWeekly\t100%\t2026-99-23T09:02:13Z"] + [
                         "Group\tWeekly\t%s\t2026-11-23T09:02:13Z" % pct
                         for pct in ("", "no", "NaN%", "inf%", "-1%", "101%", "50", "50%%")]:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    self.fetch(text)
        with self.assertRaises(ValueError):
            self.fetch("Group\tWeekly\t100%\t2026-11-23T09:02:13Z\ninvalid")

    def test_path_fallback_and_command_failure(self):
        with patch.object(usage_fetch.os, "access", return_value=False), \
                patch.object(usage_fetch.shutil, "which", return_value="/usr/bin/agy"), \
                patch.object(usage_fetch.subprocess, "run", side_effect=
                             usage_fetch.subprocess.CalledProcessError(2, "agy", stderr="login required\n")) as run:
            with self.assertRaisesRegex(RuntimeError, "AGY /usage failed: login required"):
                usage_fetch.fetch_agy()
            self.assertEqual(run.call_args.args[0], ["/usr/bin/agy", "-p", "/usage"])

    def test_missing_cli_and_timeout_are_clear(self):
        with patch.object(usage_fetch.os, "access", return_value=False), \
                patch.object(usage_fetch.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "AGY CLI not found"):
                usage_fetch.fetch_agy()
        with patch.object(usage_fetch.os, "access", return_value=True), \
                patch.object(usage_fetch.subprocess, "run", side_effect=
                             usage_fetch.subprocess.TimeoutExpired("agy", usage_fetch.TIMEOUT)):
            with self.assertRaisesRegex(RuntimeError, "AGY /usage timed out"):
                usage_fetch.fetch_agy()


class MistralTest(unittest.TestCase):
    def fetch(self, payload):
        with patch.object(usage_fetch, "omp_payload", return_value=payload):
            return usage_fetch.fetch_mistral()

    def test_api_key_without_usage_is_connected_without_quotas(self):
        result = self.fetch({
            "reports": [],
            "accountsWithoutUsage": [{"provider": "mistral", "type": "api_key"}],
            "disabledCredentials": [], "capacity": {},
        })
        self.assertEqual(result, {
            "account": "API key (omp)",
            "note": "Usage unavailable — omp does not report quota for this Mistral API key.",
        })

    def test_absent_or_unrelated_login_is_not_logged_in(self):
        for payload in ({}, {"accountsWithoutUsage": [
                {"provider": "other", "type": "api_key"},
                {"provider": "mistral", "type": "oauth"}]},
                {"reports": [{"provider": "other"}]}):
            with self.subTest(payload=payload), self.assertRaises(FileNotFoundError) as error:
                self.fetch(payload)
            self.assertEqual(usage_fetch.describe(error.exception),
                             "not logged in (no saved login found)")

    def test_unsupported_report_does_not_invent_quota(self):
        result = self.fetch({"reports": [{"provider": "mistral", "limits": []}]})
        self.assertEqual(result["account"], "API key (omp)")
        self.assertIn("Usage unavailable", result["note"])
        self.assertNotIn("quotas", result)
        self.assertNotIn("five_hour", result)
        self.assertNotIn("weekly", result)


if __name__ == "__main__":
    unittest.main()
