import unittest
from unittest.mock import patch

import doubao_dashboard_server as dashboard
from monitor_core.analytics import doubao_owned_video_category_share


class OwnedVideoSourceShareTests(unittest.TestCase):
    def test_unified_analytics_share_honors_question_and_date(self):
        runs = [
            {
                "question": "祛痘精华液推荐", "day": "2026-08-17",
                "sources": [
                    {"canonical_url": "https://v.example/owned", "type": "视频", "own_brand": True, "owned_brands": ["兰芝"]},
                    {"canonical_url": "https://v.example/other", "type": "视频", "own_brand": False},
                    {"canonical_url": "https://a.example/article", "type": "文章", "own_brand": True},
                ],
            },
            {
                "question": "祛痘精华液推荐", "day": "2026-08-17",
                "sources": [
                    {"canonical_url": "https://v.example/owned", "type": "视频", "own_brand": True, "owned_brands": ["兰芝"]},
                ],
            },
            {
                "question": "眉毛增长液推荐", "day": "2026-08-16",
                "sources": [{"canonical_url": "https://v.example/brow", "type": "视频", "own_products": ["眉毛增长液"]}],
            },
        ]
        payload = doubao_owned_video_category_share(
            runs, question="祛痘精华液推荐", date="2026-08-17",
        )
        self.assertEqual(len(payload["rows"]), 1)
        row = payload["rows"][0]
        self.assertEqual(row["all_unique_links"], 3)
        self.assertEqual(row["video_unique_links"], 2)
        self.assertEqual(row["owned_video_unique_links"], 1)
        self.assertEqual(row["owned_video_refs"], 2)
        self.assertEqual(row["owned_video_link_share"], 33.33)
        self.assertEqual(row["owned_within_video_link_share"], 50.0)

    def test_uses_unique_links_and_category_total_as_primary_denominator(self):
        rows = [
            {"question": "祛痘精华液推荐", "href": "https://v.example/owned?utm_source=a", "title": "owned", "kind": "video"},
            {"question": "祛痘精华液推荐", "href": "https://v.example/owned?utm_source=b", "title": "owned", "kind": "video"},
            {"question": "祛痘精华液推荐", "href": "https://v.example/other", "title": "other", "kind": "video"},
            {"question": "祛痘精华液推荐", "href": "https://a.example/article", "title": "article", "kind": "article"},
            {"question": "眉毛增长液推荐", "href": "https://a.example/brow", "title": "article", "kind": "article"},
        ]

        def products(_href, title, _index):
            return (["自有祛痘精华液"], "标题") if title == "owned" else ([], "")

        with (
            patch.object(dashboard, "_owned_source_kind", side_effect=lambda row, *_: row["kind"]),
            patch.object(dashboard, "owned_source_products", side_effect=products),
            patch.object(dashboard, "owned_source_brands", return_value=([], "")),
        ):
            payload = dashboard.owned_video_source_share_by_category(rows, {}, {}, {})

        by_question = {row["question"]: row for row in payload["rows"]}
        acne = by_question["祛痘精华液推荐"]
        self.assertEqual(acne["all_unique_urls"], 3)
        self.assertEqual(acne["video_unique_urls"], 2)
        self.assertEqual(acne["owned_video_unique_urls"], 1)
        self.assertEqual(acne["owned_video_refs"], 2)
        self.assertAlmostEqual(acne["owned_video_link_share"], 1 / 3)
        self.assertAlmostEqual(acne["owned_within_video_link_share"], 1 / 2)
        self.assertEqual(by_question["眉毛增长液推荐"]["owned_video_unique_urls"], 0)


if __name__ == "__main__":
    unittest.main()
