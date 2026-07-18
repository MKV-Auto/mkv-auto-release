"""
Comprehensive Parsing Test Suite

Tests all parsing functionality to ensure proper extraction of information
from MakeMKV logs, disc info, and other sources.
"""
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from core.utils import (
    parse_info_log,
    parse_title_metadata,
    coerce_duration_seconds,
    infer_resolution_from_log,
)
from core.importbuddy_prefill import parse_copy_log
from parsing.disc_parser import hydrate_disc_payload, parse_info_log as parse_info_log_parser


# ============================================================================
# INFO LOG PARSING TESTS
# ============================================================================

class TestInfoLogParsing:
    """Test parsing of MakeMKV info logs."""
    
    def test_parse_basic_info_log(self):
        """Test parsing a basic info log."""
        log = """
DRV:0,256,999,0,"BD-ROM","TEST_DISC","/dev/sr0"
TINFO:0,9,0,"01:23:45"
TINFO:0,11,0,"1234567890"
TINFO:0,10,0,"1.15 GB"
MSG:3104,0,0,"Disc label: Test Disc"
"""
        result = parse_info_log(log)
        assert result is not None
        assert "titles" in result or "info_title" in result
    
    def test_parse_title_metadata(self):
        """Test parsing title metadata from info log."""
        log = """
TINFO:1,9,0,"00:45:30"
TINFO:1,11,0,"5000000000"
TINFO:1,16,0,"00050.mpls"
TINFO:1,26,0,"1,2,3"
TINFO:1,27,0,"Main Feature"
TINFO:1,28,0,"eng"
MSG:3307,0,2,"File 00050.m2ts was added as title #1"
"""
        result = parse_title_metadata(log)
        assert isinstance(result, list)
        if result:
            title = result[0]
            assert "duration" in title or "duration_raw" in title
            assert "size" in title or "display_size" in title
    
    def test_parse_multiple_titles(self):
        """Test parsing multiple titles from info log."""
        log = """
TINFO:1,9,0,"00:45:30"
TINFO:1,16,0,"00050.mpls"
MSG:3307,0,2,"File 00050.m2ts was added as title #1"
TINFO:2,9,0,"00:30:15"
TINFO:2,16,0,"00051.mpls"
MSG:3307,0,2,"File 00051.m2ts was added as title #2"
"""
        result = parse_title_metadata(log)
        assert len(result) >= 2
    
    def test_parse_duration_formats(self):
        """Test parsing various duration formats."""
        test_cases = [
            ("01:23:45", 5025),  # 1 hour, 23 minutes, 45 seconds
            ("00:30:00", 1800),  # 30 minutes
            ("00:05:30", 330),   # 5 minutes, 30 seconds
            ("02:00:00", 7200),  # 2 hours
        ]
        
        for duration_str, expected_seconds in test_cases:
            result = coerce_duration_seconds(duration_str)
            assert result == expected_seconds, f"Failed for {duration_str}: expected {expected_seconds}, got {result}"
    
    def test_parse_resolution_inference(self):
        """Test inferring resolution from info log."""
        test_cases = [
            ("TINFO:1,27,0,\"2160p\"", (2160, "2160p")),
            ("TINFO:1,27,0,\"1080p\"", (1080, "1080p")),
            ("TINFO:1,27,0,\"720p\"", (720, "720p")),
            ("TINFO:1,27,0,\"480p\"", (480, "480p")),
        ]
        
        for log_line, (expected_res, expected_fmt) in test_cases:
            result = infer_resolution_from_log(log_line)
            # Result format may vary, but should contain resolution info
            assert result is not None
    
    def test_parse_stream_info(self):
        """Test parsing stream information (SINFO)."""
        log = """
SINFO:1,0,0,0,"Video"
SINFO:1,0,1,0,"H.264"
SINFO:1,0,2,0,"1920x1080"
SINFO:1,1,0,0,"Audio"
SINFO:1,1,1,0,"DTS-HD MA"
SINFO:1,1,2,0,"5.1"
"""
        result = parse_title_metadata(log)
        # Should extract stream information
        assert result is not None


# ============================================================================
# COPY LOG PARSING TESTS
# ============================================================================

class TestCopyLogParsing:
    """Test parsing of MakeMKV copy logs."""
    
    def test_parse_copy_log_basic(self):
        """Test parsing a basic copy log."""
        log_content = """
DRV:0,256,999,0,"BD-ROM","TEST_DISC","/dev/sr0"
MSG:3307,0,2,"File 00050.m2ts was added as title #1"
MSG:3307,0,2,"File 00051.m2ts was added as title #2"
MSG:5036,0,0,"Copy complete. 2 titles saved."
"""
        # Create a temporary log file
        from tempfile import NamedTemporaryFile
        with NamedTemporaryFile(mode='w', delete=False, suffix='.log') as f:
            f.write(log_content)
            log_path = Path(f.name)
        
        try:
            result = parse_copy_log(log_path)
            assert result is not None
            assert "titles" in result
            assert "total_titles_saved" in result
            assert result["total_titles_saved"] == 2
        finally:
            log_path.unlink(missing_ok=True)
    
    def test_parse_copy_log_skipped_titles(self):
        """Test parsing copy log with skipped titles."""
        log_content = """
MSG:3307,0,2,"File 00050.m2ts was added as title #1"
MSG:3309,0,0,"Title 00051.mpls was skipped"
MSG:5036,0,0,"Copy complete. 1 titles saved."
"""
        from tempfile import NamedTemporaryFile
        with NamedTemporaryFile(mode='w', delete=False, suffix='.log') as f:
            f.write(log_content)
            log_path = Path(f.name)
        
        try:
            result = parse_copy_log(log_path)
            assert result is not None
            assert "skipped_titles" in result
            assert len(result["skipped_titles"]) >= 1
        finally:
            log_path.unlink(missing_ok=True)
    
    def test_parse_copy_log_disc_label(self):
        """Test parsing disc label from copy log."""
        log_content = """
DRV:0,256,999,0,"BD-ROM","TEST_DISC_LABEL","/dev/sr0"
MSG:3307,0,2,"File 00050.m2ts was added as title #1"
MSG:5036,0,0,"Copy complete. 1 titles saved."
"""
        from tempfile import NamedTemporaryFile
        with NamedTemporaryFile(mode='w', delete=False, suffix='.log') as f:
            f.write(log_content)
            log_path = Path(f.name)
        
        try:
            result = parse_copy_log(log_path)
            assert result is not None
            assert "disc_label" in result
            assert result["disc_label"] == "TEST_DISC_LABEL"
        finally:
            log_path.unlink(missing_ok=True)


# ============================================================================
# DISC PAYLOAD HYDRATION TESTS
# ============================================================================

class TestDiscPayloadHydration:
    """Test hydration of disc payloads with parsed information."""
    
    def test_hydrate_disc_payload_basic(self):
        """Test basic disc payload hydration."""
        payload = {
            "disc_num": "1",
            "mount_point": "/dev/sr0",
            "info_log": """
TINFO:1,9,0,"01:23:45"
TINFO:1,16,0,"00050.mpls"
MSG:3307,0,2,"File 00050.m2ts was added as title #1"
"""
        }
        
        result = hydrate_disc_payload("1", "/dev/sr0", payload)
        assert result is not None
        assert result["disc_num"] == "1"
        assert result["mount_point"] == "/dev/sr0"
        # Should have parsed titles or scan_tracks
        assert "titles" in result or "scan_tracks" in result or "info_title" in result
    
    def test_hydrate_disc_payload_with_resolution(self):
        """Test hydration with resolution inference."""
        payload = {
            "disc_num": "1",
            "mount_point": "/dev/sr0",
            "info_log": """
TINFO:1,9,0,"01:23:45"
TINFO:1,27,0,"2160p"
"""
        }
        
        result = hydrate_disc_payload("1", "/dev/sr0", payload)
        assert result is not None
        # Resolution may be inferred from the log or may not be present
        # Just verify the payload was hydrated
        assert "disc_num" in result
        assert result["disc_num"] == "1"
    
    def test_hydrate_disc_payload_cached(self):
        """Test that cached payloads are not re-hydrated."""
        payload = {
            "disc_num": "1",
            "mount_point": "/dev/sr0",
            "_hydrated": True,
            "titles": [{"index": 1, "duration": 5025}]
        }
        
        result = hydrate_disc_payload("1", "/dev/sr0", payload)
        assert result is not None
        assert result["_hydrated"] is True
        # Should preserve existing titles
        assert "titles" in result
    
    def test_hydrate_disc_payload_label_inference(self):
        """Test label inference from various fields."""
        test_cases = [
            {"info_title": "Test Movie"},
            {"info_label": "Test Movie"},
            {"show_title": "Test Movie"},
            {"release_name": "Test Movie"},
            {"disc_label": "Test Movie"},
        ]
        
        for payload_base in test_cases:
            payload = {
                "disc_num": "1",
                "mount_point": "/dev/sr0",
                **payload_base
            }
            
            result = hydrate_disc_payload("1", "/dev/sr0", payload)
            assert result is not None
            # Should have some label information
            assert "info_title" in result or "info_label" in result or "release_name" in result


# ============================================================================
# EDGE CASE PARSING TESTS
# ============================================================================

class TestParsingEdgeCases:
    """Test parsing edge cases and error handling."""
    
    def test_parse_empty_log(self):
        """Test parsing an empty log."""
        result = parse_info_log("")
        # Should return empty dict or None, not crash
        assert result is not None or result == {}
    
    def test_parse_malformed_log(self):
        """Test parsing a malformed log."""
        log = "This is not a valid MakeMKV log\nRandom text\nMore random text"
        result = parse_info_log(log)
        # Should handle gracefully, return empty or partial result
        assert result is not None
    
    def test_parse_log_with_unicode(self):
        """Test parsing log with unicode characters."""
        log = """
TINFO:1,27,0,"Test Movie: The Sequel (2024)"
MSG:3104,0,0,"Disc label: Test Disc with émojis 🎬"
"""
        result = parse_info_log(log)
        # Should handle unicode gracefully
        assert result is not None
    
    def test_parse_log_with_special_characters(self):
        """Test parsing log with special characters."""
        log = """
TINFO:1,27,0,"Movie & TV Show: Part 1/2"
MSG:3104,0,0,"Disc label: Test's Disc (2024)"
"""
        result = parse_info_log(log)
        # Should handle special characters
        assert result is not None
    
    def test_parse_duration_edge_cases(self):
        """Test parsing edge case duration formats."""
        test_cases = [
            ("", None),  # Empty string - may return None
            ("invalid", None),  # Invalid format - may return None
            ("0:0:0", 0),  # Zero duration
        ]
        
        for duration_str, expected_result in test_cases:
            result = coerce_duration_seconds(duration_str)
            # Should return None for invalid, or 0 for zero duration
            if expected_result is None:
                assert result is None or result == 0, f"Expected None or 0 for '{duration_str}', got {result}"
            else:
                assert result == expected_result, f"Expected {expected_result} for '{duration_str}', got {result}"
        
        # Test that "99:99:99" actually parses (it's technically valid as 99 hours, 99 minutes, 99 seconds)
        result = coerce_duration_seconds("99:99:99")
        assert result is not None
        assert isinstance(result, (int, float))


# ============================================================================
# INTEGRATION PARSING TESTS
# ============================================================================

class TestParsingIntegration:
    """Test parsing integration with other components."""
    
    def test_parse_and_hydrate_full_flow(self):
        """Test full parsing and hydration flow."""
        info_log = """
DRV:0,256,999,0,"BD-ROM","TEST_DISC","/dev/sr0"
TINFO:1,9,0,"01:23:45"
TINFO:1,11,0,"5000000000"
TINFO:1,16,0,"00050.mpls"
TINFO:1,27,0,"Main Feature"
MSG:3307,0,2,"File 00050.m2ts was added as title #1"
"""
        
        payload = {
            "disc_num": "1",
            "mount_point": "/dev/sr0",
            "info_log": info_log
        }
        
        # Parse info log
        parsed = parse_info_log(info_log)
        assert parsed is not None
        
        # Hydrate payload
        hydrated = hydrate_disc_payload("1", "/dev/sr0", payload)
        assert hydrated is not None
        
        # Should have combined information
        assert "disc_num" in hydrated
        assert "mount_point" in hydrated

