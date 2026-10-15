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

"""Creative and asset management for every campaign type.

One portable "creative spec" is shared by the read tool and the write tools,
so copying is read -> edit -> write:

  * get_campaign_creatives — everything creative in a campaign: campaign-level
    assets, Performance Max asset groups (assets, signals, listing groups) and,
    for ad-group campaigns (Search, Display, Demand Gen, Video, App), the
    ad-group assets and ads. Each entry is already in the format the write
    tools accept.
  * create_asset_group / copy_asset_group / update_asset_group — Performance
    Max asset groups from scratch, as a copy with replaced creatives, or by
    swapping the creatives of an existing group.
  * create_ad / copy_ad — any responsive ad type (RSA, responsive display,
    Demand Gen multi-asset / video / carousel / product, video responsive, app).
  * link_assets — sitelinks, callouts, structured snippets, calls, promotions,
    prices, images, business name and logos at account, campaign or ad-group
    level.
  * upload_assets — create standalone assets and get their IDs.
  * upload_youtube_video / get_youtube_video_upload_status — put a video file
    (direct or Google Drive link) on YouTube through the Google Ads API, so its
    ID can be used as a YOUTUBE_VIDEO asset.

Asset references ("asset refs") accepted wherever an asset is expected:

  * an existing asset: {"asset": "customers/1/assets/2"} or {"asset_id": "2"}
  * text: "plain string" or {"text": "..."}
  * image: {"image_url": "https://..."} or {"image_base64": "..."}
  * YouTube video: {"youtube_video": "<id or URL>"}
  * call to action: {"call_to_action": "SHOP_NOW"}
  * callout: {"callout": "Free delivery"}
  * sitelink: {"sitelink": {"link_text": ..., "description1": ...,
    "description2": ..., "final_urls": [...]}}
  * structured snippet: {"structured_snippet": {"header": ..., "values": [...]}}
  * any other asset as a raw Asset proto dict: {"promotion_asset": {...},
    "final_urls": [...]}, {"price_asset": {...}}, {"call_asset": {...}}, ...

Plain strings are read in the context of the slot: text for text slots, an
image URL for image slots, a YouTube ID/URL for video slots, a call-to-action
enum for CTA slots. An "asset" reference that belongs to another customer is
ignored when the ref also carries content, so specs read from one account can
be written to another — images are re-uploaded from their URL.

Every write runs as ONE atomic GoogleAdsService.Mutate, defaults to
validate_only=True (Google validates the whole request, nothing is written),
creates new entities PAUSED, and returns a ``log_payload`` for
sheets-log/log_change.
"""

import base64
import io
import os
import re
import struct
import tempfile
from datetime import date
from typing import Any, Dict, List, Literal, Optional, Tuple

import requests
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool
from google.ads.googleads.errors import GoogleAdsException
from google.api_core import exceptions as gax_exceptions
from google.protobuf import json_format
from mcp.types import ToolAnnotations

from ads_mcp.coordinator import mcp
import ads_mcp.utils as utils

# --- Limits -----------------------------------------------------------------

MAX_IMAGE_BYTES = 5_120 * 1024  # Google Ads image asset limit.
IMAGE_FETCH_TIMEOUT_S = 30
ASPECT_TOLERANCE = 0.03
# Video files are streamed to a temporary file, never held in memory; the cap
# bounds the disk they take. Override with GOOGLE_ADS_MCP_MAX_VIDEO_MB.
MAX_VIDEO_BYTES = (
    int(os.environ.get("GOOGLE_ADS_MCP_MAX_VIDEO_MB", "2048")) * 1024 * 1024
)
VIDEO_FETCH_TIMEOUT_S = (10, 60)  # (connect, between bytes)
VIDEO_CHUNK_BYTES = 1024 * 1024
GOOGLE_IMAGE_FORMATS = {"JPEG", "PNG", "GIF"}

# Performance Max asset group: field type -> (min, max, max text length).
# Minimums only produce warnings: retail (feed-only) groups and campaigns with
# brand guidelines legitimately go below them, and the API has the final word.
PMAX_LIMITS: Dict[str, Tuple[int, int, Optional[int]]] = {
    "HEADLINE": (3, 15, 30),
    "LONG_HEADLINE": (1, 5, 90),
    "DESCRIPTION": (2, 5, 90),
    "BUSINESS_NAME": (1, 1, 25),
    "MARKETING_IMAGE": (1, 20, None),
    "SQUARE_MARKETING_IMAGE": (1, 20, None),
    "PORTRAIT_MARKETING_IMAGE": (0, 20, None),
    "TALL_PORTRAIT_MARKETING_IMAGE": (0, 20, None),
    "LOGO": (1, 5, None),
    "LANDSCAPE_LOGO": (0, 5, None),
    "YOUTUBE_VIDEO": (0, 15, None),
    "CALL_TO_ACTION_SELECTION": (0, 1, None),
}

# Text length limits for ad fields and text-like asset types.
TEXT_LIMITS = {
    "HEADLINE": 30,
    "LONG_HEADLINE": 90,
    "DESCRIPTION": 90,
    "BUSINESS_NAME": 25,
    "CALLOUT": 25,
}

# With brand guidelines on, these live on the campaign, not the asset group.
BRAND_FIELD_TYPES = {"BUSINESS_NAME", "LOGO", "LANDSCAPE_LOGO"}

TEXT_FIELD_TYPES = {
    "HEADLINE",
    "LONG_HEADLINE",
    "DESCRIPTION",
    "LONG_DESCRIPTION",
    "BUSINESS_NAME",
    "MANDATORY_AD_TEXT",
    "TEXT_DISCLAIMER",
}
IMAGE_FIELD_TYPES = {
    "MARKETING_IMAGE",
    "SQUARE_MARKETING_IMAGE",
    "PORTRAIT_MARKETING_IMAGE",
    "TALL_PORTRAIT_MARKETING_IMAGE",
    "LOGO",
    "LANDSCAPE_LOGO",
    "BUSINESS_LOGO",
    "AD_IMAGE",
    "CLASSIC_DISPLAY_IMAGE",
}
VIDEO_FIELD_TYPES = {"YOUTUBE_VIDEO", "VIDEO"}

# Aspect ratio routing for the "images" shortcut. Logos are never routed
# automatically: a square logo and a square marketing image look the same.
PMAX_IMAGE_ROUTES = [
    (1.91, "MARKETING_IMAGE"),
    (1.0, "SQUARE_MARKETING_IMAGE"),
    (0.8, "PORTRAIT_MARKETING_IMAGE"),
    (0.5625, "TALL_PORTRAIT_MARKETING_IMAGE"),
]
AD_IMAGE_ROUTES: Dict[str, List[Tuple[float, str]]] = {
    "responsive_display_ad": [
        (1.91, "marketing_images"),
        (1.0, "square_marketing_images"),
    ],
    "demand_gen_multi_asset_ad": [
        (1.91, "marketing_images"),
        (1.0, "square_marketing_images"),
        (0.8, "portrait_marketing_images"),
        (0.5625, "tall_portrait_marketing_images"),
    ],
}

# Text limits of ad fields that are stricter than the 90-char default.
AD_TEXT_LIMITS = {
    ("responsive_search_ad", "headlines"): 30,
    ("responsive_display_ad", "headlines"): 30,
    ("demand_gen_multi_asset_ad", "headlines"): 40,
    ("demand_gen_video_responsive_ad", "headlines"): 40,
    ("demand_gen_carousel_ad", "headline"): 40,
    ("demand_gen_product_ad", "headline"): 40,
    ("demand_gen_video_responsive_ad", "business_name"): 25,
    ("video_responsive_ad", "business_name"): 25,
    ("video_responsive_ad", "call_to_actions"): 10,
    ("app_ad", "headlines"): 30,
}

# Ad types the ad tools handle (Ad oneof field names).
SUPPORTED_AD_TYPES = [
    "responsive_search_ad",
    "responsive_display_ad",
    "demand_gen_multi_asset_ad",
    "demand_gen_video_responsive_ad",
    "demand_gen_carousel_ad",
    "demand_gen_product_ad",
    "video_responsive_ad",
    "app_ad",
    "app_engagement_ad",
]

# Fields of Ad*Asset messages that may be written; the rest are output-only.
_AD_ASSET_WRITABLE = {"text", "pinned_field", "asset", "ad_video_asset_info"}

# Asset fields read for every referenced asset, so specs carry their content.
_ASSET_CONTENT_FIELDS = [
    "asset.resource_name",
    "asset.id",
    "asset.name",
    "asset.type",
    "asset.final_urls",
    "asset.text_asset.text",
    "asset.image_asset.full_size.url",
    "asset.image_asset.full_size.width_pixels",
    "asset.image_asset.full_size.height_pixels",
    "asset.youtube_video_asset.youtube_video_id",
    "asset.youtube_video_asset.youtube_video_title",
    "asset.call_to_action_asset.call_to_action",
    "asset.sitelink_asset.link_text",
    "asset.sitelink_asset.description1",
    "asset.sitelink_asset.description2",
    "asset.callout_asset.callout_text",
    "asset.structured_snippet_asset.header",
    "asset.structured_snippet_asset.values",
    "asset.call_asset.country_code",
    "asset.call_asset.phone_number",
]

_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_URL = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/|live/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})"
)
_ASSET_RN = re.compile(r"^customers/(\d+)/assets/(\d+)$")


# --- Small helpers ----------------------------------------------------------


def _cid(value: Any) -> str:
    cleaned = utils.clean_customer_id(value)
    if not cleaned:
        raise ToolError(f"Invalid ID: {value!r}")
    return cleaned


def _api_error(
    ex: GoogleAdsException, labels: Optional[List[str]] = None
) -> str:
    lines = [f"Request ID: {ex.request_id}"]
    for error in ex.failure.errors:
        where = ""
        elements = list(error.location.field_path_elements)
        if elements and labels is not None:
            first = elements[0]
            if first.field_name == "mutate_operations" and first.index < len(
                labels
            ):
                where = f" [operation {first.index}: {labels[first.index]}]"
        path = ".".join(
            e.field_name + (f"[{e.index}]" if "index" in e else "")
            for e in elements[1:]
        )
        if path:
            where += f" ({path})"
        lines.append(f"Google Ads API Error{where}: {error.message}")
    return "\n".join(lines)


def _query(
    customer_id: str, query: str, login_customer_id: Optional[str]
) -> List[Any]:
    """Runs GAQL and returns raw proto-plus rows."""
    service = utils.get_googleads_service(
        "GoogleAdsService", login_customer_id=login_customer_id
    )
    try:
        rows = []
        for batch in service.search_stream(
            customer_id=customer_id, query=query
        ):
            rows.extend(batch.results)
        return rows
    except GoogleAdsException as ex:
        raise ToolError(_api_error(ex))
    except gax_exceptions.GoogleAPICallError as ex:
        raise ToolError(
            f"Google Ads API call failed: {getattr(ex, 'message', str(ex))}"
        )


def _to_dict(message: Any) -> Dict[str, Any]:
    """Proto-plus or protobuf message -> dict with proto field names."""
    pb = type(message).pb(message) if hasattr(type(message), "pb") else message
    return json_format.MessageToDict(pb, preserving_proto_field_name=True)


def _enum_name(value: Any) -> Any:
    return getattr(value, "name", value)


def _youtube_id(value: str) -> str:
    value = (value or "").strip()
    if _YOUTUBE_ID.match(value):
        return value
    match = _YOUTUBE_URL.search(value)
    if match:
        return match.group(1)
    raise ToolError(f"Not a YouTube video ID or URL: {value!r}")


def _route_by_aspect(
    width: int, height: int, routes: List[Tuple[float, str]]
) -> Optional[str]:
    if not width or not height:
        return None
    ratio = width / height
    for target, slot in routes:
        if abs(ratio - target) / target <= ASPECT_TOLERANCE:
            return slot
    return None


def _check_text(field: str, text: str, limit: Optional[int]) -> None:
    if not text or not text.strip():
        raise ToolError(f"{field}: empty text")
    if limit and len(text) > limit:
        raise ToolError(f"{field}: '{text}' is {len(text)} chars, max {limit}")


# --- Images -----------------------------------------------------------------


def _load_image(ref: Dict[str, Any]) -> Tuple[bytes, int, int]:
    """Fetches/decodes an image ref and returns (bytes, width, height).

    Formats Google Ads does not accept (WebP, ...) are converted to PNG.
    """
    from PIL import Image

    if ref.get("image_base64"):
        try:
            data = base64.b64decode(ref["image_base64"], validate=True)
        except ValueError as ex:
            raise ToolError(f"image_base64 is not valid base64: {ex}")
        source = "image_base64"
    else:
        url = ref["image_url"]
        source = url
        try:
            response = requests.get(
                url, timeout=IMAGE_FETCH_TIMEOUT_S, stream=True
            )
            response.raise_for_status()
            data = response.raw.read(
                MAX_IMAGE_BYTES * 4 + 1, decode_content=True
            )
        except requests.RequestException as ex:
            raise ToolError(f"Could not download image {url}: {ex}")

    try:
        image = Image.open(io.BytesIO(data))
        width, height = image.size
        image_format = image.format
    except Exception as ex:  # Pillow raises several unrelated types.
        raise ToolError(f"Not a readable image ({source}): {ex}")

    if image_format not in GOOGLE_IMAGE_FORMATS:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        data = buffer.getvalue()
    if len(data) > MAX_IMAGE_BYTES:
        raise ToolError(
            f"Image {source} is {len(data) // 1024} KB; Google Ads allows "
            f"{MAX_IMAGE_BYTES // 1024} KB"
        )
    return data, width, height


# --- The atomic batch -------------------------------------------------------


class _Batch:
    """Operations for one atomic GoogleAdsService.Mutate.

    New resources get temporary (negative) IDs so later operations in the same
    request can reference them. Identical asset content is created once.
    """

    def __init__(self, customer_id: str, login_customer_id: Optional[str]):
        self.customer_id = customer_id
        self.login_customer_id = login_customer_id
        self.operations: List[Dict[str, Any]] = []
        self.labels: List[str] = []
        self._next_temp_id = -1
        self._asset_cache: Dict[str, str] = {}
        self._image_info: Dict[str, Tuple[int, int]] = {}
        self.new_assets = 0

    def temp_id(self) -> int:
        value = self._next_temp_id
        self._next_temp_id -= 1
        return value

    def add(self, kind: str, payload: Dict[str, Any], label: str) -> None:
        self.operations.append({kind: payload})
        self.labels.append(label)

    # Asset refs ------------------------------------------------------------

    def _existing(self, ref: Dict[str, Any]) -> Optional[str]:
        """Resource name of a reusable existing asset in this customer, if any."""
        if ref.get("asset"):
            match = _ASSET_RN.match(str(ref["asset"]))
            if not match:
                raise ToolError(f"Bad asset resource name: {ref['asset']!r}")
            if match.group(1) == self.customer_id:
                return ref["asset"]
            return None
        if ref.get("asset_id"):
            return (
                f"customers/{self.customer_id}/assets/{_cid(ref['asset_id'])}"
            )
        return None

    def asset(self, ref: Any, slot: str = "") -> str:
        """Returns the asset resource name for a ref, creating it if needed.

        ``slot`` is a field type or ad field name used to interpret strings and
        to validate text length.
        """
        ref = normalize_ref(ref, slot)
        existing = self._existing(ref)
        if existing:
            return existing
        if ref.get("asset") and not _has_content(ref):
            raise ToolError(
                f"Asset {ref['asset']} belongs to another customer and the ref "
                "has no content (text/image_url/youtube_video) to recreate it"
            )

        asset, key = self._asset_payload(ref, slot)
        if key in self._asset_cache:
            return self._asset_cache[key]
        resource_name = f"customers/{self.customer_id}/assets/{self.temp_id()}"
        asset["resource_name"] = resource_name
        self.add("asset_operation", {"create": asset}, f"asset:{key[:60]}")
        self._asset_cache[key] = resource_name
        self.new_assets += 1
        return resource_name

    def image_size(self, ref: Any) -> Tuple[int, int]:
        """(width, height) for an image ref; downloads it if not known."""
        ref = normalize_ref(ref, "MARKETING_IMAGE")
        if ref.get("width") and ref.get("height"):
            return int(ref["width"]), int(ref["height"])
        existing = self._existing(ref)
        if existing:
            views = _assets_by_rn(
                self.customer_id, [existing], self.login_customer_id
            )
            view = views.get(existing)
            if not view or view.get("type") != "IMAGE":
                raise ToolError(
                    f"{existing} is not an image asset of customer {self.customer_id}"
                )
            return int(view["width"]), int(view["height"])
        key = ref.get("image_url") or ref.get("image_base64", "")[:64]
        if key and key not in self._image_info:
            _, width, height = _load_image(ref)
            self._image_info[key] = (width, height)
        if key:
            return self._image_info[key]
        return 0, 0

    def _asset_payload(
        self, ref: Dict[str, Any], slot: str
    ) -> Tuple[Dict[str, Any], str]:
        name = ref.get("name")
        base: Dict[str, Any] = {}
        if name:
            base["name"] = name
        if ref.get("final_urls"):
            base["final_urls"] = ref["final_urls"]

        if "text" in ref:
            _check_text(slot or "text", ref["text"], TEXT_LIMITS.get(slot))
            return {
                **base,
                "text_asset": {"text": ref["text"]},
            }, f"text:{ref['text']}"
        if ref.get("image_url") or ref.get("image_base64"):
            data, width, height = _load_image(ref)
            key = ref.get("image_url") or ref["image_base64"][:64]
            self._image_info[key] = (width, height)
            if "name" not in base:
                base["name"] = (
                    f"mcp image {width}x{height} {date.today().isoformat()}"
                )
            return (
                {
                    **base,
                    "type": "IMAGE",
                    "image_asset": {"data": base64.b64encode(data).decode()},
                },
                f"image:{key}",
            )
        if ref.get("youtube_video"):
            video_id = _youtube_id(ref["youtube_video"])
            return {
                **base,
                "youtube_video_asset": {"youtube_video_id": video_id},
            }, f"video:{video_id}"
        if ref.get("call_to_action"):
            cta = str(ref["call_to_action"]).upper()
            return {
                **base,
                "call_to_action_asset": {"call_to_action": cta},
            }, f"cta:{cta}"
        if "callout" in ref:
            _check_text("CALLOUT", ref["callout"], TEXT_LIMITS["CALLOUT"])
            return {
                **base,
                "callout_asset": {"callout_text": ref["callout"]},
            }, f"callout:{ref['callout']}"
        if ref.get("sitelink"):
            sitelink = dict(ref["sitelink"])
            final_urls = sitelink.pop("final_urls", None) or base.get(
                "final_urls"
            )
            if not final_urls:
                raise ToolError(
                    f"Sitelink '{sitelink.get('link_text')}' needs final_urls"
                )
            _check_text("sitelink.link_text", sitelink.get("link_text", ""), 25)
            for field in ("description1", "description2"):
                if sitelink.get(field):
                    _check_text(f"sitelink.{field}", sitelink[field], 35)
            return (
                {**base, "final_urls": final_urls, "sitelink_asset": sitelink},
                f"sitelink:{sitelink.get('link_text')}:{final_urls[0]}",
            )
        if ref.get("structured_snippet"):
            snippet = ref["structured_snippet"]
            return (
                {**base, "structured_snippet_asset": snippet},
                f"snippet:{snippet.get('header')}:{'|'.join(snippet.get('values', []))}",
            )
        raw = {k: v for k, v in ref.items() if k.endswith("_asset")}
        if len(raw) == 1:
            payload = {**base, **raw}
            return (
                payload,
                f"raw:{sorted(raw)[0]}:{hash(repr(sorted(payload.items())))}",
            )
        raise ToolError(
            f"Cannot interpret asset ref for {slot or 'asset'}: {ref!r}"
        )

    # Execution -------------------------------------------------------------

    def execute(self, validate_only: bool) -> List[str]:
        """Sends the batch; returns resource names in operation order."""
        if not self.operations:
            raise ToolError(
                "Nothing to change: the request produced no operations"
            )
        # Google checks a Performance Max asset group's minimums against the
        # assets that exist when each link is processed, so every new asset
        # has to come before the asset group and its links.
        order = sorted(
            range(len(self.operations)),
            key=lambda i: "asset_operation" not in self.operations[i],
        )
        self.operations = [self.operations[i] for i in order]
        self.labels = [self.labels[i] for i in order]
        client = utils.get_googleads_client(
            login_customer_id=self.login_customer_id
        )
        operations = []
        for index, payload in enumerate(self.operations):
            operation = client.get_type("MutateOperation")
            try:
                json_format.ParseDict(payload, type(operation).pb(operation))
            except json_format.ParseError as ex:
                raise ToolError(
                    f"Invalid operation {index} ({self.labels[index]}): {ex}"
                )
            operations.append(operation)

        service = client.get_service("GoogleAdsService")
        try:
            response = service.mutate(
                request={
                    "customer_id": self.customer_id,
                    "mutate_operations": operations,
                    "validate_only": validate_only,
                }
            )
        except GoogleAdsException as ex:
            raise ToolError(_api_error(ex, self.labels))
        except gax_exceptions.GoogleAPICallError as ex:
            raise ToolError(
                f"Google Ads API call failed: {getattr(ex, 'message', str(ex))}"
            )

        names = []
        for result in response.mutate_operation_responses:
            values = list(_to_dict(result).values())
            names.append(values[0].get("resource_name", "") if values else "")
        return names

    def summary(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for payload in self.operations:
            kind = next(iter(payload))
            verb = next(iter(payload[kind]))
            key = f"{kind.removesuffix('_operation')}.{verb}"
            counts[key] = counts.get(key, 0) + 1
        return counts


def _name_for(batch: "_Batch", names: List[str], label_prefix: str) -> str:
    """Resource name returned for the first operation with this label."""
    for label, name in zip(batch.labels, names):
        if label.startswith(label_prefix):
            return name
    return ""


def normalize_ref(ref: Any, slot: str = "") -> Dict[str, Any]:
    """Turns a shorthand ref (plain string) into a dict, based on the slot."""
    if isinstance(ref, dict):
        return ref
    if not isinstance(ref, str):
        raise ToolError(f"Asset ref must be a string or object, got {ref!r}")
    if _ASSET_RN.match(ref):
        return {"asset": ref}
    slot_upper = slot.upper()
    if (
        slot_upper in IMAGE_FIELD_TYPES
        or "IMAGE" in slot_upper
        or "LOGO" in slot_upper
        or slot_upper.endswith("BANNERS")
    ):
        return {"image_url": ref}
    if slot_upper in VIDEO_FIELD_TYPES or "VIDEO" in slot_upper:
        return {"youtube_video": ref}
    if slot_upper in ("CALL_TO_ACTION_SELECTION", "CALL_TO_ACTION") or slot in (
        "call_to_action",
        "call_to_actions_asset",
    ):
        return {"call_to_action": ref}
    if slot_upper == "CALLOUT":
        return {"callout": ref}
    return {"text": ref}


def _has_content(ref: Dict[str, Any]) -> bool:
    keys = {
        "text",
        "image_url",
        "image_base64",
        "youtube_video",
        "call_to_action",
        "callout",
        "sitelink",
        "structured_snippet",
    }
    return bool(keys & ref.keys()) or any(k.endswith("_asset") for k in ref)


def _result(
    batch: _Batch,
    validate_only: bool,
    names: List[str],
    action: str,
    target: Dict[str, Any],
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "validate_only": validate_only,
        "status": "VALIDATED (nothing written)" if validate_only else "APPLIED",
        "operations": batch.summary(),
        "new_assets": batch.new_assets,
    }
    if extra:
        result.update(extra)
    if validate_only:
        result["next_step"] = (
            "Google accepted the whole request. Show the plan to the user and, "
            "after explicit confirmation, call again with validate_only=false."
        )
    else:
        result["created_or_changed"] = [
            {"operation": label, "resource_name": name}
            for label, name in zip(batch.labels, names)
            if name and not label.startswith("asset:")
        ]
        result["log_payload"] = {
            "platform": "google_ads",
            "customer_id": batch.customer_id,
            "action": action,
            **target,
            "operations": batch.summary(),
        }
    return result


# --- Reading ----------------------------------------------------------------


def _campaign_info(
    customer_id: str, campaign_id: str, login_customer_id: Optional[str]
) -> Dict[str, Any]:
    rows = _query(
        customer_id,
        f"""
        SELECT campaign.id, campaign.name, campaign.status,
               campaign.advertising_channel_type,
               campaign.advertising_channel_sub_type,
               campaign.brand_guidelines_enabled,
               campaign.shopping_setting.merchant_id
        FROM campaign WHERE campaign.id = {_cid(campaign_id)}
        """,
        login_customer_id,
    )
    if not rows:
        raise ToolError(
            f"Campaign {campaign_id} not found in customer {customer_id}"
        )
    campaign = rows[0].campaign
    return {
        "id": str(campaign.id),
        "name": campaign.name,
        "status": _enum_name(campaign.status),
        "type": _enum_name(campaign.advertising_channel_type),
        "sub_type": _enum_name(campaign.advertising_channel_sub_type),
        "brand_guidelines_enabled": bool(campaign.brand_guidelines_enabled),
        "merchant_id": str(campaign.shopping_setting.merchant_id or "") or None,
    }


def _asset_view(asset: Any) -> Dict[str, Any]:
    """A readable, re-creatable asset ref from an asset row."""
    view: Dict[str, Any] = {
        "asset": asset.resource_name,
        "asset_id": str(asset.id),
        "type": _enum_name(asset.type_),
    }
    if asset.name:
        view["name"] = asset.name
    kind = view["type"]
    if kind == "TEXT":
        view["text"] = asset.text_asset.text
    elif kind == "IMAGE":
        full = asset.image_asset.full_size
        view.update(
            image_url=full.url,
            width=full.width_pixels,
            height=full.height_pixels,
        )
    elif kind == "YOUTUBE_VIDEO":
        view["youtube_video"] = asset.youtube_video_asset.youtube_video_id
        if asset.youtube_video_asset.youtube_video_title:
            view["title"] = asset.youtube_video_asset.youtube_video_title
    elif kind == "CALL_TO_ACTION":
        view["call_to_action"] = _enum_name(
            asset.call_to_action_asset.call_to_action
        )
    elif kind == "SITELINK":
        sitelink = {
            "link_text": asset.sitelink_asset.link_text,
            "final_urls": list(asset.final_urls),
        }
        for field in ("description1", "description2"):
            if getattr(asset.sitelink_asset, field):
                sitelink[field] = getattr(asset.sitelink_asset, field)
        view["sitelink"] = sitelink
    elif kind == "CALLOUT":
        view["callout"] = asset.callout_asset.callout_text
    elif kind == "STRUCTURED_SNIPPET":
        view["structured_snippet"] = {
            "header": asset.structured_snippet_asset.header,
            "values": list(asset.structured_snippet_asset.values),
        }
    elif kind == "CALL":
        view["call_asset"] = {
            "country_code": asset.call_asset.country_code,
            "phone_number": asset.call_asset.phone_number,
        }
    return view


def _assets_by_rn(
    customer_id: str,
    resource_names: List[str],
    login_customer_id: Optional[str],
) -> Dict[str, Dict[str, Any]]:
    ids = sorted(
        {m.group(2) for rn in resource_names if (m := _ASSET_RN.match(rn))}
    )
    found: Dict[str, Dict[str, Any]] = {}
    for start in range(0, len(ids), 500):
        chunk = ", ".join(ids[start : start + 500])
        rows = _query(
            customer_id,
            f"SELECT {', '.join(_ASSET_CONTENT_FIELDS)} FROM asset WHERE asset.id IN ({chunk})",
            login_customer_id,
        )
        for row in rows:
            found[row.asset.resource_name] = _asset_view(row.asset)
    return found


def _link_rows(
    customer_id: str,
    resource: str,
    where: str,
    login_customer_id: Optional[str],
    extra_fields: str = "",
) -> List[Any]:
    fields = f"{resource}.resource_name, {resource}.field_type, {resource}.status, {resource}.source"
    if extra_fields:
        fields += ", " + extra_fields
    return _query(
        customer_id,
        f"SELECT {fields}, {', '.join(_ASSET_CONTENT_FIELDS)} FROM {resource} "
        f"WHERE {where} AND {resource}.status != 'REMOVED'",
        login_customer_id,
    )


def _grouped_assets(
    rows: List[Any], resource: str
) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        link = getattr(row, resource)
        view = _asset_view(row.asset)
        view["link"] = link.resource_name
        source = _enum_name(link.source)
        if source and source not in ("ADVERTISER", "UNSPECIFIED", "UNKNOWN"):
            view["source"] = source
        grouped.setdefault(_enum_name(link.field_type), []).append(view)
    return grouped


def _read_asset_groups(
    customer_id: str,
    campaign_id: str,
    asset_group_ids: Optional[List[str]],
    login_customer_id: Optional[str],
) -> List[Dict[str, Any]]:
    campaign_rn = f"customers/{customer_id}/campaigns/{_cid(campaign_id)}"
    where = f"asset_group.campaign = '{campaign_rn}' AND asset_group.status != 'REMOVED'"
    if asset_group_ids:
        where += f" AND asset_group.id IN ({', '.join(_cid(i) for i in asset_group_ids)})"
    rows = _query(
        customer_id,
        f"""
        SELECT asset_group.id, asset_group.resource_name, asset_group.name,
               asset_group.status, asset_group.final_urls,
               asset_group.final_mobile_urls, asset_group.path1,
               asset_group.path2, asset_group.ad_strength,
               asset_group.primary_status
        FROM asset_group WHERE {where}
        """,
        login_customer_id,
    )
    groups: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        group = row.asset_group
        spec: Dict[str, Any] = {
            "asset_group_id": str(group.id),
            "name": group.name,
            "status": _enum_name(group.status),
            "final_urls": list(group.final_urls),
            "ad_strength": _enum_name(group.ad_strength),
            "primary_status": _enum_name(group.primary_status),
            "assets": {},
            "signals": {"search_themes": [], "audiences": []},
            "listing_group_filters": [],
        }
        if group.final_mobile_urls:
            spec["final_mobile_urls"] = list(group.final_mobile_urls)
        for field in ("path1", "path2"):
            if getattr(group, field):
                spec[field] = getattr(group, field)
        groups[group.resource_name] = spec
    if not groups:
        return []

    group_filter = ", ".join(f"'{rn}'" for rn in groups)
    for row in _link_rows(
        customer_id,
        "asset_group_asset",
        f"asset_group_asset.asset_group IN ({group_filter})",
        login_customer_id,
        extra_fields="asset_group_asset.asset_group",
    ):
        link = row.asset_group_asset
        view = _asset_view(row.asset)
        view["link"] = link.resource_name
        source = _enum_name(link.source)
        if source and source not in ("ADVERTISER", "UNSPECIFIED", "UNKNOWN"):
            view["source"] = source
        groups[link.asset_group]["assets"].setdefault(
            _enum_name(link.field_type), []
        ).append(view)

    for row in _query(
        customer_id,
        f"""
        SELECT asset_group_signal.asset_group, asset_group_signal.audience.audience,
               asset_group_signal.search_theme.text
        FROM asset_group_signal WHERE asset_group_signal.asset_group IN ({group_filter})
        """,
        login_customer_id,
    ):
        signal = row.asset_group_signal
        signals = groups[signal.asset_group]["signals"]
        if signal.search_theme.text:
            signals["search_themes"].append(signal.search_theme.text)
        elif signal.audience.audience:
            signals["audiences"].append(signal.audience.audience)

    for row in _query(
        customer_id,
        f"""
        SELECT asset_group_listing_group_filter.asset_group,
               asset_group_listing_group_filter.id,
               asset_group_listing_group_filter.type,
               asset_group_listing_group_filter.listing_source,
               asset_group_listing_group_filter.parent_listing_group_filter,
               {_case_value_fields()}
        FROM asset_group_listing_group_filter
        WHERE asset_group_listing_group_filter.asset_group IN ({group_filter})
        """,
        login_customer_id,
    ):
        node = row.asset_group_listing_group_filter
        entry: Dict[str, Any] = {
            "id": str(node.id),
            "type": _enum_name(node.type_),
            "listing_source": _enum_name(node.listing_source),
        }
        case_value = _to_dict(node.case_value)
        if case_value:
            entry["case_value"] = case_value
        if node.parent_listing_group_filter:
            entry["parent_id"] = node.parent_listing_group_filter.rsplit(
                "~", 1
            )[-1]
        groups[node.asset_group]["listing_group_filters"].append(entry)

    return list(groups.values())


def _leaf_fields(descriptor: Any, prefix: str) -> List[str]:
    """Selectable leaf paths of a message: GAQL refuses composite fields."""
    fields = []
    for field in descriptor.fields:
        path = f"{prefix}.{field.name}"
        if field.message_type is None:
            fields.append(path)
        elif not field.is_repeated:
            fields.extend(_leaf_fields(field.message_type, path))
    return fields


def _case_value_fields() -> str:
    client = utils.get_googleads_client()
    dimension = client.get_type("ListingGroupFilterDimension")
    descriptor = type(dimension).pb(dimension).DESCRIPTOR
    return ", ".join(
        _leaf_fields(descriptor, "asset_group_listing_group_filter.case_value")
    )


def _ad_select_fields(
    client: Any, ad_types: List[str] = SUPPORTED_AD_TYPES
) -> List[str]:
    ad_pb = type(client.get_type("Ad")).pb(client.get_type("Ad"))
    fields = []
    for ad_type in ad_types:
        info = ad_pb.DESCRIPTOR.fields_by_name[ad_type].message_type
        for field in info.fields:
            is_ad_asset = (
                field.message_type is not None
                and field.message_type.name.startswith("Ad")
            )
            if field.message_type is None or is_ad_asset:
                fields.append(f"ad_group_ad.ad.{ad_type}.{field.name}")
    return fields


def _ad_spec_from_row(row: Any) -> Dict[str, Any]:
    ad = row.ad_group_ad.ad
    ad_dict = _to_dict(ad)
    ad_type = next((t for t in SUPPORTED_AD_TYPES if t in ad_dict), None)
    spec: Dict[str, Any] = {
        "ad_id": str(ad.id),
        "ad_group_id": str(row.ad_group.id),
        "status": _enum_name(row.ad_group_ad.status),
        "ad_type": _enum_name(ad.type_),
        "type": ad_type,
        "final_urls": list(ad.final_urls),
    }
    if ad.name:
        spec["name"] = ad.name
    if ad.final_mobile_urls:
        spec["final_mobile_urls"] = list(ad.final_mobile_urls)
    if ad_type:
        spec["fields"] = _clean_ad_fields(ad_dict[ad_type])
    else:
        spec["note"] = "Ad type not supported by create_ad/copy_ad; read-only."
    return spec


def _clean_ad_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Drops output-only parts of Ad*Asset entries."""

    def clean(value: Any) -> Any:
        if isinstance(value, dict) and ({"text", "asset"} & value.keys()):
            return {k: v for k, v in value.items() if k in _AD_ASSET_WRITABLE}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value

    return {key: clean(value) for key, value in fields.items()}


def _enrich_ad_fields(
    ads: List[Dict[str, Any]], assets: Dict[str, Dict[str, Any]]
) -> None:
    """Adds the asset content next to each {"asset": rn} entry in ad fields."""

    def enrich(value: Any) -> Any:
        if (
            isinstance(value, dict)
            and "asset" in value
            and value["asset"] in assets
        ):
            view = assets[value["asset"]]
            return {
                **{k: v for k, v in view.items() if k not in ("type",)},
                **value,
            }
        if isinstance(value, list):
            return [enrich(v) for v in value]
        return value

    for ad in ads:
        if "fields" in ad:
            ad["fields"] = {k: enrich(v) for k, v in ad["fields"].items()}


def _collect_asset_rns(value: Any, out: List[str]) -> None:
    if isinstance(value, dict):
        if isinstance(value.get("asset"), str):
            out.append(value["asset"])
        for item in value.values():
            _collect_asset_rns(item, out)
    elif isinstance(value, list):
        for item in value:
            _collect_asset_rns(item, out)


def _read_ads(
    customer_id: str,
    where: str,
    login_customer_id: Optional[str],
    errors: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    base = [
        "ad_group.id",
        "ad_group_ad.status",
        "ad_group_ad.ad.id",
        "ad_group_ad.ad.type",
        "ad_group_ad.ad.name",
        "ad_group_ad.ad.final_urls",
        "ad_group_ad.ad.final_mobile_urls",
    ]

    def run(fields: List[str], extra_where: str = "") -> List[Any]:
        return _query(
            customer_id,
            f"SELECT {', '.join(base + fields)} FROM ad_group_ad WHERE {where} "
            f"AND ad_group_ad.status != 'REMOVED'{extra_where}",
            login_customer_id,
        )

    try:
        rows = run(_ad_select_fields(client))
    except ToolError:
        # One field the API refuses to select would hide every ad; read type
        # by type so only the offending type is lost, and say so.
        rows = []
        for ad_type in SUPPORTED_AD_TYPES:
            try:
                rows.extend(
                    run(
                        _ad_select_fields(client, [ad_type]),
                        f" AND ad_group_ad.ad.type = '{ad_type.upper()}'",
                    )
                )
            except ToolError as ex:
                if errors is not None:
                    errors.append(f"{ad_type}: {ex}")
    ads = [_ad_spec_from_row(row) for row in rows]
    rns: List[str] = []
    _collect_asset_rns([ad.get("fields", {}) for ad in ads], rns)
    if rns:
        _enrich_ad_fields(
            ads, _assets_by_rn(customer_id, rns, login_customer_id)
        )
    return ads


def get_campaign_creatives(
    customer_id: str,
    campaign_id: str,
    asset_group_ids: Optional[List[str]] = None,
    ad_group_ids: Optional[List[str]] = None,
    include_ads: bool = True,
    login_customer_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Read every creative in a campaign, in the format the write tools accept.

    Works for all campaign types. Returns the campaign (type, brand-guidelines
    flag, merchant), campaign-level assets (sitelinks, callouts, business name,
    logos, ...), and then either Performance Max asset groups (assets by field
    type, final URLs, search themes, audiences, listing group tree) or ad
    groups with their ad-group-level assets and ads (RSA, responsive display,
    Demand Gen, video, app).

    Every asset carries both its resource name and its content (text, image URL
    and size, YouTube ID, ...). An asset group entry can be edited and passed
    straight to create_asset_group / copy_asset_group overrides; an ad entry to
    create_ad / copy_ad.

    Args:
        customer_id: Google Ads customer ID (digits only).
        campaign_id: Campaign ID.
        asset_group_ids: Only these asset groups (Performance Max).
        ad_group_ids: Only these ad groups (other campaign types).
        include_ads: Read ads of ad-group campaigns (default true).
        login_customer_id: Manager (MCC) account ID to use as login-customer-id.
    """
    customer_id = _cid(customer_id)
    campaign = _campaign_info(customer_id, campaign_id, login_customer_id)
    result: Dict[str, Any] = {"customer_id": customer_id, "campaign": campaign}

    rows = _link_rows(
        customer_id,
        "campaign_asset",
        # GAQL wants a filtered field of another resource in SELECT too, so
        # filter on the link's own field.
        f"campaign_asset.campaign = "
        f"'customers/{customer_id}/campaigns/{_cid(campaign_id)}'",
        login_customer_id,
    )
    result["campaign_assets"] = _grouped_assets(rows, "campaign_asset")

    if campaign["type"] == "PERFORMANCE_MAX":
        result["asset_groups"] = _read_asset_groups(
            customer_id, campaign_id, asset_group_ids, login_customer_id
        )
        return result

    where = (
        f"ad_group.campaign = 'customers/{customer_id}/campaigns/"
        f"{_cid(campaign_id)}' AND ad_group.status != 'REMOVED'"
    )
    if ad_group_ids:
        where += (
            f" AND ad_group.id IN ({', '.join(_cid(i) for i in ad_group_ids)})"
        )
    group_rows = _query(
        customer_id,
        f"SELECT ad_group.id, ad_group.name, ad_group.status, ad_group.type FROM ad_group WHERE {where}",
        login_customer_id,
    )
    ad_groups: Dict[str, Dict[str, Any]] = {}
    for row in group_rows:
        ad_groups[str(row.ad_group.id)] = {
            "ad_group_id": str(row.ad_group.id),
            "name": row.ad_group.name,
            "status": _enum_name(row.ad_group.status),
            "type": _enum_name(row.ad_group.type_),
            "assets": {},
            "ads": [],
        }
    if ad_groups:
        id_list = ", ".join(ad_groups)
        for row in _link_rows(
            customer_id,
            "ad_group_asset",
            f"ad_group.id IN ({id_list})",
            login_customer_id,
            extra_fields="ad_group.id",
        ):
            link = row.ad_group_asset
            view = _asset_view(row.asset)
            view["link"] = link.resource_name
            ad_groups[str(row.ad_group.id)]["assets"].setdefault(
                _enum_name(link.field_type), []
            ).append(view)
        if include_ads:
            errors: List[str] = []
            for ad in _read_ads(
                customer_id,
                f"ad_group.id IN ({id_list})",
                login_customer_id,
                errors,
            ):
                ad_groups[ad["ad_group_id"]]["ads"].append(ad)
            if errors:
                result["read_errors"] = errors
    result["ad_groups"] = list(ad_groups.values())
    return result


# --- Asset groups -----------------------------------------------------------


def _normalize_group_assets(
    batch: _Batch, spec: Dict[str, Any]
) -> Dict[str, List[Dict[str, Any]]]:
    """Field type -> refs, with the "images" shortcut routed by aspect ratio."""
    assets: Dict[str, List[Dict[str, Any]]] = {}
    for field_type, refs in (spec.get("assets") or {}).items():
        field_type = field_type.upper()
        if not isinstance(refs, list):
            refs = [refs]
        assets[field_type] = [normalize_ref(r, field_type) for r in refs]
    for ref in spec.get("images") or []:
        ref = normalize_ref(ref, "MARKETING_IMAGE")
        width, height = batch.image_size(ref)
        slot = _route_by_aspect(width, height, PMAX_IMAGE_ROUTES)
        if not slot:
            raise ToolError(
                f"Image {ref.get('image_url') or ref.get('asset')} is {width}x{height}; "
                "Performance Max takes 1.91:1, 1:1, 4:5 or 9:16. Pass it under "
                "assets.<FIELD_TYPE> to place it explicitly (e.g. LOGO)."
            )
        assets.setdefault(slot, []).append(ref)
    return assets


def _check_group_limits(
    assets: Dict[str, List[Dict[str, Any]]], warnings: List[str], skip: set
) -> None:
    for field_type, (minimum, maximum, max_len) in PMAX_LIMITS.items():
        if field_type in skip:
            continue
        refs = assets.get(field_type, [])
        if len(refs) > maximum:
            raise ToolError(f"{field_type}: {len(refs)} assets, max {maximum}")
        if len(refs) < minimum:
            warnings.append(
                f"{field_type}: {len(refs)} assets, Google usually needs {minimum}"
            )
        if max_len:
            for ref in refs:
                if "text" in ref and ref.get("asset") is None:
                    _check_text(field_type, ref["text"], max_len)
    descriptions = [r.get("text", "") for r in assets.get("DESCRIPTION", [])]
    if descriptions and all(len(text) > 60 for text in descriptions):
        warnings.append(
            "DESCRIPTION: Performance Max needs one description of 60 chars or less"
        )


def _add_listing_tree(
    batch: _Batch, group_rn: str, nodes: List[Dict[str, Any]]
) -> None:
    """Adds listing group filter creates, parents before children.

    Children address their parent as "<asset group id>~<filter id>"; Google
    accepts temporary IDs on both sides, so a new group and its whole tree go
    in one request.
    """
    by_id = {node["id"]: node for node in nodes}
    order: List[Dict[str, Any]] = []
    seen: set = set()

    def visit(node: Dict[str, Any]) -> None:
        if node["id"] in seen:
            return
        parent = node.get("parent_id")
        if parent and parent in by_id:
            visit(by_id[parent])
        seen.add(node["id"])
        order.append(node)

    for node in nodes:
        visit(node)
    temp_ids: Dict[str, int] = {}
    group_id = group_rn.rsplit("/", 1)[-1]
    customer = batch.customer_id
    for node in order:
        temp = batch.temp_id()
        temp_ids[node["id"]] = temp
        payload: Dict[str, Any] = {
            "resource_name": f"customers/{customer}/assetGroupListingGroupFilters/{group_id}~{temp}",
            "asset_group": group_rn,
            "type": node["type"],
            "listing_source": node.get("listing_source") or "SHOPPING",
        }
        if node.get("case_value"):
            payload["case_value"] = node["case_value"]
        if node.get("parent_id"):
            payload["parent_listing_group_filter"] = (
                f"customers/{customer}/assetGroupListingGroupFilters/"
                f"{group_id}~{temp_ids[node['parent_id']]}"
            )
        if len(order) == 1:
            payload.pop("resource_name")
        batch.add(
            "asset_group_listing_group_filter_operation",
            {"create": payload},
            f"listing_group:{node['type']}",
        )


def _build_asset_group(
    batch: _Batch,
    campaign: Dict[str, Any],
    spec: Dict[str, Any],
    notes: List[str],
    warnings: List[str],
) -> Dict[str, Any]:
    if not spec.get("name"):
        raise ToolError("asset_group.name is required")
    if not spec.get("final_urls"):
        raise ToolError("asset_group.final_urls is required")
    customer = batch.customer_id
    temp = batch.temp_id()
    group_rn = f"customers/{customer}/assetGroups/{temp}"
    group: Dict[str, Any] = {
        "resource_name": group_rn,
        "campaign": f"customers/{customer}/campaigns/{campaign['id']}",
        "name": spec["name"],
        "status": (spec.get("status") or "PAUSED").upper(),
        "final_urls": spec["final_urls"],
    }
    for field in (
        "final_mobile_urls",
        "path1",
        "path2",
        "final_url_suffix",
        "tracking_url_template",
    ):
        if spec.get(field):
            group[field] = spec[field]
    batch.add(
        "asset_group_operation",
        {"create": group},
        f"asset_group:{spec['name']}",
    )

    assets = _normalize_group_assets(batch, spec)
    skip: set = set()
    if campaign["brand_guidelines_enabled"]:
        skip = BRAND_FIELD_TYPES
        dropped = [ft for ft in BRAND_FIELD_TYPES if assets.pop(ft, None)]
        if dropped:
            notes.append(
                f"Campaign uses brand guidelines: {', '.join(sorted(dropped))} belong to the "
                "campaign, not the asset group, and were not linked here. Use "
                "link_assets(level='campaign') to change them."
            )
    _check_group_limits(assets, warnings, skip)

    for field_type, refs in assets.items():
        for ref in refs:
            asset_rn = batch.asset(ref, field_type)
            batch.add(
                "asset_group_asset_operation",
                {
                    "create": {
                        "asset_group": group_rn,
                        "asset": asset_rn,
                        "field_type": field_type,
                    }
                },
                f"link:{field_type}",
            )

    signals = spec.get("signals") or {}
    for theme in signals.get("search_themes") or []:
        batch.add(
            "asset_group_signal_operation",
            {
                "create": {
                    "asset_group": group_rn,
                    "search_theme": {"text": theme},
                }
            },
            f"signal:search_theme:{theme}",
        )
    for audience in signals.get("audiences") or []:
        audience_rn = (
            audience
            if "/" in str(audience)
            else f"customers/{customer}/audiences/{_cid(audience)}"
        )
        if not audience_rn.startswith(f"customers/{customer}/"):
            notes.append(
                f"Audience {audience_rn} belongs to another customer; skipped."
            )
            continue
        batch.add(
            "asset_group_signal_operation",
            {
                "create": {
                    "asset_group": group_rn,
                    "audience": {"audience": audience_rn},
                }
            },
            "signal:audience",
        )

    tree = spec.get("listing_group_filters")
    if campaign.get("merchant_id"):
        if tree in (None, [], "ALL"):
            tree = [
                {
                    "id": "root",
                    "type": "UNIT_INCLUDED",
                    "listing_source": "SHOPPING",
                }
            ]
        if tree != "NONE":
            _add_listing_tree(batch, group_rn, tree)
    return {"resource_name": group_rn, "assets": assets}


def _plan_view(assets: Dict[str, List[Dict[str, Any]]]) -> Dict[str, List[str]]:
    plan: Dict[str, List[str]] = {}
    for field_type, refs in assets.items():
        plan[field_type] = [
            r.get("text")
            or r.get("image_url")
            or r.get("youtube_video")
            or r.get("call_to_action")
            or r.get("asset")
            or r.get("asset_id")
            or "?"
            for r in refs
        ]
    return plan


def create_asset_group(
    customer_id: str,
    campaign_id: str,
    asset_group: Dict[str, Any],
    validate_only: bool = True,
    login_customer_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a Performance Max asset group from scratch, with all its creatives.

    Everything is one atomic request: the group, new assets (texts, images
    downloaded from URLs, YouTube videos, CTA), their links, search themes,
    audience signals and, for retail campaigns, the product filter ("all
    products" unless given).

    ⚠️ Mutating. validate_only defaults to true: Google validates the whole
    request and nothing is written. Show the result to the user and repeat with
    validate_only=false only after explicit confirmation.

    asset_group spec:
        {
          "name": "Spring sale", "status": "PAUSED" (default),
          "final_urls": ["https://..."], "path1": "...", "path2": "...",
          "assets": {
            "HEADLINE": ["...", ...],                 # 3-15, <=30 chars
            "LONG_HEADLINE": ["..."],                 # 1-5, <=90 chars
            "DESCRIPTION": ["...", "..."],            # 2-5, <=90, one <=60
            "BUSINESS_NAME": ["Joom"],                # <=25
            "MARKETING_IMAGE": [{"image_url": ...}],  # 1.91:1
            "SQUARE_MARKETING_IMAGE": [...],          # 1:1
            "PORTRAIT_MARKETING_IMAGE": [...],        # 4:5
            "LOGO": [...], "LANDSCAPE_LOGO": [...],
            "YOUTUBE_VIDEO": ["https://youtu.be/..."],
            "CALL_TO_ACTION_SELECTION": ["SHOP_NOW"]
          },
          "images": ["https://...", ...],  # routed by aspect ratio
          "signals": {"search_themes": ["..."], "audiences": ["<audience id>"]},
          "listing_group_filters": "ALL" | "NONE" | [tree from get_campaign_creatives]
        }
    Any asset may also be an existing asset ({"asset": rn} / {"asset_id": id}).
    With brand guidelines enabled on the campaign, BUSINESS_NAME/LOGO/
    LANDSCAPE_LOGO are campaign assets and are skipped here (see notes).

    Args:
        customer_id: Google Ads customer ID (digits only).
        campaign_id: Performance Max campaign ID.
        asset_group: The spec above.
        validate_only: Validate without writing (default true).
        login_customer_id: Manager (MCC) account ID to use as login-customer-id.
    """
    customer_id = _cid(customer_id)
    campaign = _campaign_info(customer_id, campaign_id, login_customer_id)
    if campaign["type"] != "PERFORMANCE_MAX":
        raise ToolError(
            f"Campaign {campaign_id} is {campaign['type']}; asset groups exist only in "
            "Performance Max. For other campaign types use create_ad."
        )
    batch = _Batch(customer_id, login_customer_id)
    notes: List[str] = []
    warnings: List[str] = []
    built = _build_asset_group(batch, campaign, asset_group, notes, warnings)
    names = batch.execute(validate_only)

    extra: Dict[str, Any] = {
        "campaign": campaign,
        "plan": _plan_view(built["assets"]),
        "notes": notes,
        "warnings": warnings,
    }
    if not validate_only:
        extra["asset_group"] = _name_for(batch, names, "asset_group:")
    return _result(
        batch,
        validate_only,
        names,
        "create_asset_group",
        {
            "campaign_id": campaign["id"],
            "asset_group_name": asset_group.get("name"),
        },
        extra,
    )


def _apply_group_overrides(
    source: Dict[str, Any], overrides: Dict[str, Any], same_customer: bool
) -> Dict[str, Any]:
    spec = {
        key: source[key]
        for key in (
            "name",
            "final_urls",
            "final_mobile_urls",
            "path1",
            "path2",
            "signals",
            "listing_group_filters",
        )
        if key in source
    }
    assets = {
        field_type: [
            {k: v for k, v in ref.items() if k not in ("link", "source")}
            for ref in refs
            if ref.get("source") in (None, "ADVERTISER")
        ]
        for field_type, refs in source.get("assets", {}).items()
    }
    if not same_customer:
        for refs in assets.values():
            for ref in refs:
                ref.pop("asset", None)
                ref.pop("asset_id", None)

    overrides = dict(overrides or {})
    if "images" in overrides:
        # New images replace every image slot the shortcut can fill.
        for _, slot in PMAX_IMAGE_ROUTES:
            assets.pop(slot, None)
        spec["images"] = overrides.pop("images")
    for field_type, refs in (overrides.pop("assets", None) or {}).items():
        assets[field_type.upper()] = refs if isinstance(refs, list) else [refs]
    spec.update(overrides)
    spec["assets"] = {ft: refs for ft, refs in assets.items() if refs}
    return spec


def copy_asset_group(
    customer_id: str,
    source_asset_group_id: str,
    new_name: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    target_campaign_id: Optional[str] = None,
    target_customer_id: Optional[str] = None,
    copy_signals: bool = True,
    copy_listing_groups: bool = True,
    validate_only: bool = True,
    login_customer_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Copy a Performance Max asset group, replacing any of its creatives.

    Reads the source group (assets, final URLs, search themes, audiences,
    product tree), applies ``overrides`` and creates the copy PAUSED in the same
    or another PMax campaign — or another account (images are re-uploaded from
    their URLs, texts and videos recreated).

    overrides (all optional; anything not given is copied as is):
        {
          "assets": {"HEADLINE": [...], "DESCRIPTION": [...], "YOUTUBE_VIDEO": [...]},
              # each listed field type REPLACES that field type entirely
          "images": ["https://...", ...],
              # replaces ALL marketing images (landscape/square/portrait/tall),
              # routed by aspect ratio; logos stay unless given under assets
          "final_urls": [...], "path1": ..., "path2": ..., "status": "ENABLED",
          "signals": {"search_themes": [...], "audiences": [...]},
          "listing_group_filters": "ALL" | "NONE" | [...]
        }
    To replace "all creatives", pass HEADLINE, LONG_HEADLINE, DESCRIPTION,
    images and YOUTUBE_VIDEO (and LOGO/BUSINESS_NAME if they change too).

    ⚠️ Mutating. validate_only defaults to true; repeat with
    validate_only=false only after the user confirms.

    Args:
        customer_id: Customer that owns the source asset group.
        source_asset_group_id: Asset group to copy.
        new_name: Name of the copy (default "<source name> copy <date>").
        overrides: Replacements, see above.
        target_campaign_id: PMax campaign for the copy (default: source campaign).
        target_customer_id: Account for the copy (default: customer_id).
        copy_signals: Copy search themes and audiences (default true).
        copy_listing_groups: Copy the product tree (default true).
        validate_only: Validate without writing (default true).
        login_customer_id: Manager (MCC) account ID to use as login-customer-id.
    """
    customer_id = _cid(customer_id)
    rows = _query(
        customer_id,
        f"SELECT asset_group.campaign FROM asset_group WHERE asset_group.id = {_cid(source_asset_group_id)}",
        login_customer_id,
    )
    if not rows:
        raise ToolError(
            f"Asset group {source_asset_group_id} not found in {customer_id}"
        )
    source_campaign_id = rows[0].asset_group.campaign.rsplit("/", 1)[-1]
    groups = _read_asset_groups(
        customer_id,
        source_campaign_id,
        [source_asset_group_id],
        login_customer_id,
    )
    source = groups[0]

    target_customer = _cid(target_customer_id or customer_id)
    if target_customer != customer_id and not target_campaign_id:
        raise ToolError(
            "target_campaign_id is required when copying to another account"
        )
    target_campaign_id = _cid(target_campaign_id or source_campaign_id)
    campaign = _campaign_info(
        target_customer, target_campaign_id, login_customer_id
    )
    if campaign["type"] != "PERFORMANCE_MAX":
        raise ToolError(
            f"Target campaign {target_campaign_id} is {campaign['type']}, not PERFORMANCE_MAX"
        )

    spec = _apply_group_overrides(
        source, overrides or {}, same_customer=target_customer == customer_id
    )
    spec["name"] = (
        new_name
        or (overrides or {}).get("name")
        or f"{source['name']} copy {date.today().isoformat()}"
    )
    if not copy_signals and "signals" not in (overrides or {}):
        spec.pop("signals", None)
    if target_customer != customer_id and "signals" in spec:
        spec["signals"] = {**spec["signals"], "audiences": []}
    if not copy_listing_groups and "listing_group_filters" not in (
        overrides or {}
    ):
        spec["listing_group_filters"] = "ALL"
    if not campaign.get("merchant_id"):
        spec.pop("listing_group_filters", None)

    batch = _Batch(target_customer, login_customer_id)
    notes: List[str] = []
    warnings: List[str] = []
    built = _build_asset_group(batch, campaign, spec, notes, warnings)
    names = batch.execute(validate_only)

    extra: Dict[str, Any] = {
        "source": {
            "asset_group_id": source["asset_group_id"],
            "name": source["name"],
            "campaign_id": source_campaign_id,
        },
        "target_campaign": campaign,
        "plan": _plan_view(built["assets"]),
        "notes": notes,
        "warnings": warnings,
    }
    if not validate_only:
        extra["asset_group"] = _name_for(batch, names, "asset_group:")
    return _result(
        batch,
        validate_only,
        names,
        "copy_asset_group",
        {
            "source_asset_group_id": source["asset_group_id"],
            "campaign_id": campaign["id"],
            "asset_group_name": spec["name"],
        },
        extra,
    )


def update_asset_group(
    customer_id: str,
    asset_group_id: str,
    set_assets: Optional[Dict[str, Any]] = None,
    images: Optional[List[Any]] = None,
    add_assets: Optional[Dict[str, Any]] = None,
    remove_asset_ids: Optional[List[str]] = None,
    name: Optional[str] = None,
    final_urls: Optional[List[str]] = None,
    path1: Optional[str] = None,
    path2: Optional[str] = None,
    status: Optional[Literal["ENABLED", "PAUSED"]] = None,
    add_search_themes: Optional[List[str]] = None,
    validate_only: bool = True,
    login_customer_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Replace, add or remove creatives in an existing Performance Max asset group.

    One atomic request: new links are created before old ones are removed, so
    the group never drops below its minimums mid-way. Assets that are already
    linked are kept rather than relinked.

    ⚠️ Mutating. validate_only defaults to true; repeat with
    validate_only=false only after the user confirms.

    Args:
        customer_id: Google Ads customer ID (digits only).
        asset_group_id: Asset group to change.
        set_assets: {FIELD_TYPE: [refs]} — each listed field type is REPLACED
            by exactly these assets, e.g. {"HEADLINE": [...], "YOUTUBE_VIDEO": [...]}.
        images: Replace ALL marketing images (landscape/square/portrait/tall)
            with these, routed by aspect ratio. Logos are untouched.
        add_assets: {FIELD_TYPE: [refs]} — linked in addition to current ones.
        remove_asset_ids: Asset IDs to unlink from the group (any field type).
        name, final_urls, path1, path2, status: Asset group fields to change.
        add_search_themes: Search themes to add as signals.
        validate_only: Validate without writing (default true).
        login_customer_id: Manager (MCC) account ID to use as login-customer-id.
    """
    customer_id = _cid(customer_id)
    rows = _query(
        customer_id,
        f"SELECT asset_group.campaign FROM asset_group WHERE asset_group.id = {_cid(asset_group_id)}",
        login_customer_id,
    )
    if not rows:
        raise ToolError(
            f"Asset group {asset_group_id} not found in {customer_id}"
        )
    campaign_id = rows[0].asset_group.campaign.rsplit("/", 1)[-1]
    campaign = _campaign_info(customer_id, campaign_id, login_customer_id)
    current = _read_asset_groups(
        customer_id, campaign_id, [asset_group_id], login_customer_id
    )[0]
    group_rn = f"customers/{customer_id}/assetGroups/{_cid(asset_group_id)}"

    batch = _Batch(customer_id, login_customer_id)
    warnings: List[str] = []

    group_update: Dict[str, Any] = {"resource_name": group_rn}
    for field, value in (
        ("name", name),
        ("final_urls", final_urls),
        ("path1", path1),
        ("path2", path2),
        ("status", status),
    ):
        if value is not None:
            group_update[field] = value
    if len(group_update) > 1:
        mask = ",".join(_camel(k) for k in group_update if k != "resource_name")
        batch.add(
            "asset_group_operation",
            {"update": group_update, "update_mask": mask},
            "asset_group:update",
        )

    desired: Dict[str, List[Dict[str, Any]]] = {}
    if images is not None:
        routed = _normalize_group_assets(batch, {"images": images})
        for _, slot in PMAX_IMAGE_ROUTES:
            desired[slot] = routed.get(slot, [])
    for field_type, refs in (set_assets or {}).items():
        field_type = field_type.upper()
        refs = refs if isinstance(refs, list) else [refs]
        desired[field_type] = [normalize_ref(r, field_type) for r in refs]
    if campaign["brand_guidelines_enabled"] and BRAND_FIELD_TYPES & (
        desired.keys() | (add_assets or {}).keys()
    ):
        raise ToolError(
            "Campaign uses brand guidelines: BUSINESS_NAME/LOGO/LANDSCAPE_LOGO are "
            "campaign assets. Use link_assets(level='campaign') instead."
        )

    current_assets: Dict[str, List[Dict[str, Any]]] = current["assets"]
    to_remove: List[str] = []
    final_counts = {ft: len(v) for ft, v in current_assets.items()}

    def link(field_type: str, ref: Dict[str, Any]) -> None:
        asset_rn = batch.asset(ref, field_type)
        batch.add(
            "asset_group_asset_operation",
            {
                "create": {
                    "asset_group": group_rn,
                    "asset": asset_rn,
                    "field_type": field_type,
                }
            },
            f"link:{field_type}",
        )

    for field_type, refs in desired.items():
        existing = current_assets.get(field_type, [])
        keep: set = set()
        for ref in refs:
            match = _match_existing(ref, existing)
            if match is not None:
                keep.add(match["link"])
            else:
                link(field_type, ref)
        for view in existing:
            if view["link"] not in keep:
                if view.get("source") and view["source"] != "ADVERTISER":
                    warnings.append(
                        f"{field_type}: kept auto-created asset {view['asset_id']}"
                    )
                    continue
                to_remove.append(view["link"])
        final_counts[field_type] = len(refs)

    for field_type, refs in (add_assets or {}).items():
        field_type = field_type.upper()
        for ref in refs if isinstance(refs, list) else [refs]:
            ref = normalize_ref(ref, field_type)
            if _match_existing(ref, current_assets.get(field_type, [])) is None:
                link(field_type, ref)
                final_counts[field_type] = final_counts.get(field_type, 0) + 1

    for asset_id in remove_asset_ids or []:
        found = False
        for field_type, views in current_assets.items():
            for view in views:
                if (
                    view["asset_id"] == _cid(asset_id)
                    and view["link"] not in to_remove
                ):
                    to_remove.append(view["link"])
                    final_counts[field_type] = (
                        final_counts.get(field_type, 1) - 1
                    )
                    found = True
        if not found:
            warnings.append(
                f"Asset {asset_id} is not linked to this asset group"
            )

    for link_rn in to_remove:
        batch.add(
            "asset_group_asset_operation",
            {"remove": link_rn},
            f"unlink:{link_rn.rsplit('~', 1)[-1]}",
        )

    for theme in add_search_themes or []:
        if theme not in current["signals"]["search_themes"]:
            batch.add(
                "asset_group_signal_operation",
                {
                    "create": {
                        "asset_group": group_rn,
                        "search_theme": {"text": theme},
                    }
                },
                f"signal:search_theme:{theme}",
            )

    skip = BRAND_FIELD_TYPES if campaign["brand_guidelines_enabled"] else set()
    for field_type, (minimum, maximum, _) in PMAX_LIMITS.items():
        count = final_counts.get(field_type, 0)
        if field_type in skip:
            continue
        if count > maximum:
            raise ToolError(
                f"{field_type}: would have {count} assets, max {maximum}"
            )
        if count < minimum and (
            field_type in desired or field_type in final_counts
        ):
            warnings.append(
                f"{field_type}: would have {count} assets, Google usually needs {minimum}"
            )
    for field_type, refs in desired.items():
        limit = PMAX_LIMITS.get(field_type, (0, 0, None))[2]
        for ref in refs:
            if "text" in ref and limit:
                _check_text(field_type, ref["text"], limit)

    names = batch.execute(validate_only)
    return _result(
        batch,
        validate_only,
        names,
        "update_asset_group",
        {"asset_group_id": _cid(asset_group_id), "campaign_id": campaign_id},
        {
            "asset_group": {
                "id": current["asset_group_id"],
                "name": current["name"],
            },
            "unlinked": len(to_remove),
            "resulting_counts": {k: v for k, v in final_counts.items() if v},
            "warnings": warnings,
        },
    )


def _match_existing(
    ref: Dict[str, Any], existing: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """The currently linked asset a ref points to, if any."""
    for view in existing:
        if ref.get("asset") and ref["asset"] == view["asset"]:
            return view
        if ref.get("asset_id") and _cid(ref["asset_id"]) == view["asset_id"]:
            return view
        if (
            "text" in ref
            and ref.get("text") == view.get("text")
            and not ref.get("asset")
        ):
            return view
        if (
            ref.get("youtube_video")
            and not ref.get("asset")
            and view.get("youtube_video")
        ):
            if _youtube_id(ref["youtube_video"]) == view["youtube_video"]:
                return view
        if ref.get("call_to_action") and str(
            ref["call_to_action"]
        ).upper() == view.get("call_to_action"):
            return view
    return None


def _camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


# --- Ads --------------------------------------------------------------------


def _ad_info_descriptor(client: Any, ad_type: str) -> Any:
    if ad_type not in SUPPORTED_AD_TYPES:
        raise ToolError(
            f"Unsupported ad type {ad_type!r}; supported: {SUPPORTED_AD_TYPES}"
        )
    ad = client.get_type("Ad")
    return type(ad).pb(ad).DESCRIPTOR.fields_by_name[ad_type].message_type


def _build_ad_fields(
    batch: _Batch,
    client: Any,
    ad_type: str,
    fields: Dict[str, Any],
    images: Optional[List[Any]],
) -> Dict[str, Any]:
    info = _ad_info_descriptor(client, ad_type)
    fields = dict(fields or {})
    if images:
        routes = AD_IMAGE_ROUTES.get(ad_type)
        if not routes:
            raise ToolError(
                f"The images shortcut is not available for {ad_type}; set image fields directly"
            )
        for _, slot in routes:
            fields.pop(slot, None)
        for ref in images:
            ref = normalize_ref(ref, "image")
            width, height = batch.image_size(ref)
            slot = _route_by_aspect(width, height, routes)
            if not slot:
                raise ToolError(
                    f"Image {ref.get('image_url') or ref.get('asset')} is {width}x{height}; {ad_type} takes {[r for r, _ in routes]}"
                )
            fields.setdefault(slot, []).append(ref)

    built: Dict[str, Any] = {}
    for key, value in fields.items():
        field = info.fields_by_name.get(key)
        if field is None:
            raise ToolError(
                f"{ad_type} has no field {key!r}; fields: {[f.name for f in info.fields]}"
            )
        message = field.message_type.name if field.message_type else None
        if (
            message is None
            or not message.startswith("Ad")
            or not message.endswith("Asset")
        ):
            built[key] = value
            continue
        values = value if field.is_repeated else [value]
        if not isinstance(values, list):
            values = [values]
        converted = []
        for item in values:
            if message == "AdTextAsset":
                item = {"text": item} if isinstance(item, str) else item
                _check_text(
                    f"{ad_type}.{key}",
                    item.get("text", ""),
                    AD_TEXT_LIMITS.get((ad_type, key), 90),
                )
                converted.append(
                    {
                        k: v
                        for k, v in item.items()
                        if k in ("text", "pinned_field")
                    }
                )
            else:
                slot = {
                    "AdImageAsset": "IMAGE",
                    "AdVideoAsset": "YOUTUBE_VIDEO",
                    "AdCallToActionAsset": "CALL_TO_ACTION",
                }.get(message, key)
                entry = {"asset": batch.asset(item, slot)}
                if isinstance(item, dict) and item.get("ad_video_asset_info"):
                    entry["ad_video_asset_info"] = item["ad_video_asset_info"]
                converted.append(entry)
        built[key] = converted if field.is_repeated else converted[0]
    return built


def _build_ad(
    batch: _Batch,
    client: Any,
    ad_group_id: str,
    spec: Dict[str, Any],
    status: str,
) -> None:
    ad_type = spec.get("type")
    if not ad_type:
        raise ToolError(f"ad.type is required, one of {SUPPORTED_AD_TYPES}")
    ad: Dict[str, Any] = {
        ad_type: _build_ad_fields(
            batch, client, ad_type, spec.get("fields") or {}, spec.get("images")
        )
    }
    for field in (
        "final_urls",
        "final_mobile_urls",
        "name",
        "tracking_url_template",
        "final_url_suffix",
        "display_url",
    ):
        if spec.get(field):
            ad[field] = spec[field]
    if (
        ad_type != "app_ad"
        and ad_type != "app_engagement_ad"
        and not ad.get("final_urls")
    ):
        raise ToolError("ad.final_urls is required")
    batch.add(
        "ad_group_ad_operation",
        {
            "create": {
                "ad_group": f"customers/{batch.customer_id}/adGroups/{_cid(ad_group_id)}",
                "status": status,
                "ad": ad,
            }
        },
        f"ad:{ad_type}",
    )


def create_ad(
    customer_id: str,
    ad_group_id: str,
    ad: Dict[str, Any],
    status: Literal["ENABLED", "PAUSED"] = "PAUSED",
    validate_only: bool = True,
    login_customer_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create an ad of any responsive type, uploading its images/videos as needed.

    Covers Search (responsive_search_ad), Display (responsive_display_ad),
    Demand Gen (demand_gen_multi_asset_ad, demand_gen_video_responsive_ad,
    demand_gen_carousel_ad, demand_gen_product_ad), Video
    (video_responsive_ad) and App (app_ad, app_engagement_ad) campaigns.

    ad spec:
        {
          "type": "responsive_display_ad",
          "final_urls": ["https://..."], "name": "optional",
          "fields": {                       # fields of the ad type's Info message
            "headlines": ["...", "..."],     # text slots: strings or {"text", "pinned_field"}
            "long_headline": "...",
            "descriptions": ["..."],
            "business_name": "Joom",
            "marketing_images": [{"image_url": "https://..."}],  # image slots: refs
            "logo_images": ["https://..."],
            "youtube_videos": ["https://youtu.be/..."],          # video slots
            "call_to_actions": ["SHOP_NOW"]                       # CTA slots
          },
          "images": ["https://...", ...]    # routed by aspect ratio (display, demand gen multi-asset)
        }
    Field names follow the Google Ads API (see get_campaign_creatives output for
    real examples). Text fields of type AdTextAsset accept plain strings.

    ⚠️ Mutating. validate_only defaults to true; the ad is created PAUSED
    unless status says otherwise.

    Args:
        customer_id: Google Ads customer ID (digits only).
        ad_group_id: Ad group for the new ad.
        ad: The spec above.
        status: ENABLED or PAUSED (default PAUSED).
        validate_only: Validate without writing (default true).
        login_customer_id: Manager (MCC) account ID to use as login-customer-id.
    """
    customer_id = _cid(customer_id)
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    batch = _Batch(customer_id, login_customer_id)
    _build_ad(batch, client, ad_group_id, ad, status)
    names = batch.execute(validate_only)
    extra: Dict[str, Any] = {}
    if not validate_only:
        extra["ad"] = _name_for(batch, names, "ad:")
    return _result(
        batch,
        validate_only,
        names,
        "create_ad",
        {"ad_group_id": _cid(ad_group_id), "ad_type": ad.get("type")},
        extra,
    )


def copy_ad(
    customer_id: str,
    source_ad_id: str,
    overrides: Optional[Dict[str, Any]] = None,
    target_ad_group_id: Optional[str] = None,
    target_customer_id: Optional[str] = None,
    status: Literal["ENABLED", "PAUSED"] = "PAUSED",
    pause_source: bool = False,
    validate_only: bool = True,
    login_customer_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Copy an ad with replaced creatives (texts, images, videos, URLs).

    Most responsive ad types cannot be edited in place, so "replace the
    creatives of an ad" means: create a copy with the new creatives and pause
    the original (pause_source=true) — in one atomic request.

    overrides (anything not given is copied):
        {
          "fields": {"headlines": [...], "youtube_videos": [...], ...},
              # each listed field REPLACES that field of the source ad
          "images": [...],   # replaces all routable image fields, by aspect ratio
          "final_urls": [...], "name": "..."
        }

    ⚠️ Mutating. validate_only defaults to true; the copy is created PAUSED
    unless status says otherwise.

    Args:
        customer_id: Customer that owns the source ad.
        source_ad_id: Ad ID to copy.
        overrides: Replacements, see above.
        target_ad_group_id: Ad group for the copy (default: the source's).
        target_customer_id: Account for the copy (default: customer_id);
            requires target_ad_group_id.
        status: Status of the copy (default PAUSED).
        pause_source: Pause the source ad in the same request (default false).
        validate_only: Validate without writing (default true).
        login_customer_id: Manager (MCC) account ID to use as login-customer-id.
    """
    customer_id = _cid(customer_id)
    ads = _read_ads(
        customer_id,
        f"ad_group_ad.ad.id = {_cid(source_ad_id)}",
        login_customer_id,
    )
    if not ads:
        raise ToolError(f"Ad {source_ad_id} not found in {customer_id}")
    source = ads[0]
    if not source.get("type"):
        raise ToolError(
            f"Ad {source_ad_id} is {source['ad_type']}, which copy_ad does not support"
        )

    target_customer = _cid(target_customer_id or customer_id)
    if target_customer != customer_id and not target_ad_group_id:
        raise ToolError(
            "target_ad_group_id is required when copying to another account"
        )
    same_customer = target_customer == customer_id

    overrides = dict(overrides or {})
    fields = dict(source["fields"])
    if pause_source and not same_customer:
        raise ToolError("pause_source works only within one account")
    if not same_customer:
        for value in fields.values():
            for item in value if isinstance(value, list) else [value]:
                if isinstance(item, dict):
                    item.pop("asset", None)
                    item.pop("asset_id", None)
    fields.update(overrides.pop("fields", None) or {})
    spec = {
        "type": source["type"],
        "final_urls": source["final_urls"],
        "fields": fields,
    }
    for key in ("name", "final_mobile_urls"):
        if source.get(key):
            spec[key] = source[key]
    spec.update(overrides)

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    batch = _Batch(target_customer, login_customer_id)
    _build_ad(
        batch, client, target_ad_group_id or source["ad_group_id"], spec, status
    )
    if pause_source:
        source_rn = f"customers/{customer_id}/adGroupAds/{source['ad_group_id']}~{source['ad_id']}"
        batch.add(
            "ad_group_ad_operation",
            {
                "update": {"resource_name": source_rn, "status": "PAUSED"},
                "update_mask": "status",
            },
            "ad:pause_source",
        )
    names = batch.execute(validate_only)
    extra: Dict[str, Any] = {
        "source": {
            "ad_id": source["ad_id"],
            "ad_group_id": source["ad_group_id"],
            "type": source["type"],
        }
    }
    if not validate_only:
        extra["ad"] = next(
            (
                n
                for n, label in zip(names, batch.labels)
                if label.startswith("ad:") and label != "ad:pause_source"
            ),
            None,
        )
    return _result(
        batch,
        validate_only,
        names,
        "copy_ad",
        {
            "source_ad_id": source["ad_id"],
            "ad_group_id": target_ad_group_id or source["ad_group_id"],
        },
        extra,
    )


# --- Account / campaign / ad-group assets -----------------------------------


_LINK_LEVELS = {
    "customer": ("customer_asset", None),
    "campaign": ("campaign_asset", "campaign"),
    "ad_group": ("ad_group_asset", "ad_group"),
}


def link_assets(
    customer_id: str,
    level: Literal["customer", "campaign", "ad_group"],
    field_type: str,
    assets: List[Any],
    target_id: Optional[str] = None,
    replace_existing: bool = False,
    validate_only: bool = True,
    login_customer_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Link sitelinks, callouts, snippets, calls, images, logos, etc. to a target.

    Works for every campaign type, Performance Max included (its sitelinks,
    callouts, and — with brand guidelines — BUSINESS_NAME/LOGO/LANDSCAPE_LOGO
    are campaign assets).

    Examples:
        field_type="SITELINK", assets=[{"sitelink": {"link_text": "Sale",
            "description1": "Up to -70%", "description2": "Only this week",
            "final_urls": ["https://..."]}}]
        field_type="CALLOUT", assets=["Free delivery", "Easy returns"]
        field_type="STRUCTURED_SNIPPET", assets=[{"structured_snippet":
            {"header": "Brands", "values": ["A", "B", "C"]}}]
        field_type="BUSINESS_NAME", assets=["Joom"]
        field_type="LOGO", assets=["https://.../logo.png"]
        field_type="PROMOTION", assets=[{"promotion_asset": {...}, "final_urls": [...]}]

    ⚠️ Mutating. validate_only defaults to true.

    Args:
        customer_id: Google Ads customer ID (digits only).
        level: customer, campaign or ad_group.
        field_type: AssetFieldType, e.g. SITELINK, CALLOUT, STRUCTURED_SNIPPET,
            CALL, PROMOTION, PRICE, BUSINESS_NAME, LOGO, LANDSCAPE_LOGO,
            MARKETING_IMAGE, SQUARE_MARKETING_IMAGE, AD_IMAGE.
        assets: Asset refs (see module docs) or existing asset IDs.
        target_id: Campaign or ad group ID (not for level=customer).
        replace_existing: Unlink the target's current assets of this field type
            (default false: add to them).
        validate_only: Validate without writing (default true).
        login_customer_id: Manager (MCC) account ID to use as login-customer-id.
    """
    customer_id = _cid(customer_id)
    if level not in _LINK_LEVELS:
        raise ToolError(f"level must be one of {list(_LINK_LEVELS)}")
    resource, parent = _LINK_LEVELS[level]
    if parent and not target_id:
        raise ToolError(f"target_id is required for level={level}")
    if not assets:
        raise ToolError("assets must be a non-empty list")
    field_type = field_type.upper()

    if parent:
        prefix = "campaigns" if parent == "campaign" else "adGroups"
        where = (
            f"{resource}.{parent} = 'customers/{customer_id}/{prefix}/"
            f"{_cid(target_id)}' AND {resource}.field_type = '{field_type}'"
        )
    else:
        where = f"{resource}.field_type = '{field_type}'"
    existing_rows = _link_rows(customer_id, resource, where, login_customer_id)
    existing = _grouped_assets(existing_rows, resource).get(field_type, [])

    batch = _Batch(customer_id, login_customer_id)
    keep: set = set()
    for ref in assets:
        if isinstance(ref, (int,)) or (isinstance(ref, str) and ref.isdigit()):
            ref = {"asset_id": str(ref)}
        ref = normalize_ref(ref, field_type)
        match = _match_existing(ref, existing)
        if match is not None:
            keep.add(match["link"])
            continue
        payload: Dict[str, Any] = {
            "asset": batch.asset(ref, field_type),
            "field_type": field_type,
        }
        if parent == "campaign":
            payload["campaign"] = (
                f"customers/{customer_id}/campaigns/{_cid(target_id)}"
            )
        elif parent == "ad_group":
            payload["ad_group"] = (
                f"customers/{customer_id}/adGroups/{_cid(target_id)}"
            )
        batch.add(
            f"{resource}_operation", {"create": payload}, f"link:{field_type}"
        )

    removed = 0
    if replace_existing:
        for view in existing:
            if view["link"] not in keep:
                batch.add(
                    f"{resource}_operation",
                    {"remove": view["link"]},
                    f"unlink:{field_type}",
                )
                removed += 1

    names = batch.execute(validate_only)
    return _result(
        batch,
        validate_only,
        names,
        "link_assets",
        {"level": level, "target_id": target_id, "field_type": field_type},
        {
            "already_linked": len(keep),
            "unlinked": removed,
            "previously_linked": len(existing),
        },
    )


def upload_assets(
    customer_id: str,
    assets: List[Any],
    validate_only: bool = True,
    login_customer_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create standalone assets (images from URLs, YouTube videos, texts, ...).

    Useful to upload creatives once and reuse their IDs across asset groups,
    ads and campaigns. Unlinked assets do not serve. Refs are objects (see
    module docs): {"image_url": ...}, {"youtube_video": ...}, {"text": ...},
    {"sitelink": {...}}, {"callout": ...}, {"call_to_action": ...}, ...

    Args:
        customer_id: Google Ads customer ID (digits only).
        assets: Asset refs to create.
        validate_only: Validate without writing (default true).
        login_customer_id: Manager (MCC) account ID to use as login-customer-id.
    """
    customer_id = _cid(customer_id)
    if not assets:
        raise ToolError("assets must be a non-empty list")
    batch = _Batch(customer_id, login_customer_id)
    for ref in assets:
        if not isinstance(ref, dict):
            raise ToolError(f"upload_assets needs object refs, got {ref!r}")
        batch.asset(ref)
    names = batch.execute(validate_only)
    result = _result(batch, validate_only, names, "upload_assets", {}, {})
    if not validate_only:
        result["assets"] = [
            {
                "label": label.removeprefix("asset:"),
                "resource_name": name,
                "asset_id": name.rsplit("/", 1)[-1],
            }
            for label, name in zip(batch.labels, names)
        ]
    return result


# --- YouTube video upload ---------------------------------------------------

# YouTubeVideoUpload.video_privacy has no PRIVATE value: an Ads asset has to be
# watchable, so the API offers PUBLIC and UNLISTED only.
YOUTUBE_PRIVACIES = ("UNLISTED", "PUBLIC")
_YOUTUBE_CHANNEL = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_YOUTUBE_FORBIDDEN = re.compile(r"[<>]")
_DRIVE_URL = re.compile(
    r"^https?://(?:drive|docs)\.google\.com/"
    r"(?:file/d/([A-Za-z0-9_-]+)|(?:open|uc)\?(?:.*&)?id=([A-Za-z0-9_-]+))"
)
_UPLOAD_RN = re.compile(r"^customers/(\d+)/youTubeVideoUploads/(\d+)$")


def _download_url(url: str) -> str:
    """Direct download URL; Google Drive share links are rewritten."""
    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        raise ToolError(f"video_url must be an http(s) URL, got {url!r}")
    match = _DRIVE_URL.match(url)
    if match:
        file_id = match.group(1) or match.group(2)
        return (
            "https://drive.usercontent.google.com/download"
            f"?id={file_id}&export=download&confirm=t"
        )
    return url


def _download_video(url: str, file: Any) -> Dict[str, Any]:
    """Streams url into the open binary file; returns what the server said."""
    source = _download_url(url)
    try:
        response = requests.get(
            source, stream=True, timeout=VIDEO_FETCH_TIMEOUT_S
        )
    except requests.RequestException as ex:
        raise ToolError(f"Could not download video {url}: {ex}")
    with response:
        if not response.ok:
            raise ToolError(
                f"Could not download video {url}: HTTP {response.status_code}"
            )
        content_type = response.headers.get("Content-Type", "")
        if content_type.startswith("text/html"):
            raise ToolError(
                f"{url} returned a web page, not a video file. Use a direct "
                "file link; a Google Drive file must be shared as 'Anyone "
                "with the link'."
            )
        declared = int(response.headers.get("Content-Length") or 0)
        if declared > MAX_VIDEO_BYTES:
            raise ToolError(
                f"Video is {declared / 2**20:.0f} MB; the limit is "
                f"{MAX_VIDEO_BYTES / 2**20:.0f} MB"
            )
        size = 0
        try:
            for chunk in response.iter_content(VIDEO_CHUNK_BYTES):
                size += len(chunk)
                if size > MAX_VIDEO_BYTES:
                    raise ToolError(
                        f"Video is larger than the "
                        f"{MAX_VIDEO_BYTES / 2**20:.0f} MB limit"
                    )
                file.write(chunk)
        except requests.RequestException as ex:
            raise ToolError(f"Download of {url} broke off: {ex}")
        file.flush()
        disposition = response.headers.get("Content-Disposition", "")
        name = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disposition)
    if not size:
        raise ToolError(f"{url} returned an empty file")
    return {
        "source_url": url,
        "download_url": source,
        "file_name": name.group(1) if name else None,
        "content_type": content_type or None,
        "size_bytes": size,
        "size_mb": round(size / 2**20, 1),
    }


def _boxes(file: Any, start: int, end: int):
    """Yields (type, payload_start, box_end) of ISO-BMFF boxes in a range."""
    offset = start
    while offset + 8 <= end:
        file.seek(offset)
        header = file.read(16)
        if len(header) < 8:
            return
        size, kind = struct.unpack(">I4s", header[:8])
        payload = offset + 8
        if size == 1 and len(header) == 16:
            size = struct.unpack(">Q", header[8:16])[0]
            payload = offset + 16
        elif size == 0:
            size = end - offset
        if size < payload - offset:
            return
        yield kind.decode("latin-1"), payload, min(offset + size, end)
        offset += size


def _child(file: Any, start: int, end: int, kind: str) -> Optional[Tuple]:
    for box in _boxes(file, start, end):
        if box[0] == kind:
            return box
    return None


def _inspect_video(file: Any) -> Dict[str, Any]:
    """Container format, duration and frame size, read from the headers only.

    Covers MP4/MOV (the common ad formats) fully; other containers are only
    recognised. YouTube transcodes, so an unknown format is a warning.
    """
    end = file.seek(0, os.SEEK_END)
    file.seek(0)
    head = file.read(12)
    if head[4:8] == b"ftyp":
        info: Dict[str, Any] = {
            "format": "MOV" if head[8:12] == b"qt  " else "MP4",
            "brand": head[8:12].decode("latin-1").strip(),
        }
    elif head[:4] == b"\x1a\x45\xdf\xa3":
        return {"format": "WEBM/MKV"}
    elif head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return {"format": "AVI"}
    elif head.lstrip()[:1] == b"<":
        raise ToolError("The link returned a web page, not a video file")
    else:
        return {"format": "UNKNOWN"}

    moov = _child(file, 0, end, "moov")
    if not moov:
        info["warning"] = "No 'moov' header found; the file may be truncated"
        return info
    mvhd = _child(file, moov[1], moov[2], "mvhd")
    if mvhd:
        file.seek(mvhd[1])
        version = file.read(1)[0]
        if version == 1:
            file.seek(mvhd[1] + 20)
            timescale, duration = struct.unpack(">IQ", file.read(12))
        else:
            file.seek(mvhd[1] + 12)
            timescale, duration = struct.unpack(">II", file.read(8))
        if timescale:
            info["duration_s"] = round(duration / timescale, 2)
    for kind, start, stop in _boxes(file, moov[1], moov[2]):
        if kind != "trak":
            continue
        tkhd = _child(file, start, stop, "tkhd")
        if not tkhd or tkhd[2] - tkhd[1] < 84:
            continue
        file.seek(tkhd[2] - 8)
        width, height = struct.unpack(">II", file.read(8))
        if width and height:
            info["width"], info["height"] = width >> 16, height >> 16
            info["aspect_ratio"] = round(info["width"] / info["height"], 3)
            break
    return info


def _check_youtube_text(field: str, text: str, limit: int) -> None:
    if len(text) > limit:
        raise ToolError(f"{field} is {len(text)} chars, YouTube allows {limit}")
    if _YOUTUBE_FORBIDDEN.search(text):
        raise ToolError(f"{field} must not contain '<' or '>'")


def _upload_status(
    customer_id: str, video_upload_id: str, login_customer_id: Optional[str]
) -> Dict[str, Any]:
    rows = _query(
        customer_id,
        "SELECT you_tube_video_upload.resource_name,"
        " you_tube_video_upload.video_upload_id,"
        " you_tube_video_upload.channel_id,"
        " you_tube_video_upload.video_id,"
        " you_tube_video_upload.state,"
        " you_tube_video_upload.video_privacy"
        " FROM you_tube_video_upload"
        f" WHERE you_tube_video_upload.video_upload_id = {video_upload_id}",
        login_customer_id,
    )
    if not rows:
        raise ToolError(
            f"No YouTube video upload {video_upload_id} in customer "
            f"{customer_id}"
        )
    upload = rows[0].you_tube_video_upload
    state = _enum_name(upload.state)
    result = {
        "resource_name": upload.resource_name,
        "video_upload_id": str(upload.video_upload_id),
        "channel_id": upload.channel_id or None,
        "state": state,
        "video_privacy": _enum_name(upload.video_privacy),
        "video_id": upload.video_id or None,
    }
    if upload.video_id:
        result["youtube_url"] = f"https://youtu.be/{upload.video_id}"
        result["asset_ref"] = {"youtube_video": upload.video_id}
    result["next_step"] = {
        "PENDING": "Still uploading; check again later.",
        "UPLOADED": "Uploaded, YouTube is processing it; check again in a "
        "few minutes. It can already be linked, but serves after PROCESSED.",
        "PROCESSED": "Ready. Use asset_ref as a YOUTUBE_VIDEO asset in "
        "create_asset_group / copy_asset_group / update_asset_group / "
        "create_ad / upload_assets.",
        "FAILED": "YouTube could not process the file; upload it again.",
        "REJECTED": "YouTube rejected the video (policy or format).",
        "UNAVAILABLE": "The video is no longer available on YouTube.",
    }.get(state, "Unknown state; check again later.")
    return result


def upload_youtube_video(
    customer_id: str,
    video_url: str,
    title: str,
    description: Optional[str] = None,
    channel_id: Optional[str] = None,
    privacy: Literal["UNLISTED", "PUBLIC"] = "UNLISTED",
    generative_content: Optional[bool] = None,
    login_customer_id: Optional[str] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Upload a video file to YouTube through Google Ads, to use as an asset.

    The server downloads the file (a direct link or a Google Drive share link
    with "Anyone with the link" access) and uploads it with the Google Ads
    API's YouTubeVideoUploadService, so no YouTube credentials are needed.

    There is no validate_only for uploads, so this is a two-step tool:
      * confirm=false (default): downloads and inspects the file (size,
        format, duration, frame size) and returns the plan. Nothing is
        uploaded. Show the plan to the user.
      * confirm=true: downloads again and uploads. Processing is asynchronous:
        poll get_youtube_video_upload_status until state is PROCESSED, then use
        {"youtube_video": video_id} in create_asset_group / copy_asset_group /
        update_asset_group / create_ad.

    Destination: without channel_id the video goes to the Google-managed
    YouTube channel of the Ads account, where only UNLISTED is allowed. With
    channel_id (a brand channel linked to the account) privacy may be UNLISTED
    or PUBLIC; the API has no PRIVATE option.

    privacy="PUBLIC" publishes the video publicly on the brand channel. Only
    call with confirm=true and PUBLIC after the user has explicitly confirmed
    that exact video, title and channel for public publication.

    Args:
        customer_id: Google Ads customer ID (digits only).
        video_url: Direct file URL or Google Drive share link.
        title: YouTube title, up to 100 chars, no '<' or '>'.
        description: YouTube description, up to 5000 chars.
        channel_id: Brand YouTube channel ID (UC...). Omit for the
            Google-managed channel of the account.
        privacy: UNLISTED (default) or PUBLIC (brand channel only).
        generative_content: Disclose whether the video contains realistic
            AI-generated content (true/false); omit if not known.
        login_customer_id: Manager (MCC) account ID to use as login-customer-id.
        confirm: false = plan only (nothing uploaded); true = upload.
    """
    customer_id = _cid(customer_id)
    title = (title or "").strip()
    if not title:
        raise ToolError("title is required")
    _check_youtube_text("title", title, 100)
    description = (description or "").strip()
    _check_youtube_text("description", description, 5000)
    privacy = (privacy or "UNLISTED").strip().upper()
    if privacy not in YOUTUBE_PRIVACIES:
        raise ToolError(
            f"privacy must be one of {', '.join(YOUTUBE_PRIVACIES)}; the "
            "Google Ads API has no PRIVATE option"
        )
    channel_id = (channel_id or "").strip() or None
    if channel_id and not _YOUTUBE_CHANNEL.match(channel_id):
        raise ToolError(
            f"channel_id must look like UC + 22 characters, got {channel_id!r}"
        )
    if not channel_id and privacy != "UNLISTED":
        raise ToolError(
            "The Google-managed channel only allows UNLISTED; pass the brand "
            "channel_id to publish PUBLIC"
        )

    plan = {
        "customer_id": customer_id,
        "channel": channel_id or "Google-managed channel of the Ads account",
        "privacy": privacy,
        "title": title,
        "description": description or None,
        "generative_content": generative_content,
    }
    if confirm:  # Built before the download, so field errors surface first.
        client = utils.get_googleads_client(login_customer_id=login_customer_id)
        upload = client.get_type("YouTubeVideoUpload")
        upload.video_title = title
        if description:
            upload.video_description = description
        if channel_id:
            upload.channel_id = channel_id
        upload.video_privacy = client.enums.YouTubeVideoPrivacyEnum[privacy]
        if generative_content is not None:
            try:
                disclosure = (
                    client.enums.YouTubeVideoUploadGenerativeContentDisclosureEnum
                )
            except AttributeError:
                raise ToolError(
                    "generative_content needs Google Ads API v26 "
                    "(google-ads>=34); omit it or upgrade the server"
                )
            upload.video_content_metadata.generative_content_disclosure = (
                disclosure.GENERATIVE_CONTENT
                if generative_content
                else disclosure.NON_GENERATIVE_CONTENT
            )
    with tempfile.TemporaryDirectory(prefix="ads-video-") as tmp:
        path = os.path.join(tmp, "video")
        with open(path, "w+b") as file:
            info = _download_video(video_url, file)
            info.update(_inspect_video(file))
            if not confirm:
                result = {
                    "confirm": False,
                    "status": "PLANNED (nothing uploaded)",
                    "plan": plan,
                    "file": info,
                    "next_step": (
                        "Show the plan and file details to the user and, after "
                        "explicit confirmation, call again with confirm=true."
                    ),
                }
                if privacy == "PUBLIC":
                    result["warning"] = (
                        "PUBLIC publishes the video on the brand channel for "
                        "everyone. Confirm this exact video, title and channel "
                        "with the user first."
                    )
                return result

            service = client.get_service("YouTubeVideoUploadService")
            file.seek(0)
            try:
                response = service.create_you_tube_video_upload(
                    customer_id=customer_id,
                    you_tube_video_upload=upload,
                    stream=file,
                )
            except GoogleAdsException as ex:
                raise ToolError(_api_error(ex))
            except (
                gax_exceptions.GoogleAPICallError,
                requests.RequestException,
                RuntimeError,
                ValueError,
            ) as ex:
                raise ToolError(f"YouTube video upload failed: {ex}")

    resource_name = response.resource_name
    match = _UPLOAD_RN.match(resource_name)
    video_upload_id = match.group(2) if match else resource_name
    result = {
        "confirm": True,
        "status": "UPLOADED",
        "resource_name": resource_name,
        "video_upload_id": video_upload_id,
        "plan": plan,
        "file": info,
        "next_step": (
            "Poll get_youtube_video_upload_status until state is PROCESSED, "
            "then use {'youtube_video': video_id} as a YOUTUBE_VIDEO asset."
        ),
        "log_payload": {
            "platform": "google_ads",
            "customer_id": customer_id,
            "action": "upload_youtube_video",
            "resource_name": resource_name,
            "channel": plan["channel"],
            "privacy": privacy,
            "title": title,
            "source_url": video_url,
        },
    }
    if match:
        try:
            status = _upload_status(
                customer_id, video_upload_id, login_customer_id
            )
        except ToolError:
            status = None  # Not readable yet; the upload itself succeeded.
        if status:
            result.update(
                {k: status[k] for k in ("state", "video_id") if status[k]}
            )
    return result


def get_youtube_video_upload_status(
    customer_id: str,
    video_upload_id: str,
    login_customer_id: Optional[str] = None,
) -> Dict[str, Any]:
    """State and YouTube video ID of an upload made by upload_youtube_video.

    States: PENDING (uploading), UPLOADED (YouTube is processing), PROCESSED
    (ready), FAILED, REJECTED, UNAVAILABLE. Once video_id is set, asset_ref is
    the value to pass as a YOUTUBE_VIDEO asset.

    Args:
        customer_id: Google Ads customer ID (digits only).
        video_upload_id: The ID (or resource name) returned by the upload.
        login_customer_id: Manager (MCC) account ID to use as login-customer-id.
    """
    customer_id = _cid(customer_id)
    match = _UPLOAD_RN.match(str(video_upload_id).strip())
    upload_id = _cid(match.group(2) if match else video_upload_id)
    return _upload_status(customer_id, upload_id, login_customer_id)


# --- Registration -----------------------------------------------------------

for _fn in (get_campaign_creatives, get_youtube_video_upload_status):
    mcp.add_tool(
        Tool.from_function(_fn, annotations=ToolAnnotations(readOnlyHint=True))
    )
for _fn in (
    create_asset_group,
    copy_asset_group,
    create_ad,
    copy_ad,
    upload_assets,
    upload_youtube_video,
):
    mcp.add_tool(
        Tool.from_function(
            _fn,
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=False
            ),
        )
    )
for _fn in (update_asset_group, link_assets):
    mcp.add_tool(
        Tool.from_function(
            _fn,
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=True
            ),
        )
    )
