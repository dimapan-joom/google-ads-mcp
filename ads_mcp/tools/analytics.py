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

"""Analytics tools: spend/CPC by hour, budget pacing, geo, auction insights, asset performance."""

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


def get_spend_cpc_by_hour(
    customer_id: str,
    date: str,
    campaign_ids: List[str] | None = None,
) -> List[Dict[str, Any]]:
    """Get spend, CPC, clicks, impressions broken down by hour of day for a given date.

    Useful for hourly monitoring — identify hours where spend spikes or CPC is abnormal.
    Results are grouped by campaign and hour.

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens)
        date: Date in YYYY-MM-DD format (use today's date for real-time monitoring)
        campaign_ids: Optional list of campaign IDs to filter. If empty, returns all.
    """
    campaign_filter = ""
    if campaign_ids:
        ids = ", ".join(f"'{cid}'" for cid in campaign_ids)
        campaign_filter = f"AND campaign.id IN ({ids})"

    query = f"""
        SELECT
            campaign.id,
            campaign.name,
            segments.hour,
            metrics.cost_micros,
            metrics.clicks,
            metrics.impressions,
            metrics.average_cpc
        FROM campaign
        WHERE segments.date = '{date}'
        AND campaign.status = 'ENABLED'
        {campaign_filter}
        ORDER BY segments.hour ASC
        PARAMETERS omit_unselected_resource_names=true
    """
    rows = _run_query(customer_id, query)
    for r in rows:
        r["metrics.cost_usd"] = round(r.get("metrics.cost_micros", 0) / 1_000_000, 2)
        r["metrics.average_cpc_usd"] = round(r.get("metrics.average_cpc", 0) / 1_000_000, 4)
    return rows


def get_budget_pacing(
    customer_id: str,
    month_start: str,
    month_end: str,
) -> List[Dict[str, Any]]:
    """Check budget pacing for all active campaigns: actual spend vs expected linear pace.

    Returns each campaign's total spend, daily budget, days elapsed, expected spend,
    and pacing status (over/under/on-track).

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens)
        month_start: First day of the month in YYYY-MM-DD format
        month_end: Last day of the month in YYYY-MM-DD format
    """
    query = f"""
        SELECT
            campaign.id,
            campaign.name,
            campaign.status,
            campaign_budget.amount_micros,
            metrics.cost_micros
        FROM campaign
        WHERE segments.date BETWEEN '{month_start}' AND '{month_end}'
        AND campaign.status = 'ENABLED'
        PARAMETERS omit_unselected_resource_names=true
    """
    rows = _run_query(customer_id, query)

    from datetime import date, datetime
    today = date.today()
    start = datetime.strptime(month_start, "%Y-%m-%d").date()
    end = datetime.strptime(month_end, "%Y-%m-%d").date()
    total_days = (end - start).days + 1
    elapsed_days = min((today - start).days + 1, total_days)
    pacing_ratio = elapsed_days / total_days

    # Aggregate by campaign (rows may have multiple date segments)
    by_campaign: Dict[str, Dict] = {}
    for r in rows:
        cid = r.get("campaign.id", "unknown")
        if cid not in by_campaign:
            by_campaign[cid] = {
                "campaign.id": cid,
                "campaign.name": r.get("campaign.name"),
                "daily_budget_usd": r.get("campaign_budget.amount_micros", 0) / 1_000_000,
                "total_spend_usd": 0.0,
            }
        by_campaign[cid]["total_spend_usd"] += r.get("metrics.cost_micros", 0) / 1_000_000

    results = []
    for c in by_campaign.values():
        monthly_budget = c["daily_budget_usd"] * total_days
        expected_spend = monthly_budget * pacing_ratio
        actual = c["total_spend_usd"]
        if expected_spend > 0:
            pacing_pct = round(actual / expected_spend * 100, 1)
        else:
            pacing_pct = 0
        c["monthly_budget_usd"] = round(monthly_budget, 2)
        c["expected_spend_usd"] = round(expected_spend, 2)
        c["total_spend_usd"] = round(actual, 2)
        c["pacing_percent"] = pacing_pct
        c["pacing_status"] = (
            "over" if pacing_pct > 110
            else "under" if pacing_pct < 80
            else "on_track"
        )
        results.append(c)

    return sorted(results, key=lambda x: x["pacing_percent"])


def get_geo_performance(
    customer_id: str,
    date_range: str = "LAST_30_DAYS",
    min_cost_usd: float = 10.0,
) -> List[Dict[str, Any]]:
    """Get campaign performance broken down by country/region.

    Highlights geos with high spend but low conversions — useful for identifying
    where budget is wasted or where to increase bids.

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens)
        date_range: Date range, e.g. LAST_7_DAYS, LAST_30_DAYS (default LAST_30_DAYS)
        min_cost_usd: Minimum spend threshold to include a geo (default $10)
    """
    query = f"""
        SELECT
            geographic_view.country_criterion_id,
            geographic_view.location_type,
            campaign.id,
            campaign.name,
            metrics.cost_micros,
            metrics.clicks,
            metrics.impressions,
            metrics.conversions,
            metrics.conversions_value,
            metrics.ctr
        FROM geographic_view
        WHERE segments.date DURING {date_range}
        AND campaign.status = 'ENABLED'
        ORDER BY metrics.cost_micros DESC
        PARAMETERS omit_unselected_resource_names=true
    """
    rows = _run_query(customer_id, query)
    results = []
    for r in rows:
        cost = r.get("metrics.cost_micros", 0) / 1_000_000
        if cost < min_cost_usd:
            continue
        convs = r.get("metrics.conversions", 0)
        r["metrics.cost_usd"] = round(cost, 2)
        r["metrics.cpa_usd"] = round(cost / convs, 2) if convs > 0 else None
        r["metrics.roas"] = round(r.get("metrics.conversions_value", 0) / cost, 2) if cost > 0 else None
        results.append(r)
    return results


def get_auction_insights(
    customer_id: str,
    date_range: str = "LAST_30_DAYS",
    campaign_ids: List[str] | None = None,
) -> List[Dict[str, Any]]:
    """Get auction insights metrics showing competitive landscape.

    Returns search impression share, overlap rate, outranking share — tells you
    if competitors are becoming more aggressive or if you're losing share.

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens)
        date_range: Date range, e.g. LAST_7_DAYS, LAST_30_DAYS (default LAST_30_DAYS)
        campaign_ids: Optional list of campaign IDs to filter
    """
    campaign_filter = ""
    if campaign_ids:
        ids = ", ".join(f"'{cid}'" for cid in campaign_ids)
        campaign_filter = f"AND campaign.id IN ({ids})"

    query = f"""
        SELECT
            campaign.id,
            campaign.name,
            metrics.search_impression_share,
            metrics.search_rank_lost_impression_share,
            metrics.search_budget_lost_impression_share,
            metrics.search_top_impression_share,
            metrics.search_absolute_top_impression_share
        FROM campaign
        WHERE segments.date DURING {date_range}
        AND campaign.advertising_channel_type = 'SEARCH'
        AND campaign.status = 'ENABLED'
        {campaign_filter}
        PARAMETERS omit_unselected_resource_names=true
    """
    rows = _run_query(customer_id, query)
    for r in rows:
        # Flag campaigns losing significant impression share
        rank_lost = r.get("metrics.search_rank_lost_impression_share", 0) or 0
        budget_lost = r.get("metrics.search_budget_lost_impression_share", 0) or 0
        r["alert"] = []
        if rank_lost > 0.2:
            r["alert"].append(f"Losing {round(rank_lost*100, 1)}% IS due to low rank — consider raising bids/QS")
        if budget_lost > 0.1:
            r["alert"].append(f"Losing {round(budget_lost*100, 1)}% IS due to budget — consider increasing budget")
    return rows


def get_asset_performance(
    customer_id: str,
    date_range: str = "LAST_30_DAYS",
) -> List[Dict[str, Any]]:
    """Get RSA headline and description asset performance ratings.

    Returns assets rated 'LOW' — these should be replaced.
    Also returns 'BEST' performers for reference when writing new copy.

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens)
        date_range: Date range for performance data (default LAST_30_DAYS)
    """
    query = f"""
        SELECT
            ad_group_ad_asset_view.asset,
            ad_group_ad_asset_view.field_type,
            ad_group_ad_asset_view.performance_label,
            ad_group_ad.ad.id,
            ad_group.name,
            campaign.name,
            metrics.impressions,
            metrics.clicks
        FROM ad_group_ad_asset_view
        WHERE ad_group_ad_asset_view.enabled = TRUE
        AND ad_group_ad.status = 'ENABLED'
        AND campaign.status = 'ENABLED'
        AND segments.date DURING {date_range}
        AND ad_group_ad_asset_view.field_type IN ('HEADLINE', 'DESCRIPTION')
        ORDER BY ad_group_ad_asset_view.performance_label ASC, metrics.impressions DESC
        PARAMETERS omit_unselected_resource_names=true
    """
    rows = _run_query(customer_id, query)
    low = [r for r in rows if r.get("ad_group_ad_asset_view.performance_label") == "LOW"]
    best = [r for r in rows if r.get("ad_group_ad_asset_view.performance_label") == "BEST"]
    return {
        "low_performing_assets": low,
        "best_performing_assets": best[:20],
        "summary": {
            "total_assets": len(rows),
            "low_count": len(low),
            "best_count": len(best),
        }
    }


# Register all as read-only
for fn in [
    get_spend_cpc_by_hour,
    get_budget_pacing,
    get_geo_performance,
    get_auction_insights,
    get_asset_performance,
]:
    mcp.add_tool(Tool.from_function(fn, annotations=ToolAnnotations(readOnlyHint=True)))
