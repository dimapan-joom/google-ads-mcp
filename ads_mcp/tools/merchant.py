# Merchant Center tools, added to this fork rather than run as their own server.
#
# They belong here because the caller has already signed in to Google for the
# Ads tools: adding the `content` scope means one sign-in covers both, and a
# separate service would have meant a second login for data that is meaningless
# without the campaign context beside it.
#
# These call the Merchant API over REST rather than through a generated client.
# Content API for Shopping was sunset on 18 August 2026 and began returning
# progressive errors on 1 September, so Merchant API is the only live option;
# REST keeps the dependency list unchanged and reuses the credentials the Ads
# tools already resolve per caller.

from typing import Any, Optional

from ads_mcp.coordinator import mcp
import ads_mcp.utils as utils

MERCHANT_HOST = "https://merchantapi.googleapis.com"
REPORTS_PATH = "/reports/v1/accounts/{account}/reports:search"
ACCOUNTS_PATH = "/accounts/v1/accounts"
ISSUES_PATH = "/accounts/v1/accounts/{account}/issues"

#: The API caps a page at 5000; ask for less by default so one careless call
#: does not pull an entire catalogue into a model's context.
DEFAULT_PAGE_SIZE = 250
MAX_PAGE_SIZE = 1000
#: Bound the total pulled across pages for the same reason.
MAX_ROWS = 5000


def _session():
    """An authorised session on the caller's own Google credentials."""
    from google.auth.transport.requests import AuthorizedSession

    return AuthorizedSession(utils._create_credentials())


def _normalise_account(account: str) -> str:
    """Accepts `123456`, `accounts/123456` or a full resource name."""
    text = str(account).strip()
    if text.startswith("accounts/"):
        text = text.split("/", 1)[1]
    return text.strip()


def _get(path: str, params: Optional[dict] = None) -> dict:
    response = _session().get(MERCHANT_HOST + path, params=params or {}, timeout=60)
    if not response.ok:
        raise RuntimeError(_explain(response))
    return response.json()


def _post(path: str, body: dict) -> dict:
    response = _session().post(MERCHANT_HOST + path, json=body, timeout=120)
    if not response.ok:
        raise RuntimeError(_explain(response))
    return response.json()


def _explain(response) -> str:
    """Turns an API error into something a caller can act on."""
    try:
        detail = response.json().get("error", {}).get("message", "")
    except ValueError:
        detail = response.text[:300]
    if response.status_code == 403 and "content" in detail.lower():
        detail += (
            " — this usually means the sign-in predates Merchant Center support. "
            "Re-authenticate so the token carries the content scope."
        )
    return f"Merchant API returned {response.status_code}: {detail}"


def _search(account: str, query: str, page_size: int, max_rows: int) -> list[dict]:
    """Runs an MCQL query, following pages up to a bound."""
    path = REPORTS_PATH.format(account=_normalise_account(account))
    rows: list[dict] = []
    token: Optional[str] = None
    while True:
        body: dict[str, Any] = {"query": query, "pageSize": min(page_size, MAX_PAGE_SIZE)}
        if token:
            body["pageToken"] = token
        payload = _post(path, body)
        rows.extend(payload.get("results", []))
        token = payload.get("nextPageToken")
        if not token or len(rows) >= max_rows:
            break
    return rows[:max_rows]


@mcp.tool()
def list_merchant_accounts() -> list[dict]:
    """Lists the Merchant Center accounts your Google sign-in can reach.

    Start here: the other Merchant Center tools need an account id, and it is
    not the same number as a Google Ads customer id.
    """
    payload = _get(ACCOUNTS_PATH, {"pageSize": 100})
    accounts = []
    for account in payload.get("accounts", []):
        accounts.append(
            {
                "account_id": _normalise_account(account.get("name", "")),
                "display_name": account.get("accountName"),
                "language": account.get("languageCode"),
                "time_zone": (account.get("timeZone") or {}).get("id"),
            }
        )
    return accounts


@mcp.tool()
def get_merchant_account_issues(account_id: str) -> list[dict]:
    """Account-level problems: policy warnings, suspensions, verification gaps.

    These outrank product issues. A suspended account serves nothing, however
    healthy the feed looks.

    Args:
        account_id: Merchant Center account id, from list_merchant_accounts.
    """
    payload = _get(ISSUES_PATH.format(account=_normalise_account(account_id)))
    issues = []
    for issue in payload.get("accountIssues", []):
        issues.append(
            {
                "title": issue.get("title"),
                "severity": issue.get("severity"),
                "impacted_destinations": issue.get("impactedDestinations"),
                "detail": issue.get("detail"),
                "documentation": issue.get("documentationUri"),
            }
        )
    return issues


@mcp.tool()
def get_disapproved_products(
    account_id: str,
    limit: int = DEFAULT_PAGE_SIZE,
    feed_label: Optional[str] = None,
) -> dict:
    """Products that cannot serve, grouped by why.

    This is where Shopping and PMax budgets leak: a disapproved product still
    costs nothing to bid on but silently removes inventory from the auction, so
    a campaign can look healthy while half its catalogue is invisible.

    Returns both a per-issue tally, which is what to act on, and a sample of
    affected products.

    Args:
        account_id: Merchant Center account id.
        limit: Maximum products to pull. Bounded to keep responses usable.
        feed_label: Restrict to one feed label, e.g. a single country.
    """
    query = (
        "SELECT offer_id, id, title, feed_label, "
        "aggregated_reporting_context_status, item_issues FROM product_view "
        "WHERE aggregated_reporting_context_status = 'NOT_ELIGIBLE_OR_DISAPPROVED'"
    )
    if feed_label:
        query += f" AND feed_label = '{feed_label}'"

    rows = _search(account_id, query, page_size=limit, max_rows=min(limit, MAX_ROWS))

    tally: dict[str, dict] = {}
    products = []
    for row in rows:
        product = row.get("productView", {}) or {}
        issues = product.get("itemIssues", []) or []
        names = []
        for issue in issues:
            issue_type = (issue.get("type") or {})
            code = issue_type.get("code") or "unknown"
            names.append(code)
            entry = tally.setdefault(
                code,
                {
                    "issue": code,
                    "description": issue_type.get("canonicalAttribute") or code,
                    "products_affected": 0,
                    "severity": None,
                    "resolution": None,
                },
            )
            entry["products_affected"] += 1
            for impact in issue.get("impacts", []) or []:
                entry["severity"] = entry["severity"] or impact.get("severity")
            entry["resolution"] = entry["resolution"] or issue.get("resolution")
        products.append(
            {
                "offer_id": product.get("offerId"),
                "title": product.get("title"),
                "feed_label": product.get("feedLabel"),
                "issues": names,
            }
        )

    ranked = sorted(tally.values(), key=lambda i: i["products_affected"], reverse=True)
    return {
        "account_id": _normalise_account(account_id),
        "disapproved_products_found": len(products),
        "truncated": len(rows) >= min(limit, MAX_ROWS),
        "issues_by_frequency": ranked,
        "sample_products": products[:50],
    }


@mcp.tool()
def get_merchant_product_summary(account_id: str) -> dict:
    """How much of the catalogue can actually serve, by status and feed label.

    Use this before reaching for bids: if the eligible share has dropped, the
    problem is the feed, not the campaign.

    Args:
        account_id: Merchant Center account id.
    """
    rows = _search(
        account_id,
        "SELECT aggregated_reporting_context_status, feed_label, offer_id "
        "FROM product_view",
        page_size=MAX_PAGE_SIZE,
        max_rows=MAX_ROWS,
    )
    by_status: dict[str, int] = {}
    by_feed: dict[str, dict[str, int]] = {}
    for row in rows:
        product = row.get("productView", {}) or {}
        status = product.get("aggregatedReportingContextStatus") or "UNKNOWN"
        label = product.get("feedLabel") or "(none)"
        by_status[status] = by_status.get(status, 0) + 1
        by_feed.setdefault(label, {})[status] = by_feed.setdefault(label, {}).get(status, 0) + 1

    counted = sum(by_status.values())
    eligible = by_status.get("ELIGIBLE", 0)
    return {
        "account_id": _normalise_account(account_id),
        "products_counted": counted,
        "truncated": counted >= MAX_ROWS,
        "eligible_share": round(eligible / counted, 4) if counted else None,
        "by_status": by_status,
        "by_feed_label": by_feed,
    }


@mcp.tool()
def search_merchant_report(
    account_id: str,
    query: str,
    limit: int = DEFAULT_PAGE_SIZE,
) -> list[dict]:
    """Runs a raw Merchant Center Query Language query.

    The escape hatch, for anything the shaped tools above do not cover. Views
    include product_view, price_competitiveness_product_view, best_sellers and
    the performance views.

    Args:
        account_id: Merchant Center account id.
        query: An MCQL query, e.g. "SELECT offer_id, title FROM product_view".
        limit: Maximum rows to return.
    """
    return _search(account_id, query, page_size=limit, max_rows=min(limit, MAX_ROWS))
