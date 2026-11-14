"""Unit tests for text formatting and grammatical pluralization helpers."""

from src.common.text import pluralize


def test_pluralize_automatic_rules():
    assert pluralize(1, "runner") == "runner"
    assert pluralize(2, "runner") == "runners"
    assert pluralize(0, "runner") == "runners"

    assert pluralize(1, "organization") == "organization"
    assert pluralize(5, "organization") == "organizations"

    assert pluralize(1, "repository") == "repository"
    assert pluralize(2, "repository") == "repositories"

    assert pluralize(1, "branch") == "branch"
    assert pluralize(2, "branch") == "branches"

    assert pluralize(1, "box") == "box"
    assert pluralize(3, "box") == "boxes"


def test_pluralize_explicit_plural():
    assert pluralize(1, "repository", "repositories") == "repository"
    assert pluralize(10, "repository", "repositories") == "repositories"


def test_pluralize_with_include_count():
    assert pluralize(1, "runner", include_count=True) == "1 runner"
    assert pluralize(3, "runner", include_count=True) == "3 runners"
    assert pluralize(1, "repository", include_count=True) == "1 repository"
    assert pluralize(718, "repository", include_count=True) == "718 repositories"
    assert pluralize(1000, "organization", include_count=True) == "1,000 organizations"
