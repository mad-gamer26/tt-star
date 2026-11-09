import sys
import unittest
from unittest.mock import patch

import teamtalk_sdk


class SDKLinkParserTests(unittest.TestCase):
    def test_only_collects_standard_sdk_archives(self):
        parser = teamtalk_sdk._SDKLinkParser()
        parser.feed(
            '<a href="https://www.bearware.dk/teamtalksdk/v5.22a/'
            'tt5sdk_v5.22a_win64.7z">x64</a>'
            '<a href="https://www.bearware.dk/teamtalksdk/v5.22a/'
            'tt5prosdk_v5.22a_win64.7z">Pro</a>'
        )
        self.assertEqual(len(parser.links), 1)
        self.assertIn("tt5sdk_v5.22a_win64.7z", parser.links[0])

    @patch.object(sys, "platform", "linux")
    @patch("platform.machine", return_value="x86_64")
    def test_linux_x64_selector(self, _machine):
        self.assertEqual(
            teamtalk_sdk._platform_selector(),
            ("Linux", "x86_64", r"_ubuntu(?P<ubuntu>\d+)_x86_64\.7z"),
        )

    @patch.object(sys, "platform", "win32")
    @patch("platform.machine", return_value="AMD64")
    def test_windows_x64_selector(self, _machine):
        self.assertEqual(
            teamtalk_sdk._platform_selector(),
            ("windows", "x64", r"_win64\.7z"),
        )

    @patch.object(sys, "platform", "darwin")
    @patch("platform.machine", return_value="arm64")
    def test_macos_selector(self, _machine):
        self.assertEqual(
            teamtalk_sdk._platform_selector(),
            ("macOS", "universal", r"_macos_universal\.7z"),
        )

    def test_archive_traversal_is_rejected(self):
        with self.assertRaises(teamtalk_sdk.TeamTalkSDKError):
            teamtalk_sdk._safe_archive_names(["Library/TeamTalkPy/TeamTalk5.py", "../escape"])

    def test_archive_normal_paths_are_accepted(self):
        teamtalk_sdk._safe_archive_names(["Library/TeamTalkPy/TeamTalk5.py"])


INDEX = "https://www.bearware.dk/teamtalksdk/"
LISTINGS = {
    INDEX: [INDEX + "v5.9a/", INDEX + "v5.19a/", INDEX + "v5.22a/", INDEX + "v5.23a/", INDEX + "License.txt"],
    INDEX + "v5.23a/": [INDEX + "v5.23a/" + n for n in (
        "tt5prosdk_v5.23a_ubuntu26_x86_64.7z", "tt5sdk_v5.23a_ubuntu26_x86_64.7z", "tt5sdk_v5.23a_win64.7z")],
    INDEX + "v5.22a/": [INDEX + "v5.22a/" + n for n in (
        "tt5sdk_v5.22a_ubuntu22_x86_64.7z", "tt5sdk_v5.22a_win64.7z")],
    INDEX + "v5.19a/": [INDEX + "v5.19a/" + n for n in (
        "tt5sdk_v5.19a_ubuntu20_x86_64.7z", "tt5sdk_v5.19a_ubuntu22_x86_64.7z")],
    INDEX + "v5.9a/": [INDEX + "v5.9a/tt5sdk_v5.9a_ubuntu18_x86_64.7z"],
}


@patch.object(sys, "platform", "linux")
@patch("platform.machine", return_value="x86_64")
@patch.object(teamtalk_sdk, "_fetch_links", side_effect=lambda url, timeout: LISTINGS.get(url, []))
class DiscoveryTests(unittest.TestCase):
    def test_skips_builds_too_new_for_this_glibc(self, *_):
        with patch.object(teamtalk_sdk, "_host_glibc", return_value=(2, 35)):
            found = teamtalk_sdk.find_downloads()
        self.assertEqual([d.version for d in found], ["5.22a", "5.19a", "5.9a"])
        self.assertTrue(found[0].url.endswith("tt5sdk_v5.22a_ubuntu22_x86_64.7z"))
        self.assertTrue(found[1].url.endswith("_ubuntu22_x86_64.7z"), "prefers the newest compatible Ubuntu build")

    def test_newest_build_on_a_new_system(self, *_):
        with patch.object(teamtalk_sdk, "_host_glibc", return_value=(2, 43)):
            found = teamtalk_sdk.discover_latest_download()
        self.assertEqual(found.version, "5.23a")
        self.assertIn("/tt5sdk_v5.23a_ubuntu26_x86_64.7z", found.url)

    def test_never_picks_the_pro_sdk(self, *_):
        with patch.object(teamtalk_sdk, "_host_glibc", return_value=(2, 43)):
            self.assertNotIn("prosdk", teamtalk_sdk.discover_latest_download().url)

    def test_versions_sort_numerically(self, *_):
        self.assertGreater(teamtalk_sdk._version_key("5.23a"), teamtalk_sdk._version_key("5.9a"))

    def test_clear_error_when_nothing_runs(self, *_):
        with patch.object(teamtalk_sdk, "_host_glibc", return_value=(2, 17)), \
                self.assertRaises(teamtalk_sdk.TeamTalkSDKError) as ctx:
            teamtalk_sdk.find_downloads()
        self.assertIn("too new for this system", str(ctx.exception))


class CompatibilityTests(unittest.TestCase):
    def test_ubuntu_build_glibc(self):
        runs = teamtalk_sdk._ubuntu_build_runs_here
        self.assertTrue(runs(22, (2, 35)))
        self.assertFalse(runs(26, (2, 35)))
        self.assertTrue(runs(26, (2, 43)))
        self.assertFalse(runs(28, (2, 43)), "unlisted future releases are estimated")
        with patch.object(teamtalk_sdk, "_host_glibc", return_value=None):
            self.assertTrue(runs(26), "non-glibc systems are left to the test load")

    def test_rejects_other_hosts(self):
        with self.assertRaises(teamtalk_sdk.TeamTalkSDKError):
            teamtalk_sdk._check_url("https://example.com/tt5sdk_v5.23a_win64.7z")
        with self.assertRaises(teamtalk_sdk.TeamTalkSDKError):
            teamtalk_sdk._check_url("http://www.bearware.dk/teamtalksdk/v5.23a/tt5sdk_v5.23a_win64.7z")

    @unittest.skipIf(sys.platform == "win32", "uses a Unix shared-library loader")
    def test_test_load_reports_a_broken_library(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".so") as fake:
            fake.write(b"not a library")
            fake.flush()
            self.assertTrue(teamtalk_sdk._library_loads(teamtalk_sdk.Path(fake.name)))

    def test_relative_listing_names_are_recognised(self):
        parser = teamtalk_sdk._SDKLinkParser()
        parser.feed('<a href="tt5sdk_v5.23a_win64.7z">a</a><a href="tt5prosdk_v5.23a_win64.7z">b</a>')
        self.assertEqual(parser.links, ["tt5sdk_v5.23a_win64.7z"])


if __name__ == "__main__":
    unittest.main()
