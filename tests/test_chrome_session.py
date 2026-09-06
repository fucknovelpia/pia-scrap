import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from requests.cookies import create_cookie

from src.chrome_session import (
    _chrome_user_data_dir,
    find_chrome_binary,
    list_chrome_profiles,
    load_chrome_novelpia_session,
)


class ChromeProfileTests(unittest.TestCase):
    def test_windows_profiles_use_local_app_data(self):
        with (
            patch("src.chrome_session.sys.platform", "win32"),
            patch.dict(os.environ, {"LOCALAPPDATA": "C:/Users/Test/AppData/Local"}),
        ):
            self.assertEqual(
                _chrome_user_data_dir(),
                Path("C:/Users/Test/AppData/Local/Google/Chrome/User Data"),
            )

    def test_macos_and_linux_profiles_use_platform_locations(self):
        home = Path("/test-home")
        with patch("src.chrome_session.Path.home", return_value=home):
            with patch("src.chrome_session.sys.platform", "darwin"):
                self.assertEqual(
                    _chrome_user_data_dir(), home / "Library/Application Support/Google/Chrome"
                )
            with (
                patch("src.chrome_session.sys.platform", "linux"),
                patch.dict(os.environ, {"XDG_CONFIG_HOME": "/custom-config"}),
            ):
                self.assertEqual(_chrome_user_data_dir(), Path("/custom-config/google-chrome"))

    def test_discovers_both_modern_and_legacy_profile_databases(self):
        with tempfile.TemporaryDirectory() as folder:
            chrome_dir = Path(folder)
            for name in ("Default/Network/Cookies", "Profile 1/Cookies", "Profile 2/Preferences"):
                path = chrome_dir / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            with patch("src.chrome_session.CHROME_DIR", chrome_dir):
                self.assertEqual(list_chrome_profiles(), ["Default", "Profile 1"])

    def test_windows_binary_can_be_installed_in_program_files(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "Google/Chrome/Application/chrome.exe"
            binary.parent.mkdir(parents=True)
            binary.touch()
            with (
                patch("src.chrome_session.sys.platform", "win32"),
                patch.dict(os.environ, {"PROGRAMFILES": folder}, clear=True),
                patch("src.chrome_session.shutil.which", return_value=None),
            ):
                self.assertEqual(find_chrome_binary(), str(binary))


class ChromeSessionTests(unittest.TestCase):
    def test_modern_database_uses_matching_key_file_without_fabricating_access_token(self):
        with tempfile.TemporaryDirectory() as folder:
            chrome_dir = Path(folder)
            modern = chrome_dir / "Default/Network/Cookies"
            modern.parent.mkdir(parents=True)
            modern.touch()
            (chrome_dir / "Default/Cookies").touch()
            (chrome_dir / "Local State").touch()
            jar = [
                create_cookie("LOGINKEY", "login-cookie", domain=".novelpia.com"),
                create_cookie("USERKEY", "user-cookie", domain=".novelpia.com"),
                create_cookie("TKEY", "refresh-cookie", domain=".novelpia.com"),
                create_cookie("USERKEY", "wrong-domain", domain="notnovelpia.com"),
                create_cookie("TKEY", "expired-cookie", domain=".novelpia.com", expires=1),
            ]
            with (
                patch("src.chrome_session.CHROME_DIR", chrome_dir),
                patch("src.chrome_session.browser_cookie3.chrome", return_value=jar) as read,
            ):
                session = load_chrome_novelpia_session("Default")
            read.assert_called_once_with(
                cookie_file=str(modern),
                domain_name="novelpia.com",
                key_file=str(chrome_dir / "Local State"),
            )
            self.assertIsNone(session.login_at)
            self.assertEqual(session.login_key, "login-cookie")
            self.assertEqual(session.userkey, "user-cookie")
            self.assertEqual(session.tkey, "refresh-cookie")

    def test_legacy_database_is_still_supported(self):
        with tempfile.TemporaryDirectory() as folder:
            chrome_dir = Path(folder)
            legacy = chrome_dir / "Default/Cookies"
            legacy.parent.mkdir()
            legacy.touch()
            with (
                patch("src.chrome_session.CHROME_DIR", chrome_dir),
                patch("src.chrome_session.browser_cookie3.chrome", return_value=[]) as read,
            ):
                load_chrome_novelpia_session("Default")
            self.assertEqual(read.call_args.kwargs["cookie_file"], str(legacy))

    def test_encrypted_or_locked_database_explains_how_to_continue(self):
        with tempfile.TemporaryDirectory() as folder:
            chrome_dir = Path(folder)
            cookie_file = chrome_dir / "Default/Network/Cookies"
            cookie_file.parent.mkdir(parents=True)
            cookie_file.touch()
            with (
                patch("src.chrome_session.CHROME_DIR", chrome_dir),
                patch(
                    "src.chrome_session.browser_cookie3.chrome",
                    side_effect=RuntimeError("Unable to get key for cookie decryption"),
                ),
                self.assertRaisesRegex(RuntimeError, "Close Chrome completely.*login window"),
            ):
                load_chrome_novelpia_session("Default")


if __name__ == "__main__":
    unittest.main()
