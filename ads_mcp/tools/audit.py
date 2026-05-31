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

"""Audit tools: campaign settings errors, conversion tracking, keyword cannibalization, QS drops."""

from typing import Any, Dict, List
from ads_mcp.coordinator import mcp
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations
import ads_mcp.utils as utils
from google.ads.googleads.errors import GoogleAdsException


def _run_query(customer_id: str, query: str) -> List[Dict[str, Any]]:
    ga_service = utils.get_googleads_service("GoogleAdsService")
    try:
        results = []
        for batch in ga_service.search_stream(customer_id=customer_id, query=query):
            for row in batch.results:
                results.append(utils.format_output_row(row, batch.field_mask.paths))
        return results
    except GoogleAdsException as ex:
        error_msgs = [f"Google Ads API Error: {e.message}" for e in ex.failure.errors]
        raise ToolError(f"Request ID: {ex.request_id}\n" + "\n".join(error_msgs))


def get_campaign_settings_audit(customer_id: str) -> Dict[str, Any]:
    """Audit campaign settings for common mistakes.

    Checks for: target location mismatches, language not matching geo targets,
    search campaigns serving display, missing ad schedules, BROAD match without
    target CPA/ROAS, network settings issues.

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens)
    """
    query = """
        SELECT
            campaign.id,
            campaign.name,
            campaign.status,
            campaign.advertising_channel_type,
            campaign.advertising_channel_sub_type,
            campaign.network_settings.target_google_search,
            campaign.network_settings.target_search_network,
            campaign.network_settings.target_content_network,
            campaign.geo_target_type_setting.positive_geo_target_type,
            campaign.geo_target_type_setting.negative_geo_target_type,
            campaign.target_spend.target_spend_micros,
            campaign.bidding_strategy_type,
            campaign.language_codes
        FROM campaign
        WHERE campaign.status = 'ENABLED'
        PARAMETERS omit_unselected_resource_names=true
    """
    campaigns = _run_query(customer_id, query)

    # Fetch geo targets per campaign
    geo_query = """
        SELECT
            campaign.id,
            campaign_criterion.location.geo_target_constant,
            campaign_criterion.type,
            campaign_criterion.negative
        FROM campaign_criterion
        WHERE campaign_criterion.type = 'LOCATION'
        AND campaign.status = 'ENABLED'
        PARAMETERS omit_unselected_resource_names=true
    """
    geo_rows = _run_query(customer_id, geo_query)
    geo_by_campaign: Dict[str, List] = {}
    for g in geo_rows:
        cid = str(g.get("campaign.id", ""))
        geo_by_campaign.setdefault(cid, []).append(g)

    issues = []
    for c in campaigns:
        cid = str(c.get("campaign.id", ""))
        cname = c.get("campaign.name", "")
        ch_type = c.get("campaign.advertising_channel_type", "")
        campaign_issues = []

        # Search campaign also targeting display network
        if (
            ch_type == "SEARCH"
            and c.get("campaign.network_settings.target_content_network")
        ):
            campaign_issues.append({
                "issue": "Search campaign is also targeting Display Network (Search with Display Expansion)",
                "severity": "medium",
                "suggestion": "Disable Display Network targeting for pure search campaigns to avoid low-quality display traffic",
            })

        # No geo targets
        geos = geo_by_campaign.get(cid, [])
        positive_geos = [g for g in geos if not g.get("campaign_criterion.negative")]
        if not positive_geos:
            campaign_issues.append({
                "issue": "Campaign has no positive geo targets — serving worldwide",
                "severity": "high",
                "suggestion": "Add specific country/region targets unless worldwide reach is intentional",
            })

        # No language codes
        lang_codes = c.get("campaign.language_codes") or []
        if not lang_codes and ch_type in ("SEARCH", "DISPLAY"):
            campaign_issues.append({
                "issue": "Campaign has no language targeting set",
                "severity": "medium",
                "suggestion": "Set language targeting to match your audience and ad copy language",
            })

        if campaign_issues:
            issues.append({
                "campaign_id": cid,
                "campaign_name": cname,
                "channel_type": ch_type,
                "issues": campaign_issues,
            })

    return {
        "total_campaigns_checked": len(campaigns),
        "campaigns_with_issues": len(issues),
        "issues": issues,
    }


def get_conversion_tracking_audit(customer_id: str) -> Dict[str, Any]:
    """Audit conversion tracking for common problems.

    Detects: duplicate conversion actions (same category counted twice),
    conversions with 0 recent data (may be broken), primary vs secondary
    conversion misconfiguration.

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens)
    """
    query = """
        SELECT
            conversion_action.id,
            conversion_action.name,
            conversion_action.status,
            conversion_action.type,
            conversion_action.category,
            conversion_action.counting_type,
            conversion_action.include_in_conversions_metric,
            conversion_action.primary_for_goal,
            metrics.conversions,
            metrics.all_conversions
        FROM conversion_action
        WHERE conversion_action.status = 'ENABLED'
        AND segments.date DURING LAST_30_DAYS
        PARAMETERS omit_unselected_resource_names=true
    """
    rows = _run_query(customer_id, query)

    issues = []
    by_category: Dict[str, List] = {}
    for r in rows:
        cat = r.get("conversion_action.category", "OTHER")
        by_category.setdefault(cat, []).append(r)

        # Zero conversions in 30 days for an enabled action
        if (
            r.get("conversion_action.include_in_conversions_metric")
            and r.get("metrics.conversions", 0) == 0
            and r.get("metrics.all_conversions", 0) == 0
        ):
            issues.append({
                "issue": "Conversion action enabled and included in metric but 0 conversions in last 30 days",
                "severity": "high",
                "conversion_action": r.get("conversion_action.name"),
                "suggestion": "Check if the conversion tag is still firing correctly",
            })

    # Duplicate primary conversions in same category
    for cat, actions in by_category.items():
        primary = [a for a in actions if a.get("conversion_action.primary_for_goal")]
        if len(primary) > 1:
            issues.append({
                "issue": f"Multiple primary conversion actions in category '{cat}'",
                "severity": "medium",
                "actions": [a.get("conversion_action.name") for a in primary],
                "suggestion": "Keep only one primary conversion per category to avoid double-counting in Smart Bidding",
            })

    return {
        "total_conversion_actions": len(rows),
        "issues_found": len(issues),
        "issues": issues,
        "conversion_actions": rows,
    }


def get_keyword_cannibalization(
    customer_id: str,
    date_range: str = "LAST_30_DAYS",
) -> List[Dict[str, Any]]:
    """Find keywords that are cannibalizing each other across campaigns.

    Identifies exact or near-duplicate keywords running in multiple campaigns
    simultaneously, which causes internal auction competition and inflated CPCs.

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens)
        date_range: Date range for performance data (default LAST_30_DAYS)
    """
    query = f"""
        SELECT
            ad_group_criterion.keyword.text,
            ad_group_criterion.keyword.match_type,
            ad_group_criterion.status,
            ad_group.id,
            ad_group.name,
            campaign.id,
            campaign.name,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.average_cpc
        FROM keyword_view
        WHERE ad_group_criterion.status = 'ENABLED'
        AND campaign.status = 'ENABLED'
        AND ad_group.status = 'ENABLED'
        AND segments.date DURING {date_range}
        PARAMETERS omit_unselected_resource_names=true
    """
    rows = _run_query(customer_id, query)

    # Group by normalized keyword text
    by_keyword: Dict[str, List] = {}
    for r in rows:
        text = (r.get("ad_group_criterion.keyword.text") or "").lower().strip()
        by_keyword.setdefault(text, []).append(r)

    duplicates = []
    for text, entries in by_keyword.items():
        campaign_ids = {str(e.get("campaign.id")) for e in entries}
        if len(campaign_ids) > 1:
            total_cost = sum(e.get("metrics.cost_micros", 0) for e in entries) / 1_000_000
            duplicates.append({
                "keyword": text,
                "appears_in_campaigns": len(campaign_ids),
                "total_cost_usd": round(total_cost, 2),
                "entries": [
                    {
                        "campaign": e.get("campaign.name"),
                        "ad_group": e.get("ad_group.name"),
                        "match_type": e.get("ad_group_criterion.keyword.match_type"),
                        "impressions": e.get("metrics.impressions", 0),
                        "cost_usd": round(e.get("metrics.cost_micros", 0) / 1_000_000, 2),
                    }
                    for e in entries
                ],
                "suggestion": (
                    "Consider consolidating into one campaign or adding the other campaigns "
                    "as negative keyword targets to prevent internal competition"
                ),
            })

    return sorted(duplicates, key=lambda x: x["total_cost_usd"], reverse=True)


def get_low_quality_score_keywords(
    customer_id: str,
    max_qs: int = 4,
    min_impressions: int = 100,
    date_range: str = "LAST_30_DAYS",
) -> List[Dict[str, Any]]:
    """Find keywords with low Quality Score and diagnose the likely cause.

    Low QS keywords waste budget and inflate CPCs. This tool surfaces them with
    component scores (expected CTR, ad relevance, landing page) so you know
    exactly what to fix.

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens)
        max_qs: Maximum Quality Score to flag (default 4 — flags 1 through 4)
        min_impressions: Minimum impressions to include (default 100)
        date_range: Date range for impression filter (default LAST_30_DAYS)
    """
    query = f"""
        SELECT
            ad_group_criterion.keyword.text,
            ad_group_criterion.keyword.match_type,
            ad_group_criterion.quality_info.quality_score,
            ad_group_criterion.quality_info.creative_quality_score,
            ad_group_criterion.quality_info.post_click_quality_score,
            ad_group_criterion.quality_info.search_predicted_ctr,
            ad_group.name,
            campaign.name,
            metrics.impressions,
            metrics.clicks,
            metrics.average_cpc,
            metrics.cost_micros,
            metrics.conversions
        FROM keyword_view
        WHERE ad_group_criterion.quality_info.quality_score <= {max_qs}
        AND ad_group_criterion.status = 'ENABLED'
        AND campaign.status = 'ENABLED'
        AND ad_group.status = 'ENABLED'
        AND metrics.impressions >= {min_impressions}
        AND segments.date DURING {date_range}
        ORDER BY metrics.cost_micros DESC
        PARAMETERS omit_unselected_resource_names=true
    """
    rows = _run_query(customer_id, query)

    for r in rows:
        r["metrics.cost_usd"] = round(r.get("metrics.cost_micros", 0) / 1_000_000, 2)
        r["metrics.average_cpc_usd"] = round(r.get("metrics.average_cpc", 0) / 1_000_000, 4)

        # Diagnose the main problem
        creative_qs = r.get("ad_group_criterion.quality_info.creative_quality_score", "")
        lp_qs = r.get("ad_group_criterion.quality_info.post_click_quality_score", "")
        ctr_qs = r.get("ad_group_criterion.quality_info.search_predicted_ctr", "")

        diagnosis = []
        if creative_qs == "BELOW_AVERAGE":
            diagnosis.append("Ad relevance is below average — improve ad copy to match keyword intent")
        if lp_qs == "BELOW_AVERAGE":
            diagnosis.append("Landing page experience is below average — improve page relevance and load speed")
        if ctr_qs == "BELOW_AVERAGE":
            diagnosis.append("Expected CTR is below average — improve ad copy appeal and add extensions")
        r["diagnosis"] = diagnosis if diagnosis else ["Review all quality components — overall QS is low"]

    return rows


# Register all as read-only
for fn in [
    get_campaign_settings_audit,
    get_conversion_tracking_audit,
    get_keyword_cannibalization,
    get_low_quality_score_keywords,
]:
    mcp.add_tool(Tool.from_function(fn, annotations=ToolAnnotations(readOnlyHint=True)))
