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

    def test_gitcode_contents_url_is_derived(self) -> None:
        url = updater._gitcode_contents_api_url(
            "https://gitcode.com/gcw_mHRylKw0/biaoshu-master.git",
            "main",
            "skill-version.json",
        )
        self.assertEqual(
            url,
            "https://api.gitcode.com/api/v5/repos/gcw_mHRylKw0/biaoshu-master/contents/skill-version.json?ref=main",
        )

    def test_remote_version_falls_back_to_gitcode(self) -> None:
        github_url = "https://api.github.com/repos/example/repo/contents/skill-version.json?ref=main"
        gitcode_url = "https://api.gitcode.com/api/v5/repos/example/repo/contents/skill-version.json?ref=main"

        def fake_fetch(url: str):
            if "github.com" in url:
                return None, "network_unreachable"
            return {"version": "0.1.8"}, None

        with patch.object(updater, "_fetch_json", side_effect=fake_fetch):
            version, url, errors = updater._fetch_remote_version([github_url, gitcode_url])

        self.assertEqual(version, "0.1.8")
        self.assertEqual(url, gitcode_url)
        self.assertEqual(errors, [{"url": github_url, "reason": "network_unreachable"}])


if __name__ == "__main__":
    unittest.main()
