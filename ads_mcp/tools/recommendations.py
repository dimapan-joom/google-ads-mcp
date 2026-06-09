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

"""Recommendations & optimization tools: Google recommendations, search terms, target ROAS bulk update, weekly digest."""

from typing import Any, Dict, List, Literal
from ads_mcp.coordinator import mcp
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations
import ads_mcp.utils as utils
from google.ads.googleads.errors import GoogleAdsException
from google.protobuf import field_mask_pb2


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


def get_google_recommendations(
    customer_id: str,
    types: List[str] | None = None,
) -> List[Dict[str, Any]]:
    """Get Google's own optimization recommendations for this account.

    Includes budget increase suggestions, bid adjustments, keyword expansions,
    ad copy improvements, and more. These are the same recommendations shown
    in the Google Ads UI under 'Recommendations'.

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens)
        types: Optional filter by recommendation type, e.g. ['RAISE_TARGET_CPA',
               'INCREASE_BUDGET', 'MOVE_UNUSED_BUDGET', 'KEYWORD'].
               If empty, returns all types.
    """
    if types:
        type_list = ", ".join(f"'{t}'" for t in types)
        type_filter = f"WHERE recommendation.type IN ({type_list})"
    else:
        type_filter = ""

    # Note: recommendation.impact.base_metrics.* and projected_metrics.* sub-fields
    # were removed in Google Ads API v24. Use recommendation.impact as a whole object.
    query = f"""
        SELECT
            recommendation.type,
            recommendation.impact,
            recommendation.campaign,
            recommendation.campaigns,
            recommendation.resource_name,
            recommendation.dismissed,
            recommendation.target_roas_opt_in_recommendation,
            recommendation.maximize_conversions_opt_in_recommendation,
            recommendation.maximize_clicks_opt_in_recommendation
        FROM recommendation
        {type_filter}
    """
    rows = _run_query(customer_id, query)
    for r in rows:
        impact = r.get("recommendation.impact") or {}
        if isinstance(impact, dict):
            base = impact.get("base_metrics") or {}
            proj = impact.get("projected_metrics") or {}
            r["impact.base_cost_usd"] = round(float(base.get("cost_micros") or 0) / 1_000_000, 2)
            r["impact.projected_cost_usd"] = round(float(proj.get("cost_micros") or 0) / 1_000_000, 2)
            base_convs = float(base.get("conversions") or 0)
            proj_convs = float(proj.get("conversions") or 0)
            r["impact.projected_conversion_lift"] = round(proj_convs - base_convs, 1)
    return rows


def get_search_term_suggestions(
    customer_id: str,
    date_range: str = "LAST_30_DAYS",
    min_conversions_for_keyword: float = 1.0,
    min_cost_for_negative_usd: float = 5.0,
) -> Dict[str, Any]:
    """Analyze search terms to suggest new keywords to add and irrelevant terms to exclude.

    - New keyword suggestions: search terms that drove conversions but aren't keywords yet
    - Negative keyword suggestions: search terms that spent money but drove 0 conversions

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens)
        date_range: Date range to analyze (default LAST_30_DAYS)
        min_conversions_for_keyword: Min conversions for a term to be suggested as keyword (default 1.0)
        min_cost_for_negative_usd: Min spend (USD) for a term to be suggested as negative (default $5)
    """
    query = f"""
        SELECT
            search_term_view.search_term,
            search_term_view.status,
            campaign.id,
            campaign.name,
            ad_group.id,
            ad_group.name,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value
        FROM search_term_view
        WHERE segments.date DURING {date_range}
        AND campaign.status = 'ENABLED'
        ORDER BY metrics.cost_micros DESC
        PARAMETERS omit_unselected_resource_names=true
    """
    rows = _run_query(customer_id, query)

    add_as_keyword = []
    add_as_negative = []

    for r in rows:
        status = r.get("search_term_view.status", "")
        cost = r.get("metrics.cost_micros", 0) / 1_000_000
        convs = r.get("metrics.conversions", 0) or 0
        term = r.get("search_term_view.search_term", "")

        r["metrics.cost_usd"] = round(cost, 2)

        # Already a keyword — skip
        if status == "ADDED":
            continue

        if convs >= min_conversions_for_keyword:
            add_as_keyword.append({
                "search_term": term,
                "campaign": r.get("campaign.name"),
                "ad_group": r.get("ad_group.name"),
                "conversions": convs,
                "cost_usd": round(cost, 2),
                "suggested_match_type": "EXACT",
            })
        elif cost >= min_cost_for_negative_usd and convs == 0:
            add_as_negative.append({
                "search_term": term,
                "campaign": r.get("campaign.name"),
                "ad_group": r.get("ad_group.name"),
                "cost_usd": round(cost, 2),
                "impressions": r.get("metrics.impressions", 0),
                "suggested_level": "campaign",
            })

    return {
        "date_range": date_range,
        "new_keyword_suggestions": sorted(add_as_keyword, key=lambda x: x["conversions"], reverse=True),
        "negative_keyword_suggestions": sorted(add_as_negative, key=lambda x: x["cost_usd"], reverse=True),
        "summary": {
            "suggest_add_as_keyword": len(add_as_keyword),
            "suggest_add_as_negative": len(add_as_negative),
        },
    }


def bulk_update_target_roas(
    customer_id: str,
    updates: List[Dict[str, Any]],
    login_customer_id: str | None = None,
) -> List[Dict[str, Any]]:
    """Bulk update Target ROAS for multiple campaigns from a list.

    ⚠️ This is a mutating operation. Confirm with the user before calling.

    Each update in the list should have 'campaign_id' and 'target_roas'.
    Target ROAS is expressed as a decimal (e.g., 3.5 means 350% ROAS).

    Optionally pass 'bidding_strategy_type' per entry:
    - 'MAXIMIZE_CONVERSION_VALUE' → sets maximize_conversion_value.target_roas (PMax)
    - anything else (default) → sets target_roas.target_roas (Shopping/Search)

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens)
        updates: List of dicts with 'campaign_id' (str), 'target_roas' (float),
                 and optional 'bidding_strategy_type' (str)
        login_customer_id: The manager account (MCC) id to use as login-customer-id header.
            Required when the customer is only accessible via a manager account.

    Example:
        updates = [
            {"campaign_id": "123456", "target_roas": 3.5},
            {"campaign_id": "789012", "target_roas": 2.0, "bidding_strategy_type": "MAXIMIZE_CONVERSION_VALUE"},
        ]
    """
    if not updates:
        raise ToolError("updates list is empty")

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    campaign_service = client.get_service("CampaignService")

    operations = []
    for u in updates:
        cid = str(u.get("campaign_id", ""))
        roas = float(u.get("target_roas", 0))
        bidding_type = u.get("bidding_strategy_type", "TARGET_ROAS")
        if not cid:
            raise ToolError(f"Missing campaign_id in update entry: {u}")
        if roas <= 0:
            raise ToolError(f"target_roas must be > 0, got {roas} for campaign {cid}")

        op = client.get_type("CampaignOperation")
        op.update.resource_name = campaign_service.campaign_path(customer_id, cid)

        if bidding_type == "MAXIMIZE_CONVERSION_VALUE":
            op.update.maximize_conversion_value.target_roas = roas
            op.update_mask.CopyFrom(
                field_mask_pb2.FieldMask(paths=["maximize_conversion_value.target_roas"])
            )
        else:
            op.update.target_roas.target_roas = roas
            op.update_mask.CopyFrom(
                field_mask_pb2.FieldMask(paths=["target_roas.target_roas"])
            )

        operations.append(op)

    try:
        response = campaign_service.mutate_campaigns(
            customer_id=customer_id, operations=operations
        )
        return [
            {
                "success": True,
                "campaign_resource": r.resource_name,
                "campaign_id": updates[i].get("campaign_id"),
                "new_target_roas": updates[i].get("target_roas"),
            }
            for i, r in enumerate(response.results)
        ]
    except GoogleAdsException as ex:
        error_msgs = [f"Google Ads API Error: {e.message}" for e in ex.failure.errors]
        raise ToolError(f"Request ID: {ex.request_id}\n" + "\n".join(error_msgs))


def get_weekly_digest(
    customer_id: str,
    this_week_start: str,
    this_week_end: str,
    last_week_start: str,
    last_week_end: str,
) -> Dict[str, Any]:
    """Generate a weekly performance digest comparing this week vs last week.

    Returns overall account metrics, top campaigns by spend, week-over-week
    changes, and a list of campaigns that significantly degraded or improved.

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens)
        this_week_start: Start of current period in YYYY-MM-DD
        this_week_end: End of current period in YYYY-MM-DD
        last_week_start: Start of comparison period in YYYY-MM-DD
        last_week_end: End of comparison period in YYYY-MM-DD
    """
    def fetch_period(start: str, end: str) -> List[Dict]:
        q = f"""
            SELECT
                campaign.id,
                campaign.name,
                metrics.cost_micros,
                metrics.clicks,
                metrics.impressions,
                metrics.conversions,
                metrics.conversions_value,
                metrics.ctr,
                metrics.average_cpc
            FROM campaign
            WHERE segments.date BETWEEN '{start}' AND '{end}'
            AND campaign.status != 'REMOVED'
            PARAMETERS omit_unselected_resource_names=true
        """
        return _run_query(customer_id, q)

    def aggregate(rows: List[Dict]) -> Dict[str, Dict]:
        by_campaign: Dict[str, Dict] = {}
        for r in rows:
            cid = str(r.get("campaign.id", ""))
            if cid not in by_campaign:
                by_campaign[cid] = {
                    "campaign_id": cid,
                    "campaign_name": r.get("campaign.name"),
                    "cost_usd": 0.0,
                    "clicks": 0,
                    "impressions": 0,
                    "conversions": 0.0,
                    "conversions_value": 0.0,
                }
            entry = by_campaign[cid]
            entry["cost_usd"] += r.get("metrics.cost_micros", 0) / 1_000_000
            entry["clicks"] += r.get("metrics.clicks", 0) or 0
            entry["impressions"] += r.get("metrics.impressions", 0) or 0
            entry["conversions"] += r.get("metrics.conversions", 0) or 0
            entry["conversions_value"] += r.get("metrics.conversions_value", 0) or 0
        for entry in by_campaign.values():
            cost = entry["cost_usd"]
            convs = entry["conversions"]
            entry["cost_usd"] = round(cost, 2)
            entry["cpa_usd"] = round(cost / convs, 2) if convs > 0 else None
            entry["roas"] = round(entry["conversions_value"] / cost, 2) if cost > 0 else None
        return by_campaign

    this_week = aggregate(fetch_period(this_week_start, this_week_end))
    last_week = aggregate(fetch_period(last_week_start, last_week_end))

    def pct_change(new, old):
        if old == 0:
            return None
        return round((new - old) / old * 100, 1)

    # Account totals
    def totals(data: Dict[str, Dict]) -> Dict:
        t = {"cost_usd": 0.0, "clicks": 0, "conversions": 0.0, "conversions_value": 0.0}
        for e in data.values():
            t["cost_usd"] += e["cost_usd"]
            t["clicks"] += e["clicks"]
            t["conversions"] += e["conversions"]
            t["conversions_value"] += e["conversions_value"]
        t["cost_usd"] = round(t["cost_usd"], 2)
        t["roas"] = round(t["conversions_value"] / t["cost_usd"], 2) if t["cost_usd"] > 0 else None
        return t

    tw_total = totals(this_week)
    lw_total = totals(last_week)

    # Per-campaign changes
    campaign_changes = []
    all_cids = set(this_week) | set(last_week)
    for cid in all_cids:
        tw = this_week.get(cid, {})
        lw = last_week.get(cid, {})
        tw_cost = tw.get("cost_usd", 0)
        lw_cost = lw.get("cost_usd", 0)
        tw_convs = tw.get("conversions", 0)
        lw_convs = lw.get("conversions", 0)
        cost_chg = pct_change(tw_cost, lw_cost)
        conv_chg = pct_change(tw_convs, lw_convs)
        campaign_changes.append({
            "campaign_id": cid,
            "campaign_name": tw.get("campaign_name") or lw.get("campaign_name"),
            "this_week_cost_usd": tw_cost,
            "last_week_cost_usd": lw_cost,
            "cost_change_pct": cost_chg,
            "this_week_conversions": tw_convs,
            "last_week_conversions": lw_convs,
            "conversion_change_pct": conv_chg,
            "this_week_roas": tw.get("roas"),
            "last_week_roas": lw.get("roas"),
        })

    degraded = [
        c for c in campaign_changes
        if (c["conversion_change_pct"] or 0) < -20 and c["this_week_cost_usd"] > 50
    ]
    improved = [
        c for c in campaign_changes
        if (c["conversion_change_pct"] or 0) > 20 and c["this_week_cost_usd"] > 50
    ]

    return {
        "period": {"this_week": f"{this_week_start} — {this_week_end}", "last_week": f"{last_week_start} — {last_week_end}"},
        "account_totals": {
            "this_week": tw_total,
            "last_week": lw_total,
            "cost_change_pct": pct_change(tw_total["cost_usd"], lw_total["cost_usd"]),
            "conversion_change_pct": pct_change(tw_total["conversions"], lw_total["conversions"]),
            "roas_change_pct": pct_change(tw_total.get("roas") or 0, lw_total.get("roas") or 0),
        },
        "top_campaigns_by_spend": sorted(campaign_changes, key=lambda x: x["this_week_cost_usd"], reverse=True)[:10],
        "significantly_degraded": sorted(degraded, key=lambda x: x["conversion_change_pct"] or 0),
        "significantly_improved": sorted(improved, key=lambda x: x["conversion_change_pct"] or 0, reverse=True),
    }


# Register read-only tools
for fn in [get_google_recommendations, get_search_term_suggestions, get_weekly_digest]:
    mcp.add_tool(Tool.from_function(fn, annotations=ToolAnnotations(readOnlyHint=True)))

# Register mutating tool
mcp.add_tool(Tool.from_function(
    bulk_update_target_roas,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
))
