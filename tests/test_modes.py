"""Tests for mode profiles."""

from modes import list_modes, load_mode_profile


def test_list_modes():
    """Test that list_modes returns available modes."""
    modes = list(list_modes())
    assert len(modes) > 0
    assert "general" in modes


def test_load_general_mode():
    """Test loading the general mode profile."""
    mode = load_mode_profile("general")
    assert mode["name"] == "general"
    assert "description" in mode
    assert "reliability_method" in mode
    assert mode["reliability_method"] in ["simple", "stage", "window", "window_fec"]
    assert "timeout" in mode
    assert mode["timeout"] > 0


def test_mode_profile_structure():
    """Test that mode profiles have expected structure."""
    mode = load_mode_profile("general")
    
    # Required fields
    assert "name" in mode
    assert "description" in mode
    assert "reliability_method" in mode
    
    # Transport settings
    assert "transport" in mode
    assert isinstance(mode["transport"], dict)
    
    # Timeout settings
    if "timeout" in mode:
        assert isinstance(mode["timeout"], (int, float))
        assert mode["timeout"] > 0
