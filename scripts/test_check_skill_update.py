import base64
import json
import unittest
from unittest.mock import MagicMock, patch

import check_skill_update as updater


class SkillUpdateCheckTests(unittest.TestCase):
    def test_github_raw_url_uses_refs_heads_route(self) -> None:
        url = updater._github_raw_url(
            "https://github.com/weiweidounai0131/biaoshu-master.git",
            "main",
            "skill-version.json",
        )
        self.assertEqual(
            url,
            "https://github.com/weiweidounai0131/biaoshu-master/raw/refs/heads/main/skill-version.json",
        )

    def test_contents_api_payload_is_decoded(self) -> None:
        manifest = {"schema_version": 1, "version": "0.1.5"}
        encoded = base64.b64encode(json.dumps(manifest).encode("utf-8")).decode("ascii")
        response = MagicMock()
        response.read.return_value = json.dumps({"encoding": "base64", "content": encoded}).encode("utf-8")
        response.__enter__.return_value = response

        with patch.object(updater.urllib_request, "urlopen", return_value=response):
            data, reason = updater._fetch_json("https://api.github.com/repos/example/repo/contents/skill-version.json?ref=main")

        self.assertIsNone(reason)
        self.assertEqual(data, manifest)


if __name__ == "__main__":
    unittest.main()
