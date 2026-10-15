"""Replace existing campaign custom conversion goals in bounded batches."""

import re
from typing import Any, Dict, Optional

from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations
from google.ads.googleads.errors import GoogleAdsException
from google.api_core.exceptions import GoogleAPICallError

from ads_mcp.coordinator import mcp
from ads_mcp.tools.actions import _run_query, _format_api_error
import ads_mcp.utils as utils


def set_campaign_custom_conversion_goals(
    customer_id: str,
    custom_conversion_goal_id: str,
    expected_goals: Dict[str, str],
    validate_only: bool = True,
    login_customer_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Replace existing custom goals for 1–100 explicitly selected enabled campaigns.

    expected_goals maps campaign ID to its current custom goal ID (digits only).
    Read the current configs first. A changed goal or non-enabled campaign aborts
    the entire batch before writing. Already-targeted campaigns are skipped.
    Only same-account custom goals are supported. Standard biddable goals must
    already be disabled; this tool does not change them or conversion actions.

    Defaults to Google Ads validate_only: validates the request without writing.
    Confirm the concrete changes with the user before validate_only=False.
    A write is read back; success requires every selected campaign to match.
    On an unknown outcome, re-read before retrying. Record log_payload through
    sheets-log. Budgets, bidding targets, status and names are unchanged.
    """
    ids = [customer_id, custom_conversion_goal_id, *expected_goals.keys(),
           *expected_goals.values()]
    if login_customer_id is not None:
        ids.append(login_customer_id)
    if not 1 <= len(expected_goals) <= 100:
        raise ToolError("expected_goals must contain 1–100 campaigns")
    if any(not re.fullmatch(r"[1-9][0-9]*", value) for value in ids):
        raise ToolError("All customer, campaign and goal IDs must be positive ASCII digits")

    prefix = f"customers/{customer_id}"
    target = f"{prefix}/customConversionGoals/{custom_conversion_goal_id}"
    campaign_ids = ",".join(expected_goals)
    config_query = f"""
        SELECT campaign.id, campaign.name, campaign.status,
               conversion_goal_campaign_config.resource_name,
               conversion_goal_campaign_config.custom_conversion_goal,
               conversion_goal_campaign_config.goal_config_level
        FROM conversion_goal_campaign_config
        WHERE campaign.id IN ({campaign_ids})
    """
    def query(sql):
        return _run_query(customer_id, sql, login_customer_id)

    try:
        goals = query(f"""
            SELECT custom_conversion_goal.name, custom_conversion_goal.status,
                   custom_conversion_goal.conversion_actions
            FROM custom_conversion_goal
            WHERE custom_conversion_goal.resource_name = '{target}'
        """)
        if (len(goals) != 1 or goals[0]["custom_conversion_goal.status"] != "ENABLED"
                or not goals[0].get("custom_conversion_goal.conversion_actions")):
            raise ToolError("Target custom goal must exist, be enabled and contain actions")
        before = query(config_query)
        if {str(r["campaign.id"]) for r in before} != set(expected_goals):
            raise ToolError("Some requested campaign configs are missing; nothing written")
        field = "conversion_goal_campaign_config.custom_conversion_goal"
        level = "conversion_goal_campaign_config.goal_config_level"
        for row in before:
            expected = f"{prefix}/customConversionGoals/{expected_goals[str(row['campaign.id'])]}"
            if (row["campaign.status"] != "ENABLED" or row[level] != "CAMPAIGN"
                    or row[field] not in (expected, target)):
                raise ToolError(f"Campaign {row['campaign.id']} changed or is not enabled with a custom goal; nothing written")
        if query(f"""
            SELECT campaign.id FROM campaign_conversion_goal
            WHERE campaign.id IN ({campaign_ids})
              AND campaign_conversion_goal.biddable = TRUE
        """):
            raise ToolError("Standard biddable goals are also enabled; review them separately before replacing the custom goal")
        changed = [r for r in before if r[field] != target]
        result = {
            "customer_id": customer_id, "validate_only": validate_only,
            "target": target, "target_goal": goals[0], "before": before,
            "changed_campaign_ids": [str(r["campaign.id"]) for r in changed],
            "skipped_count": len(before) - len(changed),
        }
        if changed:
            client = utils.get_googleads_client(login_customer_id=login_customer_id)
            operations = []
            for row in changed:
                op = client.get_type("ConversionGoalCampaignConfigOperation")
                op.update.resource_name = row["conversion_goal_campaign_config.resource_name"]
                op.update.custom_conversion_goal = target
                op.update_mask.paths.append("custom_conversion_goal")
                operations.append(op)
            request = client.get_type("MutateConversionGoalCampaignConfigsRequest")
            request.customer_id = customer_id
            request.operations.extend(operations)
            request.validate_only = validate_only
            service = client.get_service("ConversionGoalCampaignConfigService")
            service.mutate_conversion_goal_campaign_configs(request=request)
        if validate_only:
            return {**result, "validated": True, "applied": False}
    except GoogleAdsException as ex:
        raise ToolError(_format_api_error(ex))
    except GoogleAPICallError as ex:
        raise ToolError(f"Google Ads call failed; re-read configs before retrying: {ex}")

    # The mutation may have committed even if this read fails. Never imply rollback.
    try:
        after = query(config_query)
        verified = ({str(r["campaign.id"]) for r in after} == set(expected_goals)
                    and all(r[field] == target and r[level] == "CAMPAIGN" for r in after))
        verification = {"verified": verified, "after": after}
    except (GoogleAdsException, GoogleAPICallError) as ex:
        verification = {"verified": False, "error": str(ex),
                        "next_step": "Mutation returned; re-read configs before retrying"}
    return {**result, "applied": bool(changed), "success": verification["verified"],
            "verification": verification,
            "log_payload": {"platform": "google_ads", "action": "set_campaign_custom_conversion_goals",
                            **result, "verified": verification["verified"]}}


mcp.add_tool(Tool.from_function(
    set_campaign_custom_conversion_goals,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
))
