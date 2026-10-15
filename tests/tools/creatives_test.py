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

"""Tests for the creative tools.

The operations are parsed into real Google Ads protos (the installed API
version), so a wrong field or enum name fails here, not in production; only the
network call is mocked.
"""

import struct
import unittest
from unittest import mock

import requests

from fastmcp.exceptions import ToolError
from google.auth.credentials import AnonymousCredentials
from google.ads.googleads.client import GoogleAdsClient

from ads_mcp.tools import creatives

CUSTOMER = "1234567890"

PMAX = {
    "id": "111",
    "name": "PMax",
    "status": "ENABLED",
    "type": "PERFORMANCE_MAX",
    "sub_type": "UNSPECIFIED",
    "brand_guidelines_enabled": False,
    "merchant_id": None,
}

SIZES = {
    "https://img/land.jpg": (1200, 628),
    "https://img/square.jpg": (1200, 1200),
    "https://img/portrait.jpg": (960, 1200),
    "https://img/logo.png": (512, 512),
    "https://img/odd.jpg": (1000, 300),
}


def fake_image(ref):
    width, height = SIZES[ref["image_url"]]
    return b"\x89PNG fake", width, height


class CreativesTestBase(unittest.TestCase):
    def setUp(self):
        self.client = GoogleAdsClient(
            credentials=AnonymousCredentials(),
            developer_token="t",
            use_proto_plus=True,
        )
        self.service = mock.MagicMock()
        self.service.mutate.return_value = mock.MagicMock(
            mutate_operation_responses=[]
        )
        self.client.get_service = mock.MagicMock(return_value=self.service)
        patches = [
            mock.patch.object(
                creatives.utils,
                "get_googleads_client",
                return_value=self.client,
            ),
            mock.patch.object(creatives, "_load_image", side_effect=fake_image),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def sent(self):
        """The operations of the last mutate, as dicts."""
        request = self.service.mutate.call_args.kwargs["request"]
        self.assertEqual(request["customer_id"], CUSTOMER)
        return [
            creatives._to_dict(op) for op in request["mutate_operations"]
        ], request

    @staticmethod
    def kinds(ops):
        return [next(iter(op)) for op in ops]

    @staticmethod
    def first(ops, kind):
        return next(op[kind] for op in ops if kind in op)

    def assert_assets_first(self, ops):
        """New assets precede everything else: Google counts a PMax group's
        assets as the links are processed."""
        kinds = self.kinds(ops)
        seen_other = False
        for kind in kinds:
            if kind != "asset_operation":
                seen_other = True
            else:
                self.assertFalse(
                    seen_other, f"asset create after other ops: {kinds}"
                )


class AssetGroupTest(CreativesTestBase):
    def spec(self, **extra):
        spec = {
            "name": "Spring",
            "final_urls": ["https://joom.com/spring"],
            "assets": {
                "HEADLINE": ["Spring sale", "New arrivals", "Up to -70%"],
                "LONG_HEADLINE": ["Everything for spring at the best prices"],
                "DESCRIPTION": [
                    "Fast delivery",
                    "Millions of products for the season",
                ],
                "BUSINESS_NAME": ["Joom"],
                "LOGO": [{"image_url": "https://img/logo.png"}],
                "YOUTUBE_VIDEO": [
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
                ],
                "CALL_TO_ACTION_SELECTION": ["SHOP_NOW"],
            },
            "images": [
                "https://img/land.jpg",
                "https://img/square.jpg",
                "https://img/portrait.jpg",
            ],
            "signals": {"search_themes": ["spring dresses"]},
        }
        spec.update(extra)
        return spec

    def test_create_builds_one_valid_atomic_request(self):
        with mock.patch.object(creatives, "_campaign_info", return_value=PMAX):
            result = creatives.create_asset_group(CUSTOMER, "111", self.spec())

        ops, request = self.sent()
        self.assertTrue(request["validate_only"])
        self.assert_assets_first(ops)
        group = self.first(ops, "asset_group_operation")["create"]
        self.assertEqual(group["status"], "PAUSED")
        self.assertEqual(
            group["campaign"], f"customers/{CUSTOMER}/campaigns/111"
        )

        links = [
            op["asset_group_asset_operation"]["create"]
            for op in ops
            if "asset_group_asset_operation" in op
        ]
        by_type = {}
        for link in links:
            by_type.setdefault(link["field_type"], 0)
            by_type[link["field_type"]] += 1
        self.assertEqual(by_type["MARKETING_IMAGE"], 1)
        self.assertEqual(by_type["SQUARE_MARKETING_IMAGE"], 1)
        self.assertEqual(by_type["PORTRAIT_MARKETING_IMAGE"], 1)
        self.assertEqual(by_type["HEADLINE"], 3)
        self.assertEqual(by_type["YOUTUBE_VIDEO"], 1)

        # Every link points at an asset created earlier in the same request.
        created = []
        for op in ops:
            if "asset_operation" in op:
                created.append(op["asset_operation"]["create"]["resource_name"])
            if "asset_group_asset_operation" in op:
                self.assertIn(
                    op["asset_group_asset_operation"]["create"]["asset"],
                    created,
                )
        video = [
            op
            for op in ops
            if "asset_operation" in op
            and "youtube_video_asset" in op["asset_operation"]["create"]
        ]
        self.assertEqual(
            video[0]["asset_operation"]["create"]["youtube_video_asset"][
                "youtube_video_id"
            ],
            "dQw4w9WgXcQ",
        )
        themes = [
            op["asset_group_signal_operation"]["create"]["search_theme"]["text"]
            for op in ops
            if "asset_group_signal_operation" in op
        ]
        self.assertEqual(themes, ["spring dresses"])
        self.assertEqual(result["status"], "VALIDATED (nothing written)")
        self.assertNotIn("log_payload", result)

    def test_retail_campaign_gets_all_products_filter(self):
        retail = {**PMAX, "merchant_id": "555"}
        with mock.patch.object(
            creatives, "_campaign_info", return_value=retail
        ):
            creatives.create_asset_group(CUSTOMER, "111", self.spec())
        ops, _ = self.sent()
        filters = [
            op["asset_group_listing_group_filter_operation"]["create"]
            for op in ops
            if "asset_group_listing_group_filter_operation" in op
        ]
        self.assertEqual(len(filters), 1)
        self.assertEqual(filters[0]["type_"], "UNIT_INCLUDED")
        self.assertEqual(filters[0]["listing_source"], "SHOPPING")

    def test_multi_node_tree_goes_in_the_same_request(self):
        retail = {**PMAX, "merchant_id": "555"}
        tree = [
            {"id": "1", "type": "SUBDIVISION", "listing_source": "SHOPPING"},
            {
                "id": "3",
                "type": "UNIT_EXCLUDED",
                "listing_source": "SHOPPING",
                "case_value": {"product_brand": {"value": "acme"}},
                "parent_id": "1",
            },
            {
                "id": "2",
                "type": "UNIT_INCLUDED",
                "listing_source": "SHOPPING",
                "case_value": {"product_brand": {}},
                "parent_id": "1",
            },
        ]
        with mock.patch.object(
            creatives, "_campaign_info", return_value=retail
        ):
            creatives.create_asset_group(
                CUSTOMER, "111", self.spec(listing_group_filters=tree)
            )
        ops, _ = self.sent()
        group_rn = self.first(ops, "asset_group_operation")["create"][
            "resource_name"
        ]
        group_id = group_rn.rsplit("/", 1)[-1]
        nodes = [
            op["asset_group_listing_group_filter_operation"]["create"]
            for op in ops
            if "asset_group_listing_group_filter_operation" in op
        ]
        self.assertEqual(len(nodes), 3)
        root = nodes[0]["resource_name"]
        self.assertTrue(
            root.endswith(f"/{group_id}~" + root.rsplit("~", 1)[-1])
        )
        self.assertTrue(group_id.startswith("-"))
        for child in nodes[1:]:
            self.assertEqual(child["parent_listing_group_filter"], root)

    def test_brand_guidelines_move_brand_assets_out(self):
        branded = {**PMAX, "brand_guidelines_enabled": True}
        with mock.patch.object(
            creatives, "_campaign_info", return_value=branded
        ):
            result = creatives.create_asset_group(CUSTOMER, "111", self.spec())
        ops, _ = self.sent()
        types = {
            op["asset_group_asset_operation"]["create"]["field_type"]
            for op in ops
            if "asset_group_asset_operation" in op
        }
        self.assertNotIn("BUSINESS_NAME", types)
        self.assertNotIn("LOGO", types)
        self.assertTrue(
            any("brand guidelines" in note for note in result["notes"])
        )

    def test_rejects_too_long_headline_and_odd_image(self):
        with mock.patch.object(creatives, "_campaign_info", return_value=PMAX):
            with self.assertRaisesRegex(ToolError, "max 30"):
                creatives.create_asset_group(
                    CUSTOMER, "111", self.spec(assets={"HEADLINE": ["x" * 31]})
                )
            with self.assertRaisesRegex(ToolError, "1000x300"):
                creatives.create_asset_group(
                    CUSTOMER, "111", self.spec(images=["https://img/odd.jpg"])
                )
        self.service.mutate.assert_not_called()

    def test_non_pmax_campaign_is_refused(self):
        with mock.patch.object(
            creatives, "_campaign_info", return_value={**PMAX, "type": "SEARCH"}
        ):
            with self.assertRaisesRegex(ToolError, "create_ad"):
                creatives.create_asset_group(CUSTOMER, "111", self.spec())

    def source_group(self):
        def view(asset_id, field_type, **content):
            return {
                "asset": f"customers/{CUSTOMER}/assets/{asset_id}",
                "asset_id": str(asset_id),
                "link": f"customers/{CUSTOMER}/assetGroupAssets/9~{asset_id}~{field_type}",
                **content,
            }

        return {
            "asset_group_id": "9",
            "name": "Source",
            "status": "ENABLED",
            "final_urls": ["https://joom.com/a"],
            "assets": {
                "HEADLINE": [
                    view(1, "HEADLINE", text="Old 1"),
                    view(2, "HEADLINE", text="Old 2"),
                    view(3, "HEADLINE", text="Keep"),
                ],
                "MARKETING_IMAGE": [
                    view(
                        4,
                        "MARKETING_IMAGE",
                        type="IMAGE",
                        image_url="https://img/land.jpg",
                        width=1200,
                        height=628,
                    )
                ],
                "SQUARE_MARKETING_IMAGE": [
                    view(
                        5,
                        "SQUARE_MARKETING_IMAGE",
                        type="IMAGE",
                        image_url="https://img/square.jpg",
                        width=1200,
                        height=1200,
                    )
                ],
                "LOGO": [
                    view(
                        6,
                        "LOGO",
                        type="IMAGE",
                        image_url="https://img/logo.png",
                        width=512,
                        height=512,
                    )
                ],
                "YOUTUBE_VIDEO": [
                    view(7, "YOUTUBE_VIDEO", youtube_video="dQw4w9WgXcQ"),
                    view(
                        8,
                        "YOUTUBE_VIDEO",
                        youtube_video="aaaaaaaaaaa",
                        source="AUTOMATICALLY_CREATED",
                    ),
                ],
            },
            "signals": {
                "search_themes": ["shoes"],
                "audiences": [f"customers/{CUSTOMER}/audiences/77"],
            },
            "listing_group_filters": [],
        }

    def test_copy_reuses_assets_and_replaces_overrides(self):
        query_row = mock.MagicMock()
        query_row.asset_group.campaign = f"customers/{CUSTOMER}/campaigns/111"
        with (
            mock.patch.object(creatives, "_campaign_info", return_value=PMAX),
            mock.patch.object(creatives, "_query", return_value=[query_row]),
            mock.patch.object(
                creatives,
                "_read_asset_groups",
                return_value=[self.source_group()],
            ),
        ):
            result = creatives.copy_asset_group(
                CUSTOMER,
                "9",
                overrides={
                    "assets": {"HEADLINE": ["New A", "New B", "New C"]},
                    "images": ["https://img/portrait.jpg"],
                },
            )
        ops, _ = self.sent()
        self.assert_assets_first(ops)
        group = self.first(ops, "asset_group_operation")["create"]
        self.assertTrue(group["name"].startswith("Source copy"))
        self.assertEqual(group["status"], "PAUSED")
        links = [
            op["asset_group_asset_operation"]["create"]
            for op in ops
            if "asset_group_asset_operation" in op
        ]
        logo = [l for l in links if l["field_type"] == "LOGO"]
        self.assertEqual(
            logo[0]["asset"], f"customers/{CUSTOMER}/assets/6"
        )  # reused, not re-uploaded
        self.assertFalse(
            [l for l in links if l["field_type"] == "MARKETING_IMAGE"]
        )  # replaced by images
        self.assertEqual(
            len(
                [
                    l
                    for l in links
                    if l["field_type"] == "PORTRAIT_MARKETING_IMAGE"
                ]
            ),
            1,
        )
        videos = [
            l["asset"] for l in links if l["field_type"] == "YOUTUBE_VIDEO"
        ]
        self.assertEqual(
            videos, [f"customers/{CUSTOMER}/assets/7"]
        )  # auto-created one not copied
        texts = [
            op["asset_operation"]["create"]["text_asset"]["text"]
            for op in ops
            if "asset_operation" in op
            and "text_asset" in op["asset_operation"]["create"]
        ]
        self.assertEqual(texts, ["New A", "New B", "New C"])
        signals = [
            op["asset_group_signal_operation"]["create"]
            for op in ops
            if "asset_group_signal_operation" in op
        ]
        self.assertEqual(len(signals), 2)
        self.assertEqual(result["source"]["asset_group_id"], "9")

    def test_copy_to_other_account_reuploads_content(self):
        query_row = mock.MagicMock()
        query_row.asset_group.campaign = f"customers/{CUSTOMER}/campaigns/111"
        other = "9999999999"
        with (
            mock.patch.object(creatives, "_campaign_info", return_value=PMAX),
            mock.patch.object(creatives, "_query", return_value=[query_row]),
            mock.patch.object(
                creatives,
                "_read_asset_groups",
                return_value=[self.source_group()],
            ),
        ):
            with self.assertRaisesRegex(ToolError, "target_campaign_id"):
                creatives.copy_asset_group(
                    CUSTOMER, "9", target_customer_id=other
                )
            creatives.copy_asset_group(
                CUSTOMER,
                "9",
                target_customer_id=other,
                target_campaign_id="222",
            )
        request = self.service.mutate.call_args.kwargs["request"]
        self.assertEqual(request["customer_id"], other)
        ops = [creatives._to_dict(op) for op in request["mutate_operations"]]
        for op in ops:
            if "asset_group_asset_operation" in op:
                self.assertTrue(
                    op["asset_group_asset_operation"]["create"][
                        "asset"
                    ].startswith(f"customers/{other}/assets/-")
                )
        self.assertFalse(
            [
                op
                for op in ops
                if "asset_group_signal_operation" in op
                and "audience" in op["asset_group_signal_operation"]["create"]
            ]
        )

    def test_update_replaces_only_listed_field_types(self):
        query_row = mock.MagicMock()
        query_row.asset_group.campaign = f"customers/{CUSTOMER}/campaigns/111"
        with (
            mock.patch.object(creatives, "_campaign_info", return_value=PMAX),
            mock.patch.object(creatives, "_query", return_value=[query_row]),
            mock.patch.object(
                creatives,
                "_read_asset_groups",
                return_value=[self.source_group()],
            ),
        ):
            result = creatives.update_asset_group(
                CUSTOMER,
                "9",
                set_assets={
                    "HEADLINE": ["Keep", "Fresh 1", "Fresh 2"],
                    "YOUTUBE_VIDEO": ["https://youtu.be/bbbbbbbbbbb"],
                },
                final_urls=["https://joom.com/b"],
            )
        ops, _ = self.sent()
        kinds = self.kinds(ops)
        self.assertEqual(
            self.first(ops, "asset_group_operation")["update_mask"],
            "finalUrls",
        )
        removes = [
            op["asset_group_asset_operation"]["remove"]
            for op in ops
            if "remove" in op.get("asset_group_asset_operation", {})
        ]
        self.assertCountEqual(
            removes,
            [
                f"customers/{CUSTOMER}/assetGroupAssets/9~1~HEADLINE",
                f"customers/{CUSTOMER}/assetGroupAssets/9~2~HEADLINE",
                f"customers/{CUSTOMER}/assetGroupAssets/9~7~YOUTUBE_VIDEO",
            ],
        )
        # Creates come before removes, so minimums hold throughout.
        last_create = max(
            i
            for i, op in enumerate(ops)
            if "create" in op.get("asset_group_asset_operation", {})
        )
        first_remove = min(
            i
            for i, op in enumerate(ops)
            if "remove" in op.get("asset_group_asset_operation", {})
        )
        self.assertLess(last_create, first_remove)
        self.assertIn("asset_group_operation", kinds)
        self.assertEqual(result["resulting_counts"]["HEADLINE"], 3)
        self.assertTrue(any("auto-created" in w for w in result["warnings"]))


class AdTest(CreativesTestBase):
    def test_create_responsive_display_ad_routes_images(self):
        result = creatives.create_ad(
            CUSTOMER,
            "55",
            {
                "type": "responsive_display_ad",
                "final_urls": ["https://joom.com"],
                "fields": {
                    "headlines": ["Joom sale"],
                    "long_headline": "Everything you need at the best prices",
                    "descriptions": ["Fast delivery"],
                    "business_name": "Joom",
                    "logo_images": ["https://img/logo.png"],
                    "youtube_videos": ["dQw4w9WgXcQ"],
                },
                "images": ["https://img/land.jpg", "https://img/square.jpg"],
            },
        )
        ops, _ = self.sent()
        ad_op = ops[-1]["ad_group_ad_operation"]["create"]
        self.assertEqual(ad_op["status"], "PAUSED")
        self.assertEqual(ad_op["ad_group"], f"customers/{CUSTOMER}/adGroups/55")
        rda = ad_op["ad"]["responsive_display_ad"]
        self.assertEqual(len(rda["marketing_images"]), 1)
        self.assertEqual(len(rda["square_marketing_images"]), 1)
        self.assertEqual(
            rda["long_headline"]["text"],
            "Everything you need at the best prices",
        )
        self.assertTrue(
            rda["youtube_videos"][0]["asset"].startswith(
                f"customers/{CUSTOMER}/assets/-"
            )
        )
        self.assertEqual(result["operations"]["ad_group_ad.create"], 1)

    def test_create_demand_gen_video_ad(self):
        creatives.create_ad(
            CUSTOMER,
            "55",
            {
                "type": "demand_gen_video_responsive_ad",
                "final_urls": ["https://joom.com"],
                "fields": {
                    "headlines": ["Watch and shop"],
                    "long_headlines": ["Discover millions of products on Joom"],
                    "descriptions": ["Free returns"],
                    "videos": ["https://youtube.com/shorts/dQw4w9WgXcQ"],
                    "logo_images": ["https://img/logo.png"],
                    "business_name": "Joom",
                    "call_to_actions": ["SHOP_NOW"],
                },
            },
            status="ENABLED",
        )
        ops, _ = self.sent()
        ad = ops[-1]["ad_group_ad_operation"]["create"]
        self.assertEqual(ad["status"], "ENABLED")
        info = ad["ad"]["demand_gen_video_responsive_ad"]
        self.assertEqual(info["business_name"]["text"], "Joom")
        cta_assets = [
            op["asset_operation"]["create"]
            for op in ops
            if "asset_operation" in op
            and "call_to_action_asset" in op["asset_operation"]["create"]
        ]
        self.assertEqual(
            cta_assets[0]["call_to_action_asset"]["call_to_action"], "SHOP_NOW"
        )

    def test_unknown_field_is_rejected(self):
        with self.assertRaisesRegex(ToolError, "no field 'headline'"):
            creatives.create_ad(
                CUSTOMER,
                "55",
                {
                    "type": "responsive_search_ad",
                    "final_urls": ["https://x"],
                    "fields": {"headline": "x"},
                },
            )

    def test_copy_ad_overrides_and_pauses_source(self):
        source = {
            "ad_id": "800",
            "ad_group_id": "55",
            "status": "ENABLED",
            "type": "responsive_search_ad",
            "ad_type": "RESPONSIVE_SEARCH_AD",
            "final_urls": ["https://joom.com"],
            "fields": {
                "headlines": [
                    {"text": "A", "pinned_field": "HEADLINE_1"},
                    {"text": "B"},
                    {"text": "C"},
                ],
                "descriptions": [{"text": "D1"}, {"text": "D2"}],
                "path1": "sale",
            },
        }
        with mock.patch.object(creatives, "_read_ads", return_value=[source]):
            creatives.copy_ad(
                CUSTOMER,
                "800",
                overrides={"fields": {"descriptions": ["New D1", "New D2"]}},
                pause_source=True,
            )
        ops, _ = self.sent()
        ad_ops = [op["ad_group_ad_operation"] for op in ops]
        rsa = ad_ops[0]["create"]["ad"]["responsive_search_ad"]
        self.assertEqual(
            rsa["headlines"][0], {"text": "A", "pinned_field": "HEADLINE_1"}
        )
        self.assertEqual(
            [d["text"] for d in rsa["descriptions"]], ["New D1", "New D2"]
        )
        self.assertEqual(rsa["path1"], "sale")
        pause = ad_ops[1]
        self.assertEqual(
            pause["update"],
            {
                "resource_name": f"customers/{CUSTOMER}/adGroupAds/55~800",
                "status": "PAUSED",
            },
        )
        self.assertEqual(pause["update_mask"], "status")


class LinkAssetsTest(CreativesTestBase):
    def test_sitelinks_replace_existing(self):
        existing_row = mock.MagicMock()
        with (
            mock.patch.object(
                creatives, "_link_rows", return_value=[existing_row]
            ),
            mock.patch.object(
                creatives,
                "_grouped_assets",
                return_value={
                    "SITELINK": [
                        {
                            "asset": f"customers/{CUSTOMER}/assets/1",
                            "asset_id": "1",
                            "link": f"customers/{CUSTOMER}/campaignAssets/111~1~SITELINK",
                        }
                    ]
                },
            ),
        ):
            result = creatives.link_assets(
                CUSTOMER,
                "campaign",
                "SITELINK",
                [
                    {
                        "sitelink": {
                            "link_text": "Sale",
                            "description1": "Up to -70%",
                            "description2": "This week",
                            "final_urls": ["https://joom.com/sale"],
                        }
                    }
                ],
                target_id="111",
                replace_existing=True,
            )
        ops, _ = self.sent()
        asset = ops[0]["asset_operation"]["create"]
        self.assertEqual(asset["sitelink_asset"]["link_text"], "Sale")
        self.assertEqual(asset["final_urls"], ["https://joom.com/sale"])
        self.assertEqual(
            ops[1]["campaign_asset_operation"]["create"]["field_type"],
            "SITELINK",
        )
        self.assertEqual(
            ops[2]["campaign_asset_operation"]["remove"],
            f"customers/{CUSTOMER}/campaignAssets/111~1~SITELINK",
        )
        self.assertEqual(result["unlinked"], 1)

    def test_callouts_and_structured_snippets_at_customer_level(self):
        with mock.patch.object(creatives, "_link_rows", return_value=[]):
            creatives.link_assets(
                CUSTOMER,
                "customer",
                "CALLOUT",
                ["Free delivery", "Easy returns"],
            )
        ops, _ = self.sent()
        self.assertEqual(
            ops[0]["asset_operation"]["create"]["callout_asset"][
                "callout_text"
            ],
            "Free delivery",
        )
        self.assertIn("customer_asset_operation", ops[2])

        with mock.patch.object(creatives, "_link_rows", return_value=[]):
            creatives.link_assets(
                CUSTOMER,
                "ad_group",
                "STRUCTURED_SNIPPET",
                [
                    {
                        "structured_snippet": {
                            "header": "Brands",
                            "values": ["A", "B", "C"],
                        }
                    }
                ],
                target_id="55",
            )
        ops, _ = self.sent()
        self.assertEqual(
            self.first(ops, "ad_group_asset_operation")["create"]["ad_group"],
            f"customers/{CUSTOMER}/adGroups/55",
        )


def box(kind, payload):
    return struct.pack(">I4s", 8 + len(payload), kind.encode()) + payload


def fake_mp4(seconds=12.5, width=1080, height=1920):
    """Smallest MP4 the inspector reads: ftyp, then moov/mvhd + trak/tkhd."""
    mvhd = bytes(4) + struct.pack(">III", 0, 0, 1000)
    mvhd += struct.pack(">I", int(seconds * 1000)) + bytes(80)
    tkhd = bytes(76) + struct.pack(">II", width << 16, height << 16)
    moov = box("moov", box("mvhd", mvhd) + box("trak", box("tkhd", tkhd)))
    return box("ftyp", b"isom" + bytes(4)) + box("mdat", b"x" * 64) + moov


def fake_download(body, headers=None, status=200):
    response = mock.MagicMock()
    response.ok = status < 400
    response.status_code = status
    response.headers = {"Content-Type": "video/mp4", **(headers or {})}
    response.iter_content.return_value = [body[:50], body[50:]]
    response.__enter__.return_value = response
    return response


class YouTubeUploadTest(CreativesTestBase):
    URL = "https://drive.google.com/file/d/1AbCdEf/view?usp=sharing"
    CHANNEL = "UC" + "a" * 22

    def setUp(self):
        super().setUp()
        self.mp4 = fake_mp4()
        self.get = mock.patch.object(
            creatives.requests,
            "get",
            return_value=fake_download(self.mp4),
        ).start()
        self.addCleanup(mock.patch.stopall)
        self.uploaded = []

        def upload(customer_id, you_tube_video_upload, stream):
            self.uploaded.append(stream.read())
            return mock.MagicMock(
                resource_name=f"customers/{customer_id}/youTubeVideoUploads/77"
            )

        self.service.create_you_tube_video_upload.side_effect = upload

    def test_plan_downloads_and_inspects_without_uploading(self):
        result = creatives.upload_youtube_video(CUSTOMER, self.URL, "Spring ad")
        self.assertEqual(result["status"], "PLANNED (nothing uploaded)")
        self.assertFalse(self.service.create_you_tube_video_upload.called)
        self.assertEqual(
            self.get.call_args.args[0],
            "https://drive.usercontent.google.com/download"
            "?id=1AbCdEf&export=download&confirm=t",
        )
        self.assertTrue(self.get.call_args.kwargs["stream"])
        file = result["file"]
        self.assertEqual(file["size_bytes"], len(self.mp4))
        self.assertEqual(file["format"], "MP4")
        self.assertEqual(file["duration_s"], 12.5)
        self.assertEqual((file["width"], file["height"]), (1080, 1920))
        self.assertEqual(result["plan"]["privacy"], "UNLISTED")
        self.assertNotIn("log_payload", result)

    def test_confirm_uploads_real_proto_and_reads_status(self):
        status_row = mock.MagicMock()
        status_row.you_tube_video_upload = self.client.get_type(
            "YouTubeVideoUpload"
        )
        status_row.you_tube_video_upload.video_upload_id = 77
        status_row.you_tube_video_upload.state = (
            self.client.enums.YouTubeVideoUploadStateEnum.UPLOADED
        )
        status_row.you_tube_video_upload.video_id = "abcdefghijk"
        with mock.patch.object(
            creatives, "_query", return_value=[status_row]
        ) as query:
            result = creatives.upload_youtube_video(
                CUSTOMER,
                "https://cdn.example.com/ad.mp4",
                "Spring ad",
                description="New collection",
                channel_id=self.CHANNEL,
                privacy="public",
                generative_content=False,
                confirm=True,
            )
        self.assertIn("video_upload_id = 77", query.call_args.args[1])
        self.assertEqual(self.uploaded, [self.mp4])
        kwargs = self.service.create_you_tube_video_upload.call_args.kwargs
        self.assertEqual(kwargs["customer_id"], CUSTOMER)
        sent = creatives._to_dict(kwargs["you_tube_video_upload"])
        self.assertEqual(
            sent,
            {
                "video_title": "Spring ad",
                "video_description": "New collection",
                "channel_id": self.CHANNEL,
                "video_privacy": "PUBLIC",
                "video_content_metadata": {
                    "generative_content_disclosure": "NON_GENERATIVE_CONTENT"
                },
            },
        )
        self.assertEqual(result["video_upload_id"], "77")
        self.assertEqual(result["state"], "UPLOADED")
        self.assertEqual(result["video_id"], "abcdefghijk")
        self.assertEqual(
            result["log_payload"]["action"], "upload_youtube_video"
        )
        self.assertEqual(result["log_payload"]["privacy"], "PUBLIC")

    def test_upload_survives_unreadable_status(self):
        with mock.patch.object(
            creatives, "_query", side_effect=ToolError("not yet")
        ):
            result = creatives.upload_youtube_video(
                CUSTOMER, self.URL, "Spring ad", confirm=True
            )
        self.assertEqual(result["status"], "UPLOADED")
        self.assertNotIn("state", result)
        sent = creatives._to_dict(
            self.service.create_you_tube_video_upload.call_args.kwargs[
                "you_tube_video_upload"
            ]
        )
        self.assertEqual(sent["video_privacy"], "UNLISTED")
        self.assertNotIn("channel_id", sent)

    def test_rejects_bad_requests_before_downloading(self):
        cases = [
            dict(title=""),
            dict(title="x" * 101),
            dict(title="<b>Sale</b>"),
            dict(privacy="PRIVATE"),
            dict(privacy="PUBLIC"),  # Google-managed channel: UNLISTED only.
            dict(channel_id="not-a-channel"),
        ]
        for case in cases:
            args = {"title": "Spring ad", **case}
            with self.subTest(case=case), self.assertRaises(ToolError):
                creatives.upload_youtube_video(CUSTOMER, self.URL, **args)
        self.assertFalse(self.get.called)

    def test_download_errors_are_clear(self):
        signin = fake_download(b"<html>", {"Content-Type": "text/html"})
        signin.url = "https://accounts.google.com/v3/signin/identifier?x=1"
        cases = [
            (fake_download(b"<html>", {"Content-Type": "text/html"}), "page"),
            (fake_download(b"", status=403), "HTTP 403"),
            (signin, "sign in"),
            (fake_download(b""), "empty"),
            (
                fake_download(self.mp4, {"Content-Length": str(2**40)}),
                "limit",
            ),
        ]
        for response, message in cases:
            self.get.return_value = response
            with self.subTest(message=message):
                with self.assertRaisesRegex(ToolError, message):
                    creatives.upload_youtube_video(CUSTOMER, self.URL, "Ad")
        self.get.side_effect = requests.ConnectionError("refused")
        with self.assertRaisesRegex(ToolError, "Could not download"):
            creatives.upload_youtube_video(CUSTOMER, self.URL, "Ad")

    def test_streamed_size_cap(self):
        with mock.patch.object(creatives, "MAX_VIDEO_BYTES", 100):
            with self.assertRaisesRegex(ToolError, "limit"):
                creatives.upload_youtube_video(CUSTOMER, self.URL, "Ad")

    def test_status_by_id_or_resource_name(self):
        row = mock.MagicMock()
        row.you_tube_video_upload = self.client.get_type("YouTubeVideoUpload")
        row.you_tube_video_upload.video_upload_id = 77
        row.you_tube_video_upload.state = (
            self.client.enums.YouTubeVideoUploadStateEnum.PROCESSED
        )
        row.you_tube_video_upload.video_id = "abcdefghijk"
        with mock.patch.object(creatives, "_query", return_value=[row]) as q:
            result = creatives.get_youtube_video_upload_status(
                CUSTOMER, f"customers/{CUSTOMER}/youTubeVideoUploads/77"
            )
        self.assertIn("FROM you_tube_video_upload", q.call_args.args[1])
        self.assertIn("video_upload_id = 77", q.call_args.args[1])
        self.assertEqual(result["state"], "PROCESSED")
        self.assertEqual(result["asset_ref"], {"youtube_video": "abcdefghijk"})
        with mock.patch.object(creatives, "_query", return_value=[]):
            with self.assertRaisesRegex(ToolError, "No YouTube video upload"):
                creatives.get_youtube_video_upload_status(CUSTOMER, "78")


class HelpersTest(unittest.TestCase):
    def test_youtube_ids(self):
        for value in [
            "dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://youtube.com/shorts/dQw4w9WgXcQ",
        ]:
            self.assertEqual(creatives._youtube_id(value), "dQw4w9WgXcQ")
        with self.assertRaises(ToolError):
            creatives._youtube_id("https://vimeo.com/1")

    def test_normalize_ref_by_slot(self):
        self.assertEqual(
            creatives.normalize_ref("https://x/a.png", "LOGO"),
            {"image_url": "https://x/a.png"},
        )
        self.assertEqual(
            creatives.normalize_ref("abc", "HEADLINE"), {"text": "abc"}
        )
        self.assertEqual(
            creatives.normalize_ref("SHOP_NOW", "CALL_TO_ACTION_SELECTION"),
            {"call_to_action": "SHOP_NOW"},
        )
        self.assertEqual(
            creatives.normalize_ref(
                f"customers/{CUSTOMER}/assets/5", "HEADLINE"
            ),
            {"asset": f"customers/{CUSTOMER}/assets/5"},
        )

    def test_aspect_routing(self):
        self.assertEqual(
            creatives._route_by_aspect(1200, 628, creatives.PMAX_IMAGE_ROUTES),
            "MARKETING_IMAGE",
        )
        self.assertEqual(
            creatives._route_by_aspect(1080, 1920, creatives.PMAX_IMAGE_ROUTES),
            "TALL_PORTRAIT_MARKETING_IMAGE",
        )
        self.assertIsNone(
            creatives._route_by_aspect(1000, 300, creatives.PMAX_IMAGE_ROUTES)
        )


if __name__ == "__main__":
    unittest.main()
