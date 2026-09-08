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

from datetime import date, timedelta
from typing import Any, Optional

from ads_mcp.coordinator import mcp
import ads_mcp.utils as utils

MERCHANT_HOST = "https://merchantapi.googleapis.com"
REPORTS_PATH = "/reports/v1/accounts/{account}/reports:search"
ACCOUNTS_PATH = "/accounts/v1/accounts"
ISSUES_PATH = "/accounts/v1/accounts/{account}/issues"
SUBACCOUNTS_PATH = "/accounts/v1/accounts/{account}:listSubaccounts"
DATASOURCES_PATH = "/datasources/v1/accounts/{account}/dataSources"
PROMOTIONS_PATH = "/promotions/v1/accounts/{account}/promotions"
PRODUCT_PATH = "/products/v1/accounts/{account}/products/{product}"

#: The API caps a page at 5000; ask for less by default so one careless call
#: does not pull an entire catalogue into a model's context.
DEFAULT_PAGE_SIZE = 250
MAX_PAGE_SIZE = 1000
#: Bound the total pulled across pages for the same reason.
MAX_ROWS = 5000



def _window(days: int) -> tuple[str, str]:
    """A closed date range ending yesterday, which is the last complete day."""
    end = date.today() - timedelta(days=1)
    return (end - timedelta(days=max(1, days) - 1)).isoformat(), end.isoformat()


def _between(days: int) -> str:
    start, end = _window(days)
    return f"date BETWEEN '{start}' AND '{end}'"


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

@mcp.tool()
def get_product_performance(
    account_id: str,
    days: int = 30,
    limit: int = DEFAULT_PAGE_SIZE,
) -> list[dict]:
    """Clicks, impressions and conversions per product.

    The Merchant Center side of what a Shopping or PMax campaign is doing, at a
    granularity Google Ads does not report: the individual offer.

    Args:
        account_id: Merchant Center account id.
        days: Days back from yesterday. Performance views require a date range.
        limit: Maximum rows.
    """
    query = (
        "SELECT offer_id, title, brand, category_l1, marketing_method, "
        "customer_country_code, clicks, impressions, click_through_rate, "
        "conversions, conversion_value, conversion_rate "
        f"FROM product_performance_view WHERE {_between(days)} "
        "ORDER BY clicks DESC"
    )
    return _search(account_id, query, page_size=limit, max_rows=min(limit, MAX_ROWS))


@mcp.tool()
def get_price_competitiveness(
    account_id: str,
    days: int = 7,
    limit: int = DEFAULT_PAGE_SIZE,
) -> dict:
    """Your price against the benchmark other merchants charge for the same item.

    Where Shopping is lost before the bid: an offer priced well above the
    benchmark can win the auction and still lose the click.

    Args:
        account_id: Merchant Center account id.
        days: Days back from yesterday.
        limit: Maximum rows.
    """
    query = (
        "SELECT offer_id, title, brand, price, benchmark_price, "
        "report_country_code, category_l1 "
        f"FROM price_competitiveness_product_view WHERE {_between(days)}"
    )
    rows = _search(account_id, query, page_size=limit, max_rows=min(limit, MAX_ROWS))

    def _amount(money: Optional[dict]) -> Optional[float]:
        if not money:
            return None
        try:
            return float(money.get("amountMicros", 0)) / 1_000_000
        except (TypeError, ValueError):
            return None

    above, priced = [], 0
    for row in rows:
        view = row.get("priceCompetitivenessProductView", {}) or {}
        ours, mark = _amount(view.get("price")), _amount(view.get("benchmarkPrice"))
        if ours is None or not mark:
            continue
        priced += 1
        if ours > mark:
            above.append(
                {
                    "offer_id": view.get("offerId"),
                    "title": view.get("title"),
                    "price": ours,
                    "benchmark": mark,
                    "above_benchmark_pct": round((ours / mark - 1) * 100, 1),
                    "country": view.get("reportCountryCode"),
                }
            )
    above.sort(key=lambda p: p["above_benchmark_pct"], reverse=True)
    return {
        "account_id": _normalise_account(account_id),
        "products_with_a_benchmark": priced,
        "priced_above_benchmark": len(above),
        "worst_offenders": above[:50],
    }


@mcp.tool()
def get_price_suggestions(account_id: str, limit: int = DEFAULT_PAGE_SIZE) -> list[dict]:
    """Google's suggested prices, with the uplift it predicts for each.

    Read this as a hypothesis to test, not an instruction: the prediction
    assumes everything else stays as it is.

    Args:
        account_id: Merchant Center account id.
        limit: Maximum rows.
    """
    query = (
        "SELECT offer_id, title, brand, price, suggested_price, effectiveness, "
        "predicted_impressions_change_fraction, predicted_clicks_change_fraction, "
        "predicted_conversions_change_fraction FROM price_insights_product_view"
    )
    return _search(account_id, query, page_size=limit, max_rows=min(limit, MAX_ROWS))


@mcp.tool()
def get_best_sellers(
    account_id: str,
    days: int = 30,
    by: str = "products",
    country_code: Optional[str] = None,
    category_id: Optional[int] = None,
    limit: int = 100,
) -> list[dict]:
    """What sells best on Shopping overall, and whether you carry it.

    Demand-side rather than account-side: it describes the market, so it is the
    one place here that can suggest something you do not already stock.

    Args:
        account_id: Merchant Center account id.
        days: Days back from yesterday.
        by: "products" for product clusters, or "brands".
        country_code: Restrict to one market, e.g. "DE".
        category_id: Google product category id.
        limit: Maximum rows.
    """
    if by not in ("products", "brands"):
        raise ValueError('by must be "products" or "brands"')
    if by == "products":
        view = "best_sellers_product_cluster_view"
        fields = ("title, brand, category_l1, variant_gtins, inventory_status, "
                  "brand_inventory_status, rank, previous_rank, relative_demand, "
                  "previous_relative_demand, relative_demand_change")
    else:
        view = "best_sellers_brand_view"
        fields = ("brand, rank, previous_rank, relative_demand, "
                  "previous_relative_demand, relative_demand_change")

    query = f"SELECT report_date, report_granularity, report_country_code, report_category_id, {fields} FROM {view} WHERE {_between(days)}"
    if country_code:
        query += f" AND report_country_code = '{country_code}'"
    if category_id:
        query += f" AND report_category_id = {int(category_id)}"
    query += " ORDER BY rank"
    return _search(account_id, query, page_size=limit, max_rows=min(limit, MAX_ROWS))


@mcp.tool()
def get_competitive_visibility(
    account_id: str,
    days: int = 30,
    kind: str = "competitors",
    country_code: Optional[str] = None,
    category_id: Optional[int] = None,
    limit: int = 100,
) -> list[dict]:
    """Who else appears where you do, and how you rank against them.

    Args:
        account_id: Merchant Center account id.
        days: Days back from yesterday.
        kind: "competitors", "top_merchants" or "benchmark".
        country_code: Restrict to one market, e.g. "DE".
        category_id: Google product category id.
        limit: Maximum rows.
    """
    views = {
        "competitors": (
            "competitive_visibility_competitor_view",
            "domain, is_your_domain, rank, ads_organic_ratio, page_overlap_rate, "
            "higher_position_rate, relative_visibility, traffic_source",
        ),
        "top_merchants": (
            "competitive_visibility_top_merchant_view",
            "domain, is_your_domain, rank, ads_organic_ratio, page_overlap_rate, "
            "higher_position_rate, traffic_source",
        ),
        "benchmark": (
            "competitive_visibility_benchmark_view",
            "your_domain_visibility_trend, category_benchmark_visibility_trend",
        ),
    }
    if kind not in views:
        raise ValueError(f"kind must be one of: {', '.join(views)}")
    view, fields = views[kind]

    query = f"SELECT report_country_code, report_category_id, {fields} FROM {view} WHERE {_between(days)}"
    if country_code:
        query += f" AND report_country_code = '{country_code}'"
    if category_id:
        query += f" AND report_category_id = {int(category_id)}"
    return _search(account_id, query, page_size=limit, max_rows=min(limit, MAX_ROWS))


@mcp.tool()
def get_non_product_performance(account_id: str, days: int = 30) -> list[dict]:
    """Traffic that is not tied to a single product, such as store visits.

    Args:
        account_id: Merchant Center account id.
        days: Days back from yesterday.
    """
    query = (
        "SELECT date, clicks, impressions, click_through_rate "
        f"FROM non_product_performance_view WHERE {_between(days)}"
    )
    return _search(account_id, query, page_size=MAX_PAGE_SIZE, max_rows=MAX_ROWS)


@mcp.tool()
def list_data_sources(account_id: str) -> list[dict]:
    """The feeds behind the catalogue, and how each is fed.

    A product that vanished usually vanished from a feed. This says which feeds
    exist and how they arrive, which is where to look next.

    Args:
        account_id: Merchant Center account id.
    """
    payload = _get(DATASOURCES_PATH.format(account=_normalise_account(account_id)),
                   {"pageSize": 100})
    sources = []
    for source in payload.get("dataSources", []):
        sources.append(
            {
                "name": source.get("name"),
                "display_name": source.get("displayName"),
                "type": next(
                    (k for k in ("primaryProductDataSource", "supplementalProductDataSource",
                                 "localInventoryDataSource", "regionalInventoryDataSource",
                                 "promotionDataSource") if k in source),
                    "unknown",
                ),
                "input": source.get("input"),
                "file_input": source.get("fileInput"),
            }
        )
    return sources


@mcp.tool()
def list_promotions(account_id: str, limit: int = 100) -> list[dict]:
    """Promotions configured on the account.

    Args:
        account_id: Merchant Center account id.
        limit: Maximum rows.
    """
    payload = _get(PROMOTIONS_PATH.format(account=_normalise_account(account_id)),
                   {"pageSize": min(limit, 1000)})
    return payload.get("promotions", [])


@mcp.tool()
def list_sub_accounts(account_id: str) -> list[dict]:
    """Sub-accounts under a multi-client Merchant Center account.

    Args:
        account_id: The advanced (parent) account id.
    """
    payload = _get(SUBACCOUNTS_PATH.format(account=_normalise_account(account_id)),
                   {"pageSize": 500})
    return [
        {
            "account_id": _normalise_account(a.get("name", "")),
            "display_name": a.get("accountName"),
        }
        for a in payload.get("accounts", [])
    ]


@mcp.tool()
def get_product(account_id: str, product_name: str) -> dict:
    """One product's full processed state, including every issue on it.

    Args:
        account_id: Merchant Center account id.
        product_name: The product id, e.g. "online~en~DE~sku123", or a full
            resource name.
    """
    product = product_name.split("/products/")[-1]
    return _get(PRODUCT_PATH.format(account=_normalise_account(account_id), product=product))


@mcp.tool()
def merchant_api_request(
    path: str,
    method: str = "GET",
    body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Any:
    """Calls any Merchant API endpoint directly.

    The escape hatch. The shaped tools above cover the sub-APIs whose paths are
    documented; this reaches anything they do not, and works around a path that
    turns out to be wrong without waiting for a new release.

    Args:
        path: Path after the host, e.g. "/accounts/v1/accounts/123/users".
        method: "GET" or "POST".
        body: JSON body for POST.
        params: Query parameters for GET.
    """
    if not path.startswith("/"):
        path = "/" + path
    verb = method.strip().upper()
    if verb == "GET":
        return _get(path, params)
    if verb == "POST":
        return _post(path, body or {})
    raise ValueError('method must be "GET" or "POST"')
