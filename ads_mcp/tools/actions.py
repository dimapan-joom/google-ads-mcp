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

"""Day-to-day "junior marketer" actions.

Write tools that close the most common optimization loops, plus the change
history needed to act safely:

  * add_negative_keywords — add negatives at campaign / ad-group level or to a
    shared negative-keyword list (the bread-and-butter search-term-mining
    action). Verifies the write and returns a ready-to-log payload.
  * get_change_history — who changed what in the account recently (context so
    an agent does not undo someone's work and can correlate metric shifts).

Every tool accepts an optional ``login_customer_id`` so it works on sub-accounts
under an MCC. Mutating tools verify their write by reading the target back and
return a ``log_payload`` the caller should record via sheets-log/log_change.
"""

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from ads_mcp.coordinator import mcp
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations
import ads_mcp.utils as utils
from google.ads.googleads.errors import GoogleAdsException
from google.api_core import exceptions as gax_exceptions

# --- Guardrail constants ----------------------------------------------------

MAX_KEYWORDS_PER_CALL = 1000
MAX_KEYWORD_LEN = 80
MAX_KEYWORD_WORDS = 10
ALLOWED_MATCH_TYPES = {"EXACT", "PHRASE", "BROAD"}
DEFAULT_MATCH_TYPE = "PHRASE"
CHANGE_HISTORY_MAX_DAYS = 30  # Google Ads API caps change_event lookback at 30d.
CHANGE_HISTORY_MAX_LIMIT = 10000  # API hard limit for change_event queries.


def _format_api_error(ex: GoogleAdsException) -> str:
    error_msgs = [f"Google Ads API Error: {e.message}" for e in ex.failure.errors]
    return f"Request ID: {ex.request_id}\n" + "\n".join(error_msgs)


def _run_query(
    customer_id: str, query: str, login_customer_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    ga_service = utils.get_googleads_service(
        "GoogleAdsService", login_customer_id=login_customer_id
    )
    results: List[Dict[str, Any]] = []
    for batch in ga_service.search_stream(customer_id=customer_id, query=query):
        for row in batch.results:
            results.append(utils.format_output_row(row, batch.field_mask.paths))
    return results


def _validate_keywords(keywords: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Validates and normalizes the keyword list. Raises ToolError on bad input."""
    if not keywords:
        raise ToolError("keywords must be a non-empty list")
    if len(keywords) > MAX_KEYWORDS_PER_CALL:
        raise ToolError(
            f"Too many keywords ({len(keywords)}); max {MAX_KEYWORDS_PER_CALL} per call"
        )
    normalized: List[Dict[str, str]] = []
    for kw in keywords:
        text = (kw.get("text") or "").strip()
        if not text:
            raise ToolError("Each keyword needs a non-empty 'text'")
        if len(text) > MAX_KEYWORD_LEN:
            raise ToolError(f"Keyword too long (max {MAX_KEYWORD_LEN} chars): '{text}'")
        if len(text.split()) > MAX_KEYWORD_WORDS:
            raise ToolError(f"Keyword has too many words (max {MAX_KEYWORD_WORDS}): '{text}'")
        match_type = (kw.get("match_type") or DEFAULT_MATCH_TYPE).upper()
        if match_type not in ALLOWED_MATCH_TYPES:
            raise ToolError(
                f"Invalid match_type '{match_type}' for '{text}'; "
                f"allowed: {sorted(ALLOWED_MATCH_TYPES)}"
            )
        normalized.append({"text": text, "match_type": match_type})
    return normalized


def add_negative_keywords(
    customer_id: str,
    keywords: List[Dict[str, Any]],
    campaign_id: Optional[str] = None,
    ad_group_id: Optional[str] = None,
    shared_set_id: Optional[str] = None,
    login_customer_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Add negative keywords at campaign level, ad-group level, or to a shared list.

    Exactly ONE target must be given: campaign_id, ad_group_id, or shared_set_id.
    This is the core search-term-mining action: exclude wasteful queries you found
    in the search term report. The write is verified by reading the target back,
    and the result includes a ``log_payload`` to record via sheets-log/log_change.

    ⚠️ Mutating operation. Confirm with the user before calling.

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens).
        keywords: List of {"text": str, "match_type": "EXACT"|"PHRASE"|"BROAD"}.
            match_type defaults to PHRASE if omitted.
        campaign_id: Add as campaign-level negatives.
        ad_group_id: Add as ad-group-level negatives.
        shared_set_id: Add to a shared negative-keyword list (find IDs via
            get_shared_library_audit / a shared_set search).
        login_customer_id: Manager (MCC) account ID to use as login-customer-id.
    """
    targets = [t for t in (campaign_id, ad_group_id, shared_set_id) if t]
    if len(targets) != 1:
        raise ToolError(
            "Provide exactly one of campaign_id, ad_group_id, or shared_set_id"
        )

    normalized = _validate_keywords(keywords)
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    match_enum = client.enums.KeywordMatchTypeEnum

    if campaign_id:
        level, target_id = "campaign", campaign_id
        service = client.get_service("CampaignCriterionService")
        operations = []
        for kw in normalized:
            op = client.get_type("CampaignCriterionOperation")
            crit = op.create
            crit.campaign = f"customers/{customer_id}/campaigns/{campaign_id}"
            crit.negative = True
            crit.keyword.text = kw["text"]
            crit.keyword.match_type = match_enum[kw["match_type"]]
            operations.append(op)
        mutate = lambda: service.mutate_campaign_criteria(
            customer_id=customer_id, operations=operations
        )
    elif ad_group_id:
        level, target_id = "ad_group", ad_group_id
        service = client.get_service("AdGroupCriterionService")
        operations = []
        for kw in normalized:
            op = client.get_type("AdGroupCriterionOperation")
            crit = op.create
            crit.ad_group = f"customers/{customer_id}/adGroups/{ad_group_id}"
            crit.negative = True
            crit.keyword.text = kw["text"]
            crit.keyword.match_type = match_enum[kw["match_type"]]
            operations.append(op)
        mutate = lambda: service.mutate_ad_group_criteria(
            customer_id=customer_id, operations=operations
        )
    else:
        level, target_id = "shared_set", shared_set_id
        service = client.get_service("SharedCriterionService")
        operations = []
        for kw in normalized:
            op = client.get_type("SharedCriterionOperation")
            crit = op.create
            crit.shared_set = f"customers/{customer_id}/sharedSets/{shared_set_id}"
            crit.keyword.text = kw["text"]
            crit.keyword.match_type = match_enum[kw["match_type"]]
            operations.append(op)
        mutate = lambda: service.mutate_shared_criteria(
            customer_id=customer_id, operations=operations
        )

    try:
        response = mutate()
        created = [r.resource_name for r in response.results]
    except GoogleAdsException as ex:
        raise ToolError(_format_api_error(ex))
    except gax_exceptions.GoogleAPICallError as ex:
        # e.g. RESOURCE_EXHAUSTED (mutate quota). Surface a clean, actionable
        # message instead of a raw gRPC stack trace.
        raise ToolError(f"Google Ads API call failed: {getattr(ex, 'message', str(ex))}")

    verification = _verify_negatives(
        customer_id, level, target_id, normalized, login_customer_id
    )

    return {
        "success": True,
        "level": level,
        "target_id": target_id,
        "added_count": len(created),
        "created_resource_names": created,
        "verification": verification,
        "log_payload": {
            "platform": "google_ads",
            "customer_id": customer_id,
            "action": "add_negative_keywords",
            "level": level,
            "target_id": target_id,
            "keywords": normalized,
            "verified": verification.get("verified"),
        },
    }


def _verify_negatives(
    customer_id: str,
    level: str,
    target_id: str,
    expected: List[Dict[str, str]],
    login_customer_id: Optional[str],
) -> Dict[str, Any]:
    """Reads the target back and confirms every requested negative is present."""
    if level == "campaign":
        query = f"""
            SELECT campaign_criterion.keyword.text
            FROM campaign_criterion
            WHERE campaign_criterion.type = 'KEYWORD'
            AND campaign_criterion.negative = true
            AND campaign.id = {target_id}
        """
        field = "campaign_criterion.keyword.text"
    elif level == "ad_group":
        query = f"""
            SELECT ad_group_criterion.keyword.text
            FROM ad_group_criterion
            WHERE ad_group_criterion.type = 'KEYWORD'
            AND ad_group_criterion.negative = true
            AND ad_group.id = {target_id}
        """
        field = "ad_group_criterion.keyword.text"
    else:
        query = f"""
            SELECT shared_criterion.keyword.text
            FROM shared_criterion
            WHERE shared_set.id = {target_id}
        """
        field = "shared_criterion.keyword.text"

    try:
        rows = _run_query(customer_id, query, login_customer_id)
    except (GoogleAdsException, gax_exceptions.GoogleAPICallError):
        return {"verified": None, "note": "verification query failed; write likely succeeded"}

    present = {(r.get(field) or "").lower().strip() for r in rows}
    missing = [kw["text"] for kw in expected if kw["text"].lower().strip() not in present]
    return {
        "verified": len(missing) == 0,
        "missing": missing,
        "total_negatives_on_target": len(present),
    }


def get_change_history(
    customer_id: str,
    days: int = 7,
    limit: int = 200,
    login_customer_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Show recent account changes — who changed what (from change_event).

    Use this to understand context before acting (don't undo a colleague's work)
    and to correlate metric shifts with edits. Returns a summary by user,
    resource type and operation, plus the most recent change rows.

    Args:
        customer_id: The Google Ads customer ID (digits only, no hyphens).
        days: Lookback window in days (max 30, the API limit).
        limit: Max rows to return (max 10000).
        login_customer_id: Manager (MCC) account ID to use as login-customer-id.
    """
    days = max(1, min(days, CHANGE_HISTORY_MAX_DAYS))
    limit = max(1, min(limit, CHANGE_HISTORY_MAX_LIMIT))
    today = date.today()
    start = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    # change_event requires a FINITE range (both bounds). Upper bound is
    # tomorrow so changes made today are included.
    end = (today + timedelta(days=1)).strftime("%Y-%m-%d")

    query = f"""
        SELECT
            change_event.change_date_time,
            change_event.user_email,
            change_event.client_type,
            change_event.change_resource_type,
            change_event.resource_change_operation,
            change_event.changed_fields,
            change_event.campaign,
            change_event.ad_group
        FROM change_event
        WHERE change_event.change_date_time >= '{start}'
        AND change_event.change_date_time <= '{end}'
        ORDER BY change_event.change_date_time DESC
        LIMIT {limit}
    """
    try:
        rows = _run_query(customer_id, query, login_customer_id)
    except GoogleAdsException as ex:
        raise ToolError(_format_api_error(ex))
    except gax_exceptions.GoogleAPICallError as ex:
        raise ToolError(f"Google Ads API call failed: {getattr(ex, 'message', str(ex))}")

    by_user: Dict[str, int] = {}
    by_resource: Dict[str, int] = {}
    by_operation: Dict[str, int] = {}
    for r in rows:
        # changed_fields comes back as a protobuf FieldMask (not JSON
        # serializable) — flatten it to a plain list of field paths.
        cf = r.get("change_event.changed_fields")
        if cf is not None and hasattr(cf, "paths"):
            r["change_event.changed_fields"] = list(cf.paths)
        user = r.get("change_event.user_email", "?")
        by_user[user] = by_user.get(user, 0) + 1
        rt = r.get("change_event.change_resource_type", "?")
        by_resource[rt] = by_resource.get(rt, 0) + 1
        op = r.get("change_event.resource_change_operation", "?")
        by_operation[op] = by_operation.get(op, 0) + 1

    return {
        "customer_id": customer_id,
        "window_days": days,
        "total_changes": len(rows),
        "by_user": dict(sorted(by_user.items(), key=lambda x: x[1], reverse=True)),
        "by_resource_type": dict(sorted(by_resource.items(), key=lambda x: x[1], reverse=True)),
        "by_operation": by_operation,
        "changes": rows,
    }


# Register tools.
mcp.add_tool(Tool.from_function(
    add_negative_keywords,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
))
mcp.add_tool(Tool.from_function(
    get_change_history,
    annotations=ToolAnnotations(readOnlyHint=True),
))
