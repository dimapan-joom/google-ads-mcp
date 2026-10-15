---
name: account-performance-diagnostics
description: Diagnose account performance issues such as conversion loss, low lead flow, and lost opportunities due to ad rank, bids, or budgets. Use this skill to guide queries for troubleshooting performance drops.
---

# Account Performance Diagnostics Skill

This skill provides instructions on how to use the Google Ads MCP server to diagnose common account performance issues.

## Workflows

### 1. Conversion and Conversion Value Loss

When conversions or conversion value suddenly decline, use the following steps to diagnose the issue.

**Steps:**
1.  **Discover Fields**: Use `get_resource_metadata` with resource `campaign` or `ad_group` to ensure you have the correct field names.
2.  **Query Performance**: Use `search` to retrieve performance data.
    *   **Resource**: `campaign` or `ad_group`
    *   **Fields**: Include `campaign.name`, `metrics.conversions`, `metrics.conversion_value`, `metrics.cost_micros`.
    *   **Segments**: To isolate the loss, include segments like `segments.date`, `segments.device`, `segments.conversion_action`.
    *   **Conditions**: Compare the period of decline with a previous period (e.g., `segments.date >= 'YYYY-MM-DD'`).
3.  **Analyze**: Check if the loss is limited to certain devices (e.g., mobile vs desktop) or specific conversion actions.
4.  **Check Uploads**: If using offline conversions, consider checking `offline_conversion_upload_conversion_action_summary` for upload issues.

### 2. Opportunities Lost (Impression Share)

To identify lost opportunities due to ad rank, bids, or budgets, analyze impression share metrics.

**Steps:**
1.  **Query Impression Share**: Use `search` to retrieve impression share metrics.
    *   **Resource**: `campaign`
    *   **Fields**: `campaign.name`, `metrics.search_impression_share`, `metrics.search_rank_lost_impression_share`, `metrics.search_budget_lost_impression_share`.
2.  **Analyze**:
    *   High `search_budget_lost_impression_share` indicates opportunities lost due to limited budget.
    *   High `search_rank_lost_impression_share` indicates opportunities lost due to low ad rank (bid or quality issues).

### 3. Low Lead Flow Diagnostics

When a user asks "why is my lead flow low these past few days?", follow this systematic approach.

**Steps:**
1.  **Confirm Drop**: Query conversions segmented by date for the last few days vs the previous period.
2.  **Isolate Cause**:
    *   Check if **Traffic** (clicks, impressions) dropped.
    *   Check if **Conversion Rate** (conversions/clicks) dropped.
3.  **If Traffic Dropped**: Check Impression Share metrics (see Workflow 2) to see if it's a budget or rank issue, or if search volume generally declined.
4.  **If Conversion Rate Dropped**: Check breakdowns by `segments.device` or `segments.conversion_action` to see if a specific area is failing.
5.  **Check Changes**: Query the `change_event` resource to see if any changes were made to bids, budgets, or targeting around the time the drop started.
    *   *Note*: Queries to `change_event` must specify a LIMIT of less than or equal to 10000.
