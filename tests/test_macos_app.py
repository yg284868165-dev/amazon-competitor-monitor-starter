import plistlib
import stat
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "Amazon竞品监控.app"


def test_macos_app_bundle_has_launcher_icon_and_desktop_permission_text():
    with (APP / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)

    assert info["CFBundleIdentifier"] == "com.local.amazon-competitor-monitor"
    assert info["CFBundleExecutable"] == "launcher"
    assert info["CFBundleIconFile"] == "AppIcon"
    assert info["NSDesktopFolderUsageDescription"]
    assert (APP / "Contents/Resources/AppIcon.icns").is_file()

    launcher = APP / "Contents/MacOS/launcher"
    assert launcher.is_file()
    assert launcher.stat().st_mode & stat.S_IXUSR
    assert launcher.read_bytes()[:4] == b"\xca\xfe\xba\xbe"


def test_source_app_icon_is_square_1024_png():
    payload = (ROOT / "assets/app-icon.png").read_bytes()
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    width = int.from_bytes(payload[16:20], "big")
    height = int.from_bytes(payload[20:24], "big")
    assert (width, height) == (1024, 1024)
