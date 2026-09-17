"""Regression checks for the release site's platform marks."""

from pathlib import Path

import pytest

SITE_ROOT = Path(__file__).parents[1] / "site"
SITE_INDEX = SITE_ROOT / "index.html"
SITE_STYLES = SITE_ROOT / "styles.css"


@pytest.mark.parametrize(
    ("platform", "icon"),
    [("windows", "windows"), ("macos", "apple"), ("linux", "linux")],
)
def test_download_card_uses_a_vendored_platform_icon(platform: str, icon: str) -> None:
    """Each download card should render its canonical vendored brand asset."""
    html = SITE_INDEX.read_text(encoding="utf-8")
    card_start = html.index(f'data-os="{platform}"')
    card_end = html.index("</div>", html.index("</div>", card_start) + 1)
    card = html[card_start:card_end]

    assert '<div class="os-mark" aria-hidden="true">' in card
    assert f'src="assets/platform-icons/{icon}.svg"' in card
    assert (SITE_ROOT / "assets" / "platform-icons" / f"{icon}.svg").is_file()
    assert "<svg" not in card
    assert not any(glyph in card for glyph in ("⊞", "●", "⌁"))


def test_detected_platform_badge_uses_an_opaque_background() -> None:
    """The selected-card border must not remain visible through the badge."""
    styles = SITE_STYLES.read_text(encoding="utf-8")
    selector = ".home-v2 .os-card.is-detected .os-badge"
    rule_start = styles.index(selector)
    rule_end = styles.index("}", rule_start)
    rule = styles[rule_start:rule_end]

    assert "background: #103536" in rule
