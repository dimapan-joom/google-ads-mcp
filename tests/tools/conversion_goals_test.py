# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for set_campaign_custom_conversion_goals.

The request is built from real Google Ads protos; only GAQL reads and the
mutate call are mocked.
"""

import unittest
from unittest import mock

from fastmcp.exceptions import ToolError
from google.api_core.exceptions import ServiceUnavailable
from google.auth.credentials import AnonymousCredentials
from google.ads.googleads.client import GoogleAdsClient

from ads_mcp.tools import conversion_goals

CUSTOMER = "1234567890"
PREFIX = f"customers/{CUSTOMER}/customConversionGoals"
TARGET = f"{PREFIX}/900"


def config(campaign_id, goal, status="ENABLED", level="CAMPAIGN"):
    return {
        "campaign.id": int(campaign_id),
        "campaign.name": f"Campaign {campaign_id}",
        "campaign.status": status,
        "conversion_goal_campaign_config.resource_name": (
            f"customers/{CUSTOMER}/conversionGoalCampaignConfigs/{campaign_id}"
        ),
        "conversion_goal_campaign_config.custom_conversion_goal": goal,
        "conversion_goal_campaign_config.goal_config_level": level,
    }


class SetCampaignCustomConversionGoalsTest(unittest.TestCase):
    def setUp(self):
        self.client = GoogleAdsClient(
            credentials=AnonymousCredentials(),
            developer_token="t",
            use_proto_plus=True,
        )
        self.service = mock.MagicMock()
        self.client.get_service = mock.MagicMock(return_value=self.service)
        self.target_goal = [{
            "custom_conversion_goal.name": "Purchases",
            "custom_conversion_goal.status": "ENABLED",
            "custom_conversion_goal.conversion_actions": ["a/1"],
        }]
        self.before = [config("11", f"{PREFIX}/100"), config("22", TARGET)]
        self.after = [config("11", TARGET), config("22", TARGET)]
        self.biddable = []
        self.config_reads = 0
        patches = [
            mock.patch.object(
                conversion_goals.utils,
                "get_googleads_client",
                return_value=self.client,
            ),
            mock.patch.object(
                conversion_goals, "_run_query", side_effect=self.fake_query
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def fake_query(self, customer_id, sql, login_customer_id=None):
        self.assertEqual(customer_id, CUSTOMER)
        if "FROM custom_conversion_goal" in sql:
            return self.target_goal
        if "FROM campaign_conversion_goal" in sql:
            return self.biddable
        self.config_reads += 1
        reads = self.after if self.config_reads > 1 else self.before
        if isinstance(reads, Exception):
            raise reads
        return reads

    def call(self, **kwargs):
        args = {
            "customer_id": CUSTOMER,
            "custom_conversion_goal_id": "900",
            "expected_goals": {"11": "100", "22": "900"},
        }
        args.update(kwargs)
        return conversion_goals.set_campaign_custom_conversion_goals(**args)

    def request(self):
        call = self.service.mutate_conversion_goal_campaign_configs.call_args
        return call.kwargs["request"]

    def test_rejects_bad_ids_and_sizes(self):
        for kwargs in (
            {"expected_goals": {}},
            {"expected_goals": {str(i): "1" for i in range(1, 102)}},
            {"customer_id": "123-456"},
            {"expected_goals": {"11": "0100"}},
            {"login_customer_id": "x"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ToolError):
                self.call(**kwargs)
        self.service.mutate_conversion_goal_campaign_configs.assert_not_called()

    def test_validate_only_by_default_and_skips_targeted(self):
        result = self.call()

        request = self.request()
        self.assertTrue(request.validate_only)
        self.assertEqual(request.customer_id, CUSTOMER)
        self.assertEqual(len(request.operations), 1)
        operation = request.operations[0]
        self.assertEqual(
            operation.update.resource_name,
            f"customers/{CUSTOMER}/conversionGoalCampaignConfigs/11",
        )
        self.assertEqual(operation.update.custom_conversion_goal, TARGET)
        self.assertEqual(
            list(operation.update_mask.paths), ["custom_conversion_goal"]
        )
        self.assertEqual(result["changed_campaign_ids"], ["11"])
        self.assertEqual(result["skipped_count"], 1)
        self.assertTrue(result["validated"])
        self.assertFalse(result["applied"])
        self.assertEqual(self.config_reads, 1)

    def test_aborts_before_writing(self):
        cases = {
            "goal disabled": lambda: self.target_goal[0].update(
                {"custom_conversion_goal.status": "REMOVED"}
            ),
            "goal changed": lambda: self.before.__setitem__(
                0, config("11", f"{PREFIX}/101")
            ),
            "campaign paused": lambda: self.before.__setitem__(
                0, config("11", f"{PREFIX}/100", status="PAUSED")
            ),
            "customer level": lambda: self.before.__setitem__(
                0, config("11", f"{PREFIX}/100", level="CUSTOMER")
            ),
            "campaign missing": lambda: self.before.pop(),
            "biddable goals": lambda: self.biddable.append({"campaign.id": 11}),
        }
        for name, break_state in cases.items():
            with self.subTest(name):
                self.setUp()
                break_state()
                with self.assertRaises(ToolError):
                    self.call(validate_only=False)
                self.service.mutate_conversion_goal_campaign_configs.assert_not_called()

    def test_write_is_read_back(self):
        result = self.call(validate_only=False)

        self.assertFalse(self.request().validate_only)
        self.assertTrue(result["applied"])
        self.assertTrue(result["success"])
        self.assertTrue(result["verification"]["verified"])
        self.assertEqual(
            result["log_payload"]["action"],
            "set_campaign_custom_conversion_goals",
        )
        self.assertTrue(result["log_payload"]["verified"])

    def test_unverified_write_is_not_success(self):
        self.after = [config("11", f"{PREFIX}/100"), config("22", TARGET)]
        result = self.call(validate_only=False)
        self.assertTrue(result["applied"])
        self.assertFalse(result["success"])

        self.setUp()
        self.after = ServiceUnavailable("down")
        result = self.call(validate_only=False)
        self.assertFalse(result["success"])
        self.assertIn("next_step", result["verification"])

    def test_nothing_to_change_writes_nothing(self):
        self.before = [config("11", TARGET), config("22", TARGET)]
        result = self.call(validate_only=False)
        self.service.mutate_conversion_goal_campaign_configs.assert_not_called()
        self.assertFalse(result["applied"])
        self.assertEqual(result["skipped_count"], 2)


if __name__ == "__main__":
    unittest.main()
