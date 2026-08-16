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
            ("Linux", "x86_64", "_ubuntu22_x86_64.7z"),
        )

    @patch.object(sys, "platform", "win32")
    @patch("platform.machine", return_value="AMD64")
    def test_windows_x64_selector(self, _machine):
        self.assertEqual(
            teamtalk_sdk._platform_selector(),
            ("windows", "x64", "_win64.7z"),
        )

    @patch.object(sys, "platform", "darwin")
    @patch("platform.machine", return_value="arm64")
    def test_macos_selector(self, _machine):
        self.assertEqual(
            teamtalk_sdk._platform_selector(),
            ("macOS", "universal", "_macos_universal.7z"),
        )

    def test_archive_traversal_is_rejected(self):
        with self.assertRaises(teamtalk_sdk.TeamTalkSDKError):
            teamtalk_sdk._safe_archive_names(["Library/TeamTalkPy/TeamTalk5.py", "../escape"])

    def test_archive_normal_paths_are_accepted(self):
        teamtalk_sdk._safe_archive_names(["Library/TeamTalkPy/TeamTalk5.py"])


if __name__ == "__main__":
    unittest.main()
