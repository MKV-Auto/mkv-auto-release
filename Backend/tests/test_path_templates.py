"""
Tests for path template resolution system.
"""
import pytest
from core.path_templates import (
    resolve_template,
    validate_template,
    get_available_variables,
    PATH_TEMPLATE_SCHEMA_VERSION,
)


def test_path_template_schema_version_defined():
    """Path template schema version is defined for traceability."""
    assert PATH_TEMPLATE_SCHEMA_VERSION is not None
    assert isinstance(PATH_TEMPLATE_SCHEMA_VERSION, str)
    assert len(PATH_TEMPLATE_SCHEMA_VERSION) >= 1


def test_get_available_variables():
    """Test that available variables are returned."""
    vars = get_available_variables()
    assert "movie_name" in vars
    assert "year" in vars
    assert "release_name" in vars
    assert "disc_number" in vars


def test_validate_template_valid():
    """Test validation of valid templates."""
    valid, error = validate_template("{movie_name} ({year})")
    assert valid
    assert error == ""

    valid, error = validate_template("{movie_name}/{release_name}")
    assert valid
    assert error == ""


def test_validate_template_invalid():
    """Test validation of invalid templates."""
    valid, error = validate_template("{movie_name} ({unknown_var})")
    assert not valid
    assert "unknown" in error.lower()

    valid, error = validate_template("{movie_name} ({year}")
    assert not valid
    assert "braces" in error.lower()


def test_resolve_template_simple():
    """Test simple template resolution."""
    context = {
        "movie_name": "Test Movie",
        "year": 2024,
    }
    result = resolve_template("{movie_name} ({year})", context)
    assert result == "Test Movie (2024)"


def test_resolve_template_with_release():
    """Test template with release name."""
    context = {
        "movie_name": "Test Movie",
        "year": 2024,
        "release_name": "Collectors Edition",
    }
    result = resolve_template("{movie_name} ({year})/{release_name}", context)
    assert result == "Test Movie (2024)/Collectors Edition"


def test_resolve_template_disc_number():
    """Test template with disc number formatting."""
    context = {
        "movie_name": "Test Movie",
        "year": 2024,
        "disc_number": 1,
    }
    result = resolve_template("{movie_name} ({year})/disc{disc_number}", context)
    assert result == "Test Movie (2024)/disc01"

    context["disc_number"] = 12
    result = resolve_template("{movie_name} ({year})/disc{disc_number}", context)
    assert result == "Test Movie (2024)/disc12"


def test_resolve_template_movie_year_alias():
    """Test that movie_year and year are both supported."""
    context = {
        "movie_name": "Test Movie",
        "movie_year": 2024,
    }
    result = resolve_template("{movie_name} ({year})", context)
    assert result == "Test Movie (2024)"


def test_resolve_template_missing_variables():
    """Test that missing variables are replaced with empty string."""
    context = {
        "movie_name": "Test Movie",
    }
    result = resolve_template("{movie_name} ({year})/{release_name}", context)
    assert result == "Test Movie ()/"


def test_resolve_template_sanitization():
    """Test that invalid filesystem characters are sanitized."""
    context = {
        "movie_name": "Test/Movie\\Name",
        "year": 2024,
    }
    result = resolve_template("{movie_name} ({year})", context)
    assert "/" not in result
    assert "\\" not in result


def test_resolve_template_empty():
    """Test empty template."""
    result = resolve_template("", {})
    assert result == ""

    result = resolve_template("", {"movie_name": "Test"})
    assert result == ""











