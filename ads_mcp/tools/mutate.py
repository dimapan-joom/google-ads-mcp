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

"""Tools for mutating Google Ads resources (ads, campaigns, budgets, etc.)."""

from typing import Any, Dict, List, Literal
from ads_mcp.coordinator import mcp
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations
import ads_mcp.utils as utils
from google.ads.googleads.errors import GoogleAdsException


def get_disapproved_ads(customer_id: str) -> List[Dict[str, Any]]:
    """Find all disapproved ads in a Google Ads account.

    Returns a list of disapproved responsive search ads and expanded text ads
    with their policy topics (reasons for disapproval).

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens)
    """
    ga_service = utils.get_googleads_service("GoogleAdsService")
    query = """
        SELECT
            ad_group_ad.ad.id,
            ad_group_ad.ad.name,
            ad_group_ad.ad.type,
            ad_group_ad.ad.responsive_search_ad.headlines,
            ad_group_ad.ad.responsive_search_ad.descriptions,
            ad_group_ad.policy_summary.approval_status,
            ad_group_ad.policy_summary.policy_topic_entries,
            ad_group.name,
            campaign.name,
            campaign.id,
            ad_group.id
        FROM ad_group_ad
        WHERE ad_group_ad.policy_summary.approval_status IN (
            'DISAPPROVED', 'AREA_OF_INTEREST_ONLY'
        )
        AND ad_group_ad.status = 'ENABLED'
        AND ad_group.status = 'ENABLED'
        AND campaign.status = 'ENABLED'
        PARAMETERS omit_unselected_resource_names=true
    """
    try:
        results = []
        for batch in ga_service.search_stream(customer_id=customer_id, query=query):
            for row in batch.results:
                results.append(utils.format_output_row(row, batch.field_mask.paths))
        return results
    except GoogleAdsException as ex:
        error_msgs = [f"Google Ads API Error: {e.message}" for e in ex.failure.errors]
        raise ToolError(f"Request ID: {ex.request_id}\n" + "\n".join(error_msgs))


def get_account_health(customer_id: str) -> Dict[str, Any]:
    """Get a health summary for a Google Ads account.

    Checks for common issues: disapproved ads, low quality score keywords,
    campaigns with no budget, paused campaigns with active budgets.

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens)
    """
    ga_service = utils.get_googleads_service("GoogleAdsService")

    issues = []

    # Check disapproved ads count
    try:
        q = """
            SELECT ad_group_ad.ad.id
            FROM ad_group_ad
            WHERE ad_group_ad.policy_summary.approval_status IN ('DISAPPROVED', 'AREA_OF_INTEREST_ONLY')
            AND ad_group_ad.status = 'ENABLED'
            AND ad_group.status = 'ENABLED'
            AND campaign.status = 'ENABLED'
            PARAMETERS omit_unselected_resource_names=true
        """
        disapproved = sum(
            len(batch.results)
            for batch in ga_service.search_stream(customer_id=customer_id, query=q)
        )
        if disapproved > 0:
            issues.append({"type": "disapproved_ads", "count": disapproved, "severity": "high"})
    except GoogleAdsException:
        pass

    # Check low quality score keywords (QS <= 3)
    try:
        q = """
            SELECT keyword_view.resource_name, ad_group_criterion.quality_info.quality_score,
                   ad_group_criterion.keyword.text, campaign.name, ad_group.name
            FROM keyword_view
            WHERE ad_group_criterion.quality_info.quality_score <= 3
            AND ad_group_criterion.status != 'REMOVED'
            AND campaign.status = 'ENABLED'
            LIMIT 50
            PARAMETERS omit_unselected_resource_names=true
        """
        low_qs = []
        for batch in ga_service.search_stream(customer_id=customer_id, query=q):
            for row in batch.results:
                low_qs.append(utils.format_output_row(row, batch.field_mask.paths))
        if low_qs:
            issues.append({"type": "low_quality_score_keywords", "count": len(low_qs), "severity": "medium", "examples": low_qs[:5]})
    except GoogleAdsException:
        pass

    # Check enabled campaigns with 0 impressions last 7 days
    try:
        q = """
            SELECT campaign.name, campaign.id, metrics.impressions
            FROM campaign
            WHERE campaign.status = 'ENABLED'
            AND segments.date DURING LAST_7_DAYS
            AND metrics.impressions = 0
            PARAMETERS omit_unselected_resource_names=true
        """
        zero_imp = []
        for batch in ga_service.search_stream(customer_id=customer_id, query=q):
            for row in batch.results:
                zero_imp.append(utils.format_output_row(row, batch.field_mask.paths))
        if zero_imp:
            issues.append({"type": "enabled_campaigns_zero_impressions", "count": len(zero_imp), "severity": "medium", "campaigns": zero_imp})
    except GoogleAdsException:
        pass

    return {
        "customer_id": customer_id,
        "total_issues": len(issues),
        "issues": issues,
    }


def update_responsive_search_ad(
    customer_id: str,
    ad_id: str,
    headlines: List[str],
    descriptions: List[str],
) -> Dict[str, Any]:
    """Update the headlines and descriptions of a Responsive Search Ad.

    ⚠️ This is a mutating operation. Confirm with the user before calling.

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens)
        ad_id: The numeric ID of the ad to update
        headlines: List of headline texts (3–15 items, max 30 chars each)
        descriptions: List of description texts (2–4 items, max 90 chars each)
    """
    if len(headlines) < 3 or len(headlines) > 15:
        raise ToolError("headlines must have between 3 and 15 items")
    if len(descriptions) < 2 or len(descriptions) > 4:
        raise ToolError("descriptions must have between 2 and 4 items")
    for h in headlines:
        if len(h) > 30:
            raise ToolError(f"Headline too long (max 30 chars): '{h}'")
    for d in descriptions:
        if len(d) > 90:
            raise ToolError(f"Description too long (max 90 chars): '{d}'")

    client = utils.get_googleads_client()
    ad_service = client.get_service("AdService")
    ad_type = client.get_type("Ad")

    ad = ad_type()
    ad.resource_name = ad_service.ad_path(customer_id, ad_id)

    rsa = ad.responsive_search_ad
    rsa.headlines.clear()
    rsa.descriptions.clear()

    for text in headlines:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        rsa.headlines.append(asset)

    for text in descriptions:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        rsa.descriptions.append(asset)

    field_mask = client.get_type("FieldMask")
    field_mask.paths.extend([
        "responsive_search_ad.headlines",
        "responsive_search_ad.descriptions",
    ])

    operation = client.get_type("AdOperation")
    operation.update.CopyFrom(ad)
    operation.update_mask.CopyFrom(field_mask)

    try:
        response = ad_service.mutate_ads(
            customer_id=customer_id, operations=[operation]
        )
        return {
            "success": True,
            "updated_ad": response.results[0].resource_name,
        }
    except GoogleAdsException as ex:
        error_msgs = [f"Google Ads API Error: {e.message}" for e in ex.failure.errors]
        raise ToolError(f"Request ID: {ex.request_id}\n" + "\n".join(error_msgs))


def update_campaign_budget(
    customer_id: str,
    campaign_budget_id: str,
    new_amount_micros: int,
) -> Dict[str, Any]:
    """Update the daily budget of a campaign budget.

    ⚠️ This is a mutating operation. Confirm with the user before calling.

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens)
        campaign_budget_id: The numeric ID of the campaign budget
        new_amount_micros: New daily budget in micros (1 USD = 1_000_000 micros)
    """
    client = utils.get_googleads_client()
    budget_service = client.get_service("CampaignBudgetService")
    budget_type = client.get_type("CampaignBudget")

    budget = budget_type()
    budget.resource_name = budget_service.campaign_budget_path(customer_id, campaign_budget_id)
    budget.amount_micros = new_amount_micros

    field_mask = client.get_type("FieldMask")
    field_mask.paths.append("amount_micros")

    operation = client.get_type("CampaignBudgetOperation")
    operation.update.CopyFrom(budget)
    operation.update_mask.CopyFrom(field_mask)

    try:
        response = budget_service.mutate_campaign_budgets(
            customer_id=customer_id, operations=[operation]
        )
        return {
            "success": True,
            "updated_budget": response.results[0].resource_name,
            "new_amount_micros": new_amount_micros,
            "new_amount_usd": new_amount_micros / 1_000_000,
        }
    except GoogleAdsException as ex:
        error_msgs = [f"Google Ads API Error: {e.message}" for e in ex.failure.errors]
        raise ToolError(f"Request ID: {ex.request_id}\n" + "\n".join(error_msgs))


def set_campaign_status(
    customer_id: str,
    campaign_id: str,
    status: Literal["ENABLED", "PAUSED"],
) -> Dict[str, Any]:
    """Enable or pause a campaign.

    ⚠️ This is a mutating operation. Confirm with the user before calling.

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens)
        campaign_id: The numeric ID of the campaign
        status: 'ENABLED' to enable or 'PAUSED' to pause
    """
    client = utils.get_googleads_client()
    campaign_service = client.get_service("CampaignService")
    campaign_type = client.get_type("Campaign")
    status_enum = client.enums.CampaignStatusEnum.CampaignStatus

    campaign = campaign_type()
    campaign.resource_name = campaign_service.campaign_path(customer_id, campaign_id)
    campaign.status = status_enum[status]

    field_mask = client.get_type("FieldMask")
    field_mask.paths.append("status")

    operation = client.get_type("CampaignOperation")
    operation.update.CopyFrom(campaign)
    operation.update_mask.CopyFrom(field_mask)

    try:
        response = campaign_service.mutate_campaigns(
            customer_id=customer_id, operations=[operation]
        )
        return {
            "success": True,
            "campaign": response.results[0].resource_name,
            "new_status": status,
        }
    except GoogleAdsException as ex:
        error_msgs = [f"Google Ads API Error: {e.message}" for e in ex.failure.errors]
        raise ToolError(f"Request ID: {ex.request_id}\n" + "\n".join(error_msgs))


def get_low_performing_ads(
    customer_id: str,
    min_impressions: int = 1000,
    max_ctr_percent: float = 1.0,
    date_range: str = "LAST_30_DAYS",
) -> List[Dict[str, Any]]:
    """Find text creatives (RSA) with low performance based on CTR and impressions.

    Returns responsive search ads that have enough impressions to be statistically
    meaningful but are underperforming on CTR. Also includes ad strength rating.

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens)
        min_impressions: Minimum impressions to consider (default 1000)
        max_ctr_percent: CTR threshold in percent — ads below this are flagged (default 1.0%)
        date_range: Date range to use, e.g. LAST_30_DAYS, LAST_7_DAYS (default LAST_30_DAYS)
    """
    ga_service = utils.get_googleads_service("GoogleAdsService")
    max_ctr = max_ctr_percent / 100.0

    query = f"""
        SELECT
            ad_group_ad.ad.id,
            ad_group_ad.ad.responsive_search_ad.headlines,
            ad_group_ad.ad.responsive_search_ad.descriptions,
            ad_group_ad.ad_strength,
            ad_group_ad.policy_summary.approval_status,
            ad_group.name,
            ad_group.id,
            campaign.name,
            campaign.id,
            metrics.impressions,
            metrics.clicks,
            metrics.ctr,
            metrics.conversions,
            metrics.cost_micros
        FROM ad_group_ad
        WHERE ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
        AND ad_group_ad.status = 'ENABLED'
        AND ad_group_ad.policy_summary.approval_status = 'APPROVED'
        AND campaign.status = 'ENABLED'
        AND ad_group.status = 'ENABLED'
        AND metrics.impressions >= {min_impressions}
        AND metrics.ctr <= {max_ctr}
        AND segments.date DURING {date_range}
        ORDER BY metrics.impressions DESC
        PARAMETERS omit_unselected_resource_names=true
    """

    try:
        results = []
        for batch in ga_service.search_stream(customer_id=customer_id, query=query):
            for row in batch.results:
                data = utils.format_output_row(row, batch.field_mask.paths)
                # Add human-readable CTR
                ctr = data.get("metrics.ctr", 0)
                data["metrics.ctr_percent"] = round(ctr * 100, 2)
                data["metrics.cost_usd"] = round(data.get("metrics.cost_micros", 0) / 1_000_000, 2)
                results.append(data)
        return results
    except GoogleAdsException as ex:
        error_msgs = [f"Google Ads API Error: {e.message}" for e in ex.failure.errors]
        raise ToolError(f"Request ID: {ex.request_id}\n" + "\n".join(error_msgs))


def create_ad_variation_experiment(
    customer_id: str,
    campaign_id: str,
    experiment_name: str,
    traffic_split_percent: int = 50,
    new_headlines: List[str] | None = None,
    new_descriptions: List[str] | None = None,
    ad_group_id: str | None = None,
) -> Dict[str, Any]:
    """Create a Google Ads Experiment (A/B test) to test new ad copy in a campaign.

    Creates a draft experiment where the control is the existing campaign and
    the treatment gets new RSA headlines/descriptions. Traffic is split according
    to traffic_split_percent.

    ⚠️ This is a mutating operation. Confirm with the user before calling.

    Note: After creation the experiment is in DRAFT status. Call start_experiment
    to begin serving it.

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens)
        campaign_id: The campaign to run the experiment on
        experiment_name: Human-readable name for the experiment
        traffic_split_percent: % of traffic going to the treatment arm (default 50)
        new_headlines: New headlines to test (3–15 items, max 30 chars each)
        new_descriptions: New descriptions to test (2–4 items, max 90 chars each)
        ad_group_id: If set, only modify ads in this specific ad group
    """
    if traffic_split_percent < 1 or traffic_split_percent > 99:
        raise ToolError("traffic_split_percent must be between 1 and 99")
    if new_headlines and (len(new_headlines) < 3 or len(new_headlines) > 15):
        raise ToolError("new_headlines must have between 3 and 15 items")
    if new_descriptions and (len(new_descriptions) < 2 or len(new_descriptions) > 4):
        raise ToolError("new_descriptions must have between 2 and 4 items")
    if new_headlines:
        for h in new_headlines:
            if len(h) > 30:
                raise ToolError(f"Headline too long (max 30 chars): '{h}'")
    if new_descriptions:
        for d in new_descriptions:
            if len(d) > 90:
                raise ToolError(f"Description too long (max 90 chars): '{d}'")

    client = utils.get_googleads_client()
    experiment_service = client.get_service("ExperimentService")

    # Step 1: Create the experiment
    experiment = client.get_type("Experiment")
    experiment.name = experiment_name
    experiment.type_ = client.enums.ExperimentTypeEnum.ExperimentType.SEARCH_CUSTOM
    experiment.status = client.enums.ExperimentStatusEnum.ExperimentStatus.SETUP
    experiment.traffic_split_percent = traffic_split_percent
    experiment.traffic_split_type = (
        client.enums.ExperimentTrafficSplitTypeEnum.ExperimentTrafficSplitType.RANDOM_QUERY
    )

    exp_op = client.get_type("ExperimentOperation")
    exp_op.create.CopyFrom(experiment)

    try:
        exp_response = experiment_service.mutate_experiments(
            customer_id=customer_id, operations=[exp_op]
        )
        experiment_resource = exp_response.results[0].resource_name
        experiment_id = experiment_resource.split("/")[-1]
    except GoogleAdsException as ex:
        error_msgs = [f"Google Ads API Error: {e.message}" for e in ex.failure.errors]
        raise ToolError(f"Failed to create experiment. Request ID: {ex.request_id}\n" + "\n".join(error_msgs))

    # Step 2: Add the campaign arm (treatment)
    arm_service = client.get_service("ExperimentArmService")
    campaign_service = client.get_service("CampaignService")

    arm = client.get_type("ExperimentArm")
    arm.experiment = experiment_resource
    arm.name = "Treatment"
    arm.control = False
    arm.traffic_split = traffic_split_percent
    arm.campaigns.append(campaign_service.campaign_path(customer_id, campaign_id))

    arm_op = client.get_type("ExperimentArmOperation")
    arm_op.create.CopyFrom(arm)

    try:
        arm_response = arm_service.mutate_experiment_arms(
            customer_id=customer_id, operations=[arm_op]
        )
        arm_resource = arm_response.results[0].resource_name
    except GoogleAdsException as ex:
        error_msgs = [f"Google Ads API Error: {e.message}" for e in ex.failure.errors]
        raise ToolError(f"Failed to create experiment arm. Request ID: {ex.request_id}\n" + "\n".join(error_msgs))

    result = {
        "success": True,
        "experiment_resource": experiment_resource,
        "experiment_id": experiment_id,
        "arm_resource": arm_resource,
        "status": "SETUP",
        "traffic_split_percent": traffic_split_percent,
        "next_step": (
            "Experiment is in SETUP status. "
            "Review it in Google Ads UI under Experiments, then start it manually "
            "or call start_experiment with the experiment_id."
        ),
    }

    # Step 3: If new ad copy provided, note it for manual application to treatment campaign
    if new_headlines or new_descriptions:
        result["ad_copy_to_apply"] = {
            "note": (
                "After the experiment draft campaign is created by Google Ads, "
                "use update_responsive_search_ad to apply these to ads in the draft campaign."
            ),
            "new_headlines": new_headlines,
            "new_descriptions": new_descriptions,
            "ad_group_id": ad_group_id,
        }

    return result


# Register read-only tools
mcp.add_tool(Tool.from_function(
    get_disapproved_ads,
    annotations=ToolAnnotations(readOnlyHint=True)
))
mcp.add_tool(Tool.from_function(
    get_account_health,
    annotations=ToolAnnotations(readOnlyHint=True)
))
mcp.add_tool(Tool.from_function(
    get_low_performing_ads,
    annotations=ToolAnnotations(readOnlyHint=True)
))

# Register mutating tools
mcp.add_tool(Tool.from_function(
    update_responsive_search_ad,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
))
mcp.add_tool(Tool.from_function(
    update_campaign_budget,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
))
mcp.add_tool(Tool.from_function(
    set_campaign_status,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
))
mcp.add_tool(Tool.from_function(
    create_ad_variation_experiment,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
))
