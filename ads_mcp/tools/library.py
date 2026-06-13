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

"""Shared Library & "rarely-visited corners" audits.

These tools surface stale, orphaned or broken objects that live outside the
day-to-day campaign view and therefore almost never get cleaned up:

  * Shared Library: orphaned negative-keyword / placement / brand lists,
    portfolio bidding strategies with no campaigns, shared budgets with no
    references, dead remarketing audiences.
  * Business data (feeds): asset-set page/dynamic/merchant feeds, feeds not
    linked to any campaign, legacy feeds.
  * Asset library: expired promotion assets, disapproved assets.
  * Conversion upload health: offline / enhanced-conversion upload summaries.
  * Stale config: expired Smart Bidding data exclusions & seasonality
    adjustments, zombie experiments past their end date.

Every tool accepts an optional ``login_customer_id`` so it works on sub-accounts
under an MCC (the built-in audit tools do not).
"""

from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple

from ads_mcp.coordinator import mcp
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations
import ads_mcp.utils as utils
from google.ads.googleads.errors import GoogleAdsException

# --- Thresholds (named constants — avoid magic numbers) ---------------------

# Remarketing lists below this size cannot serve on the Search network.
USER_LIST_MIN_SEARCH_SIZE = 1000
# Upload success rate below this is flagged.
UPLOAD_SUCCESS_RATE_WARN = 0.90
# An upload client/action with no successful upload in this many days is stale.
UPLOAD_STALE_DAYS = 7
# Statuses that the offline-upload diagnostics consider healthy.
UPLOAD_HEALTHY_STATUSES = {"EXCELLENT", "GOOD"}
# Asset-set types whose orphan (no campaign link) state is worth flagging.
# Merchant-Center feeds are auto-managed and linked implicitly, so excluded.
FEED_ORPHAN_TYPES = {
    "PAGE_FEED",
    "DYNAMIC_CUSTOM",
    "DYNAMIC_EDUCATION",
    "DYNAMIC_FLIGHTS",
    "DYNAMIC_HOTELS_AND_RENTALS",
    "DYNAMIC_JOBS",
    "DYNAMIC_LOCAL",
    "DYNAMIC_REAL_ESTATE",
    "DYNAMIC_TRAVEL",
}


# --- Query helpers ----------------------------------------------------------


def _run_query(
    customer_id: str,
    query: str,
    login_customer_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Runs a GAQL query and returns formatted rows. Raises ToolError on failure."""
    ga_service = utils.get_googleads_service(
        "GoogleAdsService", login_customer_id=login_customer_id
    )
    try:
        results: List[Dict[str, Any]] = []
        for batch in ga_service.search_stream(customer_id=customer_id, query=query):
            for row in batch.results:
                results.append(utils.format_output_row(row, batch.field_mask.paths))
        return results
    except GoogleAdsException as ex:
        error_msgs = [f"Google Ads API Error: {e.message}" for e in ex.failure.errors]
        raise ToolError(f"Request ID: {ex.request_id}\n" + "\n".join(error_msgs))


def _try_query(
    customer_id: str,
    query: str,
    login_customer_id: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Like _run_query but never raises — returns (rows, error_message).

    Used for probes that may not be supported on every account/API surface so
    that one unsupported sub-query does not sink the whole audit.
    """
    try:
        return _run_query(customer_id, query, login_customer_id), None
    except ToolError as ex:
        return [], str(ex)


def _finding(
    category: str,
    severity: str,
    issue: str,
    entity: str = "",
    suggestion: str = "",
    **details: Any,
) -> Dict[str, Any]:
    """Builds a consistently-shaped finding record."""
    return {
        "category": category,
        "severity": severity,
        "issue": issue,
        "entity": entity,
        "suggestion": suggestion,
        "details": details,
    }


def _parse_date(value: Optional[str]) -> Optional[date]:
    """Parses 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS[.ffffff]' into a date."""
    if not value:
        return None
    try:
        return datetime.strptime(value.split(" ")[0], "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None


def _days_ago(value: Optional[str], today: date) -> Optional[int]:
    parsed = _parse_date(value)
    if parsed is None:
        return None
    return (today - parsed).days


def _summarize(findings: List[Dict[str, Any]]) -> Dict[str, int]:
    """Counts findings by severity."""
    counts = {"high": 0, "medium": 0, "low": 0}
    for f in findings:
        sev = f.get("severity", "low")
        counts[sev] = counts.get(sev, 0) + 1
    counts["total"] = len(findings)
    return counts


# Cap on how many example entities to attach to an aggregated finding.
SAMPLE_SIZE = 10


def _aggregate(
    items: List[Dict[str, Any]],
    category: str,
    severity: str,
    issue: str,
    suggestion: str,
    sample_fn,
) -> Optional[Dict[str, Any]]:
    """Collapses many same-kind items into a single counted finding with a sample.

    Returns None when ``items`` is empty so callers can filter it out. This
    keeps reports actionable instead of emitting hundreds of one-line findings.
    """
    if not items:
        return None
    return _finding(
        category, severity, issue.format(n=len(items)),
        suggestion=suggestion,
        count=len(items),
        sample=[sample_fn(x) for x in items[:SAMPLE_SIZE]],
    )


# --- Tool 1: Shared Library --------------------------------------------------


def get_shared_library_audit(
    customer_id: str,
    login_customer_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Audit the Google Ads "Shared Library" for orphaned and stale objects.

    Checks four shared-library areas that are rarely revisited:
      * Shared sets (negative-keyword / placement-exclusion / brand lists) that
        are attached to zero campaigns (reference_count == 0).
      * Portfolio (shared) bidding strategies attached to zero campaigns.
      * Explicitly-shared campaign budgets attached to zero campaigns.
      * Remarketing audiences that are dead: closed, empty, or too small to
        serve on the Search network (< 1000 members).

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens).
        login_customer_id: Manager (MCC) account ID to use as login-customer-id.
            Required for sub-accounts only reachable via a manager account.
    """
    findings: List[Dict[str, Any]] = []

    # 1. Shared sets (negative keyword lists, placement exclusions, brand lists).
    shared_sets = _run_query(
        customer_id,
        """
        SELECT shared_set.id, shared_set.name, shared_set.type,
               shared_set.status, shared_set.member_count, shared_set.reference_count
        FROM shared_set
        WHERE shared_set.status = 'ENABLED'
        """,
        login_customer_id,
    )
    for s in shared_sets:
        if s.get("shared_set.reference_count", 0) == 0:
            findings.append(
                _finding(
                    "shared_set",
                    "medium",
                    "Shared set is attached to no campaigns (orphaned)",
                    entity=s.get("shared_set.name", ""),
                    suggestion="Attach it to a campaign or remove it to declutter the Shared Library",
                    shared_set_id=s.get("shared_set.id"),
                    type=s.get("shared_set.type"),
                    member_count=s.get("shared_set.member_count"),
                )
            )

    # 2. Portfolio (shared) bidding strategies with no live campaigns.
    strategies = _run_query(
        customer_id,
        """
        SELECT bidding_strategy.id, bidding_strategy.name, bidding_strategy.type,
               bidding_strategy.status, bidding_strategy.non_removed_campaign_count
        FROM bidding_strategy
        WHERE bidding_strategy.status = 'ENABLED'
        """,
        login_customer_id,
    )
    orphan_strategies = [
        b for b in strategies
        if b.get("bidding_strategy.non_removed_campaign_count", 0) == 0
    ]
    agg = _aggregate(
        orphan_strategies, "bidding_strategy", "medium",
        "{n} portfolio bidding strategy(ies) have no active campaigns (orphaned)",
        "Remove unused portfolio strategies or assign campaigns to them",
        lambda b: {"id": b.get("bidding_strategy.id"),
                   "name": b.get("bidding_strategy.name"),
                   "type": b.get("bidding_strategy.type")},
    )
    if agg:
        findings.append(agg)

    # 3. Explicitly-shared budgets with no references.
    budgets = _run_query(
        customer_id,
        """
        SELECT campaign_budget.id, campaign_budget.name,
               campaign_budget.reference_count, campaign_budget.amount_micros
        FROM campaign_budget
        WHERE campaign_budget.explicitly_shared = true
        AND campaign_budget.status = 'ENABLED'
        """,
        login_customer_id,
    )
    orphan_budgets = [
        bd for bd in budgets if bd.get("campaign_budget.reference_count", 0) == 0
    ]
    agg = _aggregate(
        orphan_budgets, "shared_budget", "low",
        "{n} shared budget(s) are attached to no campaigns (orphaned)",
        "Remove unused shared budgets",
        lambda bd: {"id": bd.get("campaign_budget.id"),
                    "name": bd.get("campaign_budget.name"),
                    "daily_amount": round(bd.get("campaign_budget.amount_micros", 0) / 1_000_000, 2)},
    )
    if agg:
        findings.append(agg)

    # 4. Dead / unusable remarketing audiences.
    user_lists = _run_query(
        customer_id,
        """
        SELECT user_list.id, user_list.name, user_list.type,
               user_list.membership_status, user_list.size_for_search,
               user_list.size_for_display, user_list.eligible_for_search,
               user_list.closing_reason
        FROM user_list
        """,
        login_customer_id,
    )
    closed, empty, too_small = [], [], []
    for u in user_lists:
        status = u.get("user_list.membership_status")
        size_search = u.get("user_list.size_for_search", 0) or 0
        size_display = u.get("user_list.size_for_display", 0) or 0
        if status == "CLOSED":
            closed.append(u)
        elif size_search == 0 and size_display == 0:
            empty.append(u)
        elif u.get("user_list.eligible_for_search") and 0 < size_search < USER_LIST_MIN_SEARCH_SIZE:
            too_small.append(u)

    audience_sample = lambda u: {"id": u.get("user_list.id"), "name": u.get("user_list.name")}
    for agg in (
        _aggregate(closed, "audience", "low",
                   "{n} remarketing audience(s) are CLOSED (no longer accumulating members)",
                   "Review and remove obsolete closed audiences (many are historical/DELETED lists)",
                   lambda u: {**audience_sample(u), "closing_reason": u.get("user_list.closing_reason")}),
        _aggregate(empty, "audience", "low",
                   "{n} audience(s) are empty (0 members) — tag may not be firing or list unused",
                   "Verify the remarketing tag/feed for actively-used lists, remove the rest",
                   audience_sample),
        _aggregate(too_small, "audience", "low",
                   f"{{n}} audience(s) too small to serve on Search (< {USER_LIST_MIN_SEARCH_SIZE} members)",
                   "These will not be applied on Search until they reach 1000 members",
                   lambda u: {**audience_sample(u), "size_for_search": u.get("user_list.size_for_search", 0)}),
    ):
        if agg:
            findings.append(agg)

    return {
        "customer_id": customer_id,
        "summary": _summarize(findings),
        "checked": {
            "shared_sets": len(shared_sets),
            "portfolio_strategies": len(strategies),
            "shared_budgets": len(budgets),
            "audiences": len(user_lists),
        },
        "findings": findings,
    }


# --- Tool 2: Feeds / business data ------------------------------------------


def get_feeds_audit(
    customer_id: str,
    login_customer_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Audit feeds / business data (asset-set feeds + legacy feeds).

    Surfaces:
      * Inventory of asset-set feeds by type and status (page feeds, dynamic
        remarketing feeds, Merchant Center feeds).
      * ENABLED page/dynamic feeds that are linked to NO campaign (orphaned —
        wasted setup, often left behind after a campaign is rebuilt).
      * The amount of REMOVED feed clutter sitting in the account.
      * Legacy (pre-asset) feeds still present.

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens).
        login_customer_id: Manager (MCC) account ID to use as login-customer-id.
    """
    findings: List[Dict[str, Any]] = []

    asset_sets = _run_query(
        customer_id,
        """
        SELECT asset_set.id, asset_set.name, asset_set.type, asset_set.status
        FROM asset_set
        """,
        login_customer_id,
    )

    # Inventory by type/status.
    by_type: Dict[str, Dict[str, int]] = {}
    for a in asset_sets:
        t = a.get("asset_set.type", "UNKNOWN")
        st = a.get("asset_set.status", "UNKNOWN")
        by_type.setdefault(t, {}).setdefault(st, 0)
        by_type[t][st] += 1

    removed_count = sum(
        1 for a in asset_sets if a.get("asset_set.status") == "REMOVED"
    )

    # Which asset sets have at least one ENABLED campaign link?
    links = _run_query(
        customer_id,
        """
        SELECT campaign_asset_set.asset_set, campaign_asset_set.status
        FROM campaign_asset_set
        WHERE campaign_asset_set.status = 'ENABLED'
        """,
        login_customer_id,
    )
    linked_asset_sets = {
        l.get("campaign_asset_set.asset_set") for l in links
    }

    # Orphaned ENABLED page/dynamic feeds (not linked to any campaign).
    for a in asset_sets:
        if a.get("asset_set.status") != "ENABLED":
            continue
        if a.get("asset_set.type") not in FEED_ORPHAN_TYPES:
            continue
        resource_name = f"customers/{customer_id}/assetSets/{a.get('asset_set.id')}"
        if resource_name not in linked_asset_sets:
            findings.append(
                _finding(
                    "feed_orphan", "medium",
                    "ENABLED page/dynamic feed is not linked to any campaign",
                    entity=a.get("asset_set.name", ""),
                    suggestion="Link the feed to a campaign or remove it",
                    asset_set_id=a.get("asset_set.id"),
                    type=a.get("asset_set.type"),
                )
            )

    if removed_count:
        findings.append(
            _finding(
                "feed_clutter", "low",
                f"{removed_count} REMOVED asset-set feeds remain in the account",
                suggestion="Informational — REMOVED feeds are hidden in the UI but inflate inventory",
                removed_count=removed_count,
            )
        )

    return {
        "customer_id": customer_id,
        "summary": _summarize(findings),
        "inventory": {
            "asset_sets_total": len(asset_sets),
            "by_type": by_type,
            "removed_count": removed_count,
        },
        "findings": findings,
    }


# --- Tool 3: Asset library ---------------------------------------------------


def get_asset_library_audit(
    customer_id: str,
    login_customer_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Audit the asset library for stale and disapproved assets.

    Surfaces:
      * Promotion assets whose end_date is in the past but that still live in
        the account (expired sales left attached can mislead reporting / serve).
      * Disapproved assets (sitelinks, callouts, images, …) by approval status.

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens).
        login_customer_id: Manager (MCC) account ID to use as login-customer-id.
    """
    today = date.today()
    findings: List[Dict[str, Any]] = []

    # Expired promotion assets.
    promos = _run_query(
        customer_id,
        """
        SELECT asset.id, asset.name, asset.promotion_asset.end_date
        FROM asset
        WHERE asset.type = 'PROMOTION'
        """,
        login_customer_id,
    )
    expired = []
    for p in promos:
        end = _parse_date(p.get("asset.promotion_asset.end_date"))
        if end is not None and end < today:
            expired.append(p)
    # Collapse into one finding with a sample to avoid hundreds of rows.
    if expired:
        sample = sorted(
            expired,
            key=lambda x: x.get("asset.promotion_asset.end_date", ""),
        )[:10]
        findings.append(
            _finding(
                "expired_promotion", "medium",
                f"{len(expired)} promotion asset(s) have an end_date in the past",
                suggestion="Remove expired promotion assets so they cannot serve or skew reports",
                expired_count=len(expired),
                sample=[
                    {"asset_id": s.get("asset.id"),
                     "end_date": s.get("asset.promotion_asset.end_date")}
                    for s in sample
                ],
            )
        )

    # Disapproved assets (guarded — policy filtering is not supported everywhere).
    disapproved, disapp_err = _try_query(
        customer_id,
        """
        SELECT asset.id, asset.name, asset.type,
               asset.policy_summary.approval_status
        FROM asset
        WHERE asset.policy_summary.approval_status = 'DISAPPROVED'
        """,
        login_customer_id,
    )
    by_type: Dict[str, int] = {}
    for d in disapproved:
        t = d.get("asset.type", "UNKNOWN")
        by_type[t] = by_type.get(t, 0) + 1
    if disapproved:
        findings.append(
            _finding(
                "disapproved_asset", "high",
                f"{len(disapproved)} disapproved asset(s) found",
                suggestion="Fix or replace disapproved assets — they cannot serve",
                disapproved_count=len(disapproved),
                by_type=by_type,
                sample=[
                    {"asset_id": d.get("asset.id"), "type": d.get("asset.type")}
                    for d in disapproved[:10]
                ],
            )
        )

    return {
        "customer_id": customer_id,
        "summary": _summarize(findings),
        "checked": {"promotion_assets": len(promos)},
        "findings": findings,
        "notes": ([] if disapp_err is None else [f"disapproved-asset probe skipped: {disapp_err}"]),
    }


# --- Tool 4: Conversion upload health ---------------------------------------


def get_conversion_upload_health(
    customer_id: str,
    login_customer_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Diagnose offline / enhanced-conversion upload health.

    This is one of the least-visited corners of an account: uploads can silently
    degrade (success rate drops, a feed stops sending) without any campaign-level
    symptom. Checks both the per-client and per-conversion-action upload
    summaries for: non-healthy status, low success rate, and stale uploads (no
    successful upload in the last few days relative to the freshest one).

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens).
        login_customer_id: Manager (MCC) account ID to use as login-customer-id.
    """
    today = date.today()
    findings: List[Dict[str, Any]] = []

    clients = _run_query(
        customer_id,
        """
        SELECT offline_conversion_upload_client_summary.client,
               offline_conversion_upload_client_summary.status,
               offline_conversion_upload_client_summary.total_event_count,
               offline_conversion_upload_client_summary.successful_event_count,
               offline_conversion_upload_client_summary.pending_event_count,
               offline_conversion_upload_client_summary.last_upload_date_time
        FROM offline_conversion_upload_client_summary
        """,
        login_customer_id,
    )
    for c in clients:
        client = c.get("offline_conversion_upload_client_summary.client", "")
        status = c.get("offline_conversion_upload_client_summary.status", "")
        total = c.get("offline_conversion_upload_client_summary.total_event_count", 0) or 0
        ok = c.get("offline_conversion_upload_client_summary.successful_event_count", 0) or 0
        last = c.get("offline_conversion_upload_client_summary.last_upload_date_time")
        rate = (ok / total) if total else 1.0
        days = _days_ago(last, today)
        if status and status not in UPLOAD_HEALTHY_STATUSES:
            findings.append(
                _finding(
                    "upload_client", "high",
                    f"Upload client status is '{status}'",
                    entity=client,
                    suggestion="Investigate failing uploads for this client",
                    success_rate=round(rate, 3), last_upload=last,
                )
            )
        if total and rate < UPLOAD_SUCCESS_RATE_WARN:
            findings.append(
                _finding(
                    "upload_client", "medium",
                    f"Upload success rate {round(rate * 100, 1)}% (< {int(UPLOAD_SUCCESS_RATE_WARN * 100)}%)",
                    entity=client,
                    suggestion="Check rejected events (matching, formatting, consent) for this client",
                    successful=ok, total=total,
                )
            )
        if days is not None and days > UPLOAD_STALE_DAYS:
            findings.append(
                _finding(
                    "upload_client", "medium",
                    f"No upload in {days} days (last: {last})",
                    entity=client,
                    suggestion="The upload pipeline for this client may have stopped",
                    last_upload=last,
                )
            )

    actions = _run_query(
        customer_id,
        """
        SELECT offline_conversion_upload_conversion_action_summary.client,
               offline_conversion_upload_conversion_action_summary.conversion_action_name,
               offline_conversion_upload_conversion_action_summary.status,
               offline_conversion_upload_conversion_action_summary.total_event_count,
               offline_conversion_upload_conversion_action_summary.successful_event_count,
               offline_conversion_upload_conversion_action_summary.last_upload_date_time
        FROM offline_conversion_upload_conversion_action_summary
        """,
        login_customer_id,
    )
    # Determine the freshest upload to detect actions that have gone quiet.
    freshest = None
    for a in actions:
        d = _parse_date(a.get("offline_conversion_upload_conversion_action_summary.last_upload_date_time"))
        if d and (freshest is None or d > freshest):
            freshest = d
    for a in actions:
        name = a.get("offline_conversion_upload_conversion_action_summary.conversion_action_name", "")
        status = a.get("offline_conversion_upload_conversion_action_summary.status", "")
        total = a.get("offline_conversion_upload_conversion_action_summary.total_event_count", 0) or 0
        ok = a.get("offline_conversion_upload_conversion_action_summary.successful_event_count", 0) or 0
        last = a.get("offline_conversion_upload_conversion_action_summary.last_upload_date_time")
        rate = (ok / total) if total else 1.0
        last_date = _parse_date(last)
        if status and status not in UPLOAD_HEALTHY_STATUSES:
            findings.append(
                _finding(
                    "upload_action", "high",
                    f"Conversion action upload status is '{status}'",
                    entity=name,
                    suggestion="Investigate why uploads for this action are unhealthy",
                    client=a.get("offline_conversion_upload_conversion_action_summary.client"),
                    success_rate=round(rate, 3), last_upload=last,
                )
            )
        if total and rate < UPLOAD_SUCCESS_RATE_WARN:
            findings.append(
                _finding(
                    "upload_action", "medium",
                    f"Upload success rate {round(rate * 100, 1)}% for this action",
                    entity=name,
                    suggestion="Check rejected events for this conversion action",
                    successful=ok, total=total,
                )
            )
        # Action stopped sending while others are still fresh.
        if freshest and last_date and (freshest - last_date).days > UPLOAD_STALE_DAYS:
            findings.append(
                _finding(
                    "upload_action", "medium",
                    f"Action's last upload ({last}) is {(freshest - last_date).days} days behind the freshest upload",
                    entity=name,
                    suggestion="This action may be deprecated but still ENABLED, or its feed stopped",
                    last_upload=last,
                )
            )

    return {
        "customer_id": customer_id,
        "summary": _summarize(findings),
        "checked": {"clients": len(clients), "conversion_actions": len(actions)},
        "findings": findings,
        "client_summaries": clients,
    }


# --- Tool 5: Stale config ----------------------------------------------------


def get_stale_config_audit(
    customer_id: str,
    login_customer_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Audit rarely-cleaned config: bidding signals & zombie experiments.

    Surfaces:
      * Smart Bidding data exclusions and seasonality adjustments whose window
        has already ended but that are still ENABLED (no effect, but clutter
        and confusion).
      * Experiments still ENABLED/HALTED whose end_date is in the past — zombie
        A/B tests that were never graduated or removed.

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens).
        login_customer_id: Manager (MCC) account ID to use as login-customer-id.
    """
    today = date.today()
    findings: List[Dict[str, Any]] = []

    for resource, id_field, label in (
        ("bidding_data_exclusion", "data_exclusion_id", "data exclusion"),
        ("bidding_seasonality_adjustment", "seasonality_adjustment_id", "seasonality adjustment"),
    ):
        rows = _run_query(
            customer_id,
            f"""
            SELECT {resource}.{id_field}, {resource}.name, {resource}.status,
                   {resource}.start_date_time, {resource}.end_date_time
            FROM {resource}
            WHERE {resource}.status = 'ENABLED'
            """,
            login_customer_id,
        )
        for r in rows:
            end = _parse_date(r.get(f"{resource}.end_date_time"))
            name = r.get(f"{resource}.name", "")
            if end is not None and end < today:
                findings.append(
                    _finding(
                        "bidding_signal", "medium",
                        f"ENABLED {label} ended on {r.get(f'{resource}.end_date_time')} (expired)",
                        entity=name,
                        suggestion=f"Remove the expired {label} — it no longer affects bidding",
                        id=r.get(f"{resource}.{id_field}"),
                    )
                )

    # Zombie experiments: still ENABLED/HALTED but end_date already passed.
    experiments = _run_query(
        customer_id,
        """
        SELECT experiment.experiment_id, experiment.name, experiment.status,
               experiment.type, experiment.start_date, experiment.end_date
        FROM experiment
        WHERE experiment.status IN ('ENABLED', 'HALTED')
        """,
        login_customer_id,
    )
    zombies = [
        e for e in experiments
        if (_parse_date(e.get("experiment.end_date")) or today) < today
    ]
    agg = _aggregate(
        zombies, "zombie_experiment", "medium",
        "{n} experiment(s) are still ENABLED/HALTED but past their end_date",
        "Graduate or remove these stale experiments — they no longer run a valid test",
        lambda e: {"id": e.get("experiment.experiment_id"),
                   "name": e.get("experiment.name"),
                   "status": e.get("experiment.status"),
                   "end_date": e.get("experiment.end_date")},
    )
    if agg:
        findings.append(agg)

    return {
        "customer_id": customer_id,
        "summary": _summarize(findings),
        "checked": {"experiments": len(experiments)},
        "findings": findings,
    }


# --- Tool 6: Umbrella --------------------------------------------------------


def get_library_health(
    customer_id: str,
    login_customer_id: Optional[str] = None,
) -> Dict[str, Any]:
    """One-shot health report across every shared-library / rarely-visited area.

    Runs get_shared_library_audit, get_feeds_audit, get_asset_library_audit,
    get_conversion_upload_health and get_stale_config_audit, then consolidates
    their findings with an overall severity breakdown. Use this for a quick
    "what has gone stale in this account?" sweep.

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens).
        login_customer_id: Manager (MCC) account ID to use as login-customer-id.
    """
    sections = {
        "shared_library": get_shared_library_audit,
        "feeds": get_feeds_audit,
        "asset_library": get_asset_library_audit,
        "conversion_uploads": get_conversion_upload_health,
        "stale_config": get_stale_config_audit,
    }

    results: Dict[str, Any] = {}
    all_findings: List[Dict[str, Any]] = []
    errors: Dict[str, str] = {}
    for key, fn in sections.items():
        try:
            section = fn(customer_id, login_customer_id)
            results[key] = section.get("summary", {})
            for f in section.get("findings", []):
                all_findings.append({**f, "section": key})
        except ToolError as ex:
            errors[key] = str(ex)

    order = {"high": 0, "medium": 1, "low": 2}
    all_findings.sort(key=lambda f: order.get(f.get("severity", "low"), 3))

    return {
        "customer_id": customer_id,
        "overall": _summarize(all_findings),
        "by_section": results,
        "findings": all_findings,
        "errors": errors,
    }


# Register all as read-only tools.
for _fn in [
    get_shared_library_audit,
    get_feeds_audit,
    get_asset_library_audit,
    get_conversion_upload_health,
    get_stale_config_audit,
    get_library_health,
]:
    mcp.add_tool(Tool.from_function(_fn, annotations=ToolAnnotations(readOnlyHint=True)))
