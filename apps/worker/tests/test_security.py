"""Path jailing, filename normalisation and SSRF guards."""

from __future__ import annotations


import pytest

from clipforge.errors import ValidationFailed
from clipforge.security.paths import assert_within, display_name, relative_display, safe_join
from clipforge.security.urls import (
    is_private_address,
    validate_url_shape,
)


def test_display_name_strips_path_separators_and_control_characters():
    # given / when
    cleaned = display_name("../../etc/pass\x00wd\n.mp4")

    # then
    assert "/" not in cleaned
    assert "\\" not in cleaned
    assert "\x00" not in cleaned
    assert "\n" not in cleaned


def test_display_name_escapes_windows_reserved_names():
    # given / when
    cleaned = display_name("CON.mp4")

    # then
    assert cleaned == "_CON.mp4"


def test_display_name_falls_back_when_nothing_usable_remains():
    # given / when / then
    assert display_name("...", fallback="Untitled") == "Untitled"
    assert display_name(None) == "Untitled"


def test_display_name_is_capped_to_a_sane_length():
    # given / when
    cleaned = display_name("a" * 500 + ".mp4")

    # then
    assert len(cleaned) <= 180


def test_safe_join_builds_a_path_inside_the_root(tmp_path):
    # given / when
    joined = safe_join(tmp_path, "proj_1", "source.mp4")

    # then
    assert joined == (tmp_path / "proj_1" / "source.mp4").resolve()


@pytest.mark.parametrize("segment", ["..", ".", "", "a/b", "a\\b", "a\x00b"])
def test_safe_join_rejects_unsafe_segments(tmp_path, segment):
    # given / when / then
    with pytest.raises(ValidationFailed):
        safe_join(tmp_path, segment)


def test_assert_within_rejects_a_sibling_directory(tmp_path):
    # given
    root = tmp_path / "projects" / "alpha"
    root.mkdir(parents=True)
    sibling = tmp_path / "projects" / "beta" / "secret.mp4"
    sibling.parent.mkdir(parents=True)
    sibling.write_bytes(b"x")

    # when / then
    with pytest.raises(ValidationFailed):
        assert_within(root, sibling)


def test_assert_within_accepts_the_root_itself(tmp_path):
    # given / when / then
    assert assert_within(tmp_path, tmp_path) == tmp_path.resolve()


def test_relative_display_never_leaks_absolute_paths(tmp_path):
    # given
    root = tmp_path / "data"
    target = root / "projects" / "p1" / "source.mp4"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")

    # when
    label = relative_display(target, root)

    # then
    assert label == "projects/p1/source.mp4"
    assert str(tmp_path) not in label


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.10", "172.16.4.2", "::1", "fc00::1", "224.0.0.1"],
)
def test_private_and_local_addresses_are_blocked(address):
    # given / when / then
    assert is_private_address(address) is True


@pytest.mark.parametrize("address", ["8.8.8.8", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_unicast_addresses_are_allowed(address):
    # given / when / then
    assert is_private_address(address) is False


def test_unparseable_addresses_are_treated_as_private():
    # given / when / then
    assert is_private_address("not-an-address") is True


def test_validate_url_shape_accepts_a_known_provider():
    # given / when
    validated = validate_url_shape("https://www.youtube.com/watch?v=abc123")

    # then
    assert validated.host == "www.youtube.com"
    assert validated.is_known_provider is True


def test_validate_url_shape_accepts_an_unknown_public_host():
    # given / when
    validated = validate_url_shape("https://example.com/talk.mp4")

    # then
    assert validated.is_known_provider is False


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/video.mp4",
        "javascript:alert(1)",
        "file:///C:/Windows/System32/drivers/etc/hosts",
        "http://localhost:80/video.mp4",
        "http://127.0.0.1/video.mp4",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://printer.local/video.mp4",
        "https://user:pass@example.com/video.mp4",
        "https://example.com:22/video.mp4",
        "https://",
        "https://example.com/" + "a" * 3000,
    ],
)
def test_validate_url_shape_rejects_dangerous_urls(url):
    # given / when / then
    with pytest.raises(ValidationFailed):
        validate_url_shape(url)


def test_validate_url_shape_rejects_blank_input():
    # given / when / then
    with pytest.raises(ValidationFailed, match="Enter a video URL"):
        validate_url_shape("   ")
