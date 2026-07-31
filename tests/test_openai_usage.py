from __future__ import annotations

from synapse.integrations.openai_usage import (
    CodexUsageService,
    CodexUsageSnapshot,
    ResetCredits,
    UsageWindow,
    format_reset_remaining,
    format_usage_label,
    parse_reset_credits_details,
    parse_usage_payload,
    usage_style,
)


def test_usage_service_hides_label_without_oauth_profile(monkeypatch) -> None:
    service = CodexUsageService(settings=object())
    monkeypatch.setattr(service, "has_oauth_profile", lambda: False)

    assert service.label == ""


def test_usage_service_shows_label_with_oauth_profile(monkeypatch) -> None:
    service = CodexUsageService(settings=object())
    monkeypatch.setattr(service, "has_oauth_profile", lambda: True)

    assert service.label.plain == "codex n/a"


def test_parse_codex_usage_windows() -> None:
    snapshot = parse_usage_payload(
        {
            "rate_limit": {
                "primary_window": {
                    "used_percent": 18,
                    "limit_window_seconds": 18_000,
                    "reset_at": 1_700_000_600,
                },
                "secondary_window": {
                    "used_percent": 62,
                    "limit_window_seconds": 86_400,
                    "reset_at": 1_700_003_600,
                },
            }
        },
        expires_at=1_700_010_000,
        captured_at=1_700_000_000,
    )
    assert snapshot.primary is not None
    assert snapshot.primary.remaining_percent == 82
    assert snapshot.primary.window_minutes == 300
    assert snapshot.secondary is not None
    assert snapshot.secondary.remaining_percent == 38
    assert snapshot.lowest_remaining_percent == 38
    assert snapshot.reset_credits is None


def test_usage_label_contains_remaining_windows_reset_and_expiry() -> None:
    snapshot = CodexUsageSnapshot(
        primary=UsageWindow(used_percent=18, window_minutes=300, reset_at=1_700_000_600),
        secondary=UsageWindow(used_percent=62, window_minutes=1_440, reset_at=1_700_003_600),
        expires_at=1_700_010_000,
    )
    label = format_usage_label(snapshot)
    assert label.startswith("5h 82%/")
    assert "1d 38%/" in label
    assert "exp " in label


def test_label_includes_reset_credits_count() -> None:
    snapshot = CodexUsageSnapshot(
        primary=UsageWindow(used_percent=80, window_minutes=None, reset_at=None),
        reset_credits=ResetCredits(available_count=3),
    )
    label = format_usage_label(snapshot)
    assert "resets 3" in label


def test_label_skips_zero_resets() -> None:
    snapshot = CodexUsageSnapshot(
        primary=UsageWindow(used_percent=80, window_minutes=None, reset_at=None),
        reset_credits=ResetCredits(available_count=0),
    )
    label = format_usage_label(snapshot)
    assert "resets" not in label


def test_low_remaining_usage_is_red() -> None:
    low = CodexUsageSnapshot(
        primary=UsageWindow(used_percent=51, window_minutes=None, reset_at=None)
    )
    high = CodexUsageSnapshot(
        primary=UsageWindow(used_percent=50, window_minutes=None, reset_at=None)
    )
    assert usage_style(low) == "#f28b82"
    assert usage_style(high) == "#8ab4f8"


def test_format_reset_remaining_is_compact() -> None:
    assert format_reset_remaining(960, now=0) == "16m"
    assert format_reset_remaining(7_200, now=0) == "2h"
    assert format_reset_remaining(None, now=0) == "--"


def test_parse_reset_credits_from_usage_payload() -> None:
    snapshot = parse_usage_payload(
        {
            "rate_limit": {
                "primary_window": {
                    "used_percent": 10,
                    "limit_window_seconds": 18_000,
                    "reset_at": 1_700_000_600,
                },
                "secondary_window": None,
            },
            "rate_limit_reset_credits": {
                "available_count": 2,
                "credits": [
                    {
                        "id": "rc_1",
                        "resetType": "codexRateLimits",
                        "status": "available",
                        "grantedAt": 1781654400,
                        "expiresAt": 1784246400,
                        "title": "Full reset",
                        "description": "Ready",
                    }
                ],
            },
        }
    )
    assert snapshot.reset_credits is not None
    assert snapshot.reset_credits.available_count == 2
    assert len(snapshot.reset_credits.credits) == 1
    assert snapshot.reset_credits.credits[0].id == "rc_1"
    assert snapshot.reset_credits.credits[0].status == "available"
    label = format_usage_label(snapshot)
    assert "resets 2" in label


def test_parse_reset_credits_details_from_dedicated_endpoint() -> None:
    details = parse_reset_credits_details(
        {
            "available_count": 3,
            "credits": [
                {
                    "id": "rc_a",
                    "reset_type": "codexRateLimits",
                    "status": "available",
                    "granted_at": "2026-07-01T00:00:00Z",
                    "expires_at": "2026-08-01T00:00:00Z",
                },
                {
                    "id": "rc_b",
                    "reset_type": "unknown",
                    "status": "redeemed",
                },
            ],
        }
    )
    assert details.available_count == 3
    assert len(details.credits) == 2
    assert details.credits[0].title is None
    assert details.credits[0].granted_at is not None
    assert details.credits[0].expires_at is not None
    assert details.credits[0].expires_at > details.credits[0].granted_at