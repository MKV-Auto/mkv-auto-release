"""
Tests for gather_final_outputs with cached_hashes parameter.
Tests the hash caching functionality without requiring actual disc ripping.
"""
import pytest
from pathlib import Path
from unittest.mock import Mock, MagicMock
from sqlalchemy.orm import Session
import uuid

from core.transfer.validation import calculate_file_hash
from core.utils import hash_file


@pytest.fixture
def sample_files(tmp_path):
    """Create sample MKV files for testing."""
    files = {}
    for i in range(3):
        filename = f"title_{i+1:03d}.mkv"
        filepath = tmp_path / filename
        content = b"fake mkv content " * (100 + i * 50)
        filepath.write_bytes(content)
        files[filename] = filepath
    
    return files


@pytest.fixture
def mock_disc_titles(sample_files):
    """Create mock DiscTitle objects with title_id mappings."""
    titles = []
    title_ids = []
    for i, filename in enumerate(sample_files.keys()):
        title_id = str(uuid.uuid4())
        title_ids.append(title_id)
        title = Mock()
        title.id = title_id
        title.source_file = f"0010{i}.mpls"  # Mock source_file
        title.comment = filename  # Output filename matches
        titles.append(title)
    
    return titles, title_ids


class TestGatherFinalOutputsCachedHashes:
    """Test gather_final_outputs with cached_hashes parameter."""
    
    def test_gather_final_outputs_uses_cached_hashes(self, sample_files, tmp_path, mock_disc_titles):
        """Test that cached hashes are used instead of recalculating."""
        from workers.tasks import gather_final_outputs
        from sqlalchemy.orm import Session
        
        disc_titles, title_ids = mock_disc_titles
        
        # Build title_id -> filename mapping
        title_id_to_filename = {}
        for i, (title, title_id) in enumerate(zip(disc_titles, title_ids)):
            filename = list(sample_files.keys())[i]
            title_id_to_filename[title_id] = filename
        
        # Calculate hashes upfront using title_id keys
        cached_hashes = {}
        ripped_files = {}
        for i, (filename, filepath) in enumerate(sample_files.items()):
            title_id = title_ids[i]
            cached_hashes[title_id] = calculate_file_hash(filepath)
            ripped_files[title_id] = filename  # Relative path is just filename
        
        # Mock database session
        db = Mock(spec=Session)
        mock_query = Mock()
        mock_query.filter.return_value.all.return_value = disc_titles
        db.query.return_value = mock_query
        
        # Mock the hash_file function to detect if it's called
        hash_call_count = [0]
        
        def mock_hash_file(path, hash_type="sha256", progress_cb=None, **kwargs):
            hash_call_count[0] += 1
            return hash_file(path, hash_type=hash_type)
        
        # Import and patch hash_file in the tasks module
        import workers.tasks
        original_hash_file = workers.tasks.hash_file
        
        try:
            workers.tasks.hash_file = mock_hash_file
            
            # Call gather_final_outputs with cached_hashes and disc_id/db
            paths_result, hashes_result = gather_final_outputs(
                tmp_path,
                final_paths=ripped_files,
                cached_hashes=cached_hashes,
                disc_id="test-disc-123",
                db=db
            )
            
            # Verify cached hashes were used (hash_file should not be called)
            assert hash_call_count[0] == 0, "hash_file should not be called when cached_hashes provided"
            
            # Verify results match cached hashes (both use title_id keys)
            assert paths_result == ripped_files
            assert hashes_result == cached_hashes
            # Verify keys are title_ids (UUIDs)
            for key in paths_result.keys():
                assert len(key) == 36 and '-' in key, f"Expected UUID format, got {key}"
            
        finally:
            workers.tasks.hash_file = original_hash_file
    
    def test_gather_final_outputs_calculates_when_no_cache(self, sample_files, tmp_path, mock_disc_titles):
        """Test that hashes are calculated when cached_hashes is not provided."""
        from workers.tasks import gather_final_outputs
        from sqlalchemy.orm import Session
        
        disc_titles, title_ids = mock_disc_titles
        
        # Build ripped_files with title_id keys
        ripped_files = {}
        for i, filename in enumerate(sample_files.keys()):
            title_id = title_ids[i]
            ripped_files[title_id] = filename
        
        # Mock database session
        db = Mock(spec=Session)
        mock_query = Mock()
        mock_query.filter.return_value.all.return_value = disc_titles
        db.query.return_value = mock_query
        
        # Call without cached_hashes but with disc_id/db
        paths_result, hashes_result = gather_final_outputs(
            tmp_path,
            final_paths=ripped_files,
            cached_hashes=None,
            disc_id="test-disc-123",
            db=db
        )
        
        # Verify hashes were calculated
        assert len(hashes_result) == len(sample_files)
        
        # Verify hashes are correct (using title_id keys)
        for i, (filename, filepath) in enumerate(sample_files.items()):
            title_id = title_ids[i]
            expected_hash = calculate_file_hash(filepath)
            assert hashes_result[title_id] == expected_hash
            assert title_id in paths_result
    
    def test_gather_final_outputs_partial_cache(self, sample_files, tmp_path, mock_disc_titles):
        """Test that partial cache works - calculates missing hashes."""
        from workers.tasks import gather_final_outputs
        from sqlalchemy.orm import Session
        
        disc_titles, title_ids = mock_disc_titles
        
        # Cache only first file's hash (using title_id key)
        cached_hashes = {}
        first_filename = list(sample_files.keys())[0]
        first_filepath = sample_files[first_filename]
        first_title_id = title_ids[0]
        cached_hashes[first_title_id] = calculate_file_hash(first_filepath)
        
        # Build ripped_files with title_id keys
        ripped_files = {}
        for i, filename in enumerate(sample_files.keys()):
            title_id = title_ids[i]
            ripped_files[title_id] = filename
        
        # Mock database session
        db = Mock(spec=Session)
        mock_query = Mock()
        mock_query.filter.return_value.all.return_value = disc_titles
        db.query.return_value = mock_query
        
        # Mock hash_file to count calls
        hash_call_count = [0]
        
        def mock_hash_file(path, hash_type="sha256", progress_cb=None, **kwargs):
            hash_call_count[0] += 1
            return hash_file(path, hash_type=hash_type)
        
        import workers.tasks
        original_hash_file = workers.tasks.hash_file
        
        try:
            workers.tasks.hash_file = mock_hash_file
            
            # Call with partial cache
            paths_result, hashes_result = gather_final_outputs(
                tmp_path,
                final_paths=ripped_files,
                cached_hashes=cached_hashes,
                disc_id="test-disc-123",
                db=db
            )
            
            # Should calculate hashes for uncached files only
            expected_calls = len(sample_files) - 1  # All except the cached one
            assert hash_call_count[0] == expected_calls, \
                f"Expected {expected_calls} hash calculations, got {hash_call_count[0]}"
            
            # Verify cached hash is used
            assert hashes_result[first_title_id] == cached_hashes[first_title_id]
            
            # Verify all files have hashes
            assert len(hashes_result) == len(sample_files)
            
            # Verify calculated hashes are correct
            for i, (filename, filepath) in enumerate(sample_files.items()):
                title_id = title_ids[i]
                if title_id != first_title_id:
                    expected_hash = calculate_file_hash(filepath)
                    assert hashes_result[title_id] == expected_hash
            
        finally:
            workers.tasks.hash_file = original_hash_file
    
    def test_gather_final_outputs_method_uses_cached_hashes(self, sample_files, tmp_path, mock_disc_titles):
        """Test that JobTask.gather_final_outputs method also uses cached_hashes."""
        from workers.tasks import JobTask
        from unittest.mock import Mock
        from sqlalchemy.orm import Session
        
        disc_titles, title_ids = mock_disc_titles
        
        # Create a mock JobTask instance
        task = Mock(spec=JobTask)
        task.gather_final_outputs = JobTask.gather_final_outputs.__get__(task, JobTask)
        
        # Calculate hashes upfront using title_id keys
        cached_hashes = {}
        ripped_files = {}
        for i, (filename, filepath) in enumerate(sample_files.items()):
            title_id = title_ids[i]
            cached_hashes[title_id] = calculate_file_hash(filepath)
            ripped_files[title_id] = filename
        
        # Mock database session
        db = Mock(spec=Session)
        mock_query = Mock()
        mock_query.filter.return_value.all.return_value = disc_titles
        db.query.return_value = mock_query
        
        # Mock hash_file
        hash_call_count = [0]
        
        def mock_hash_file(path, hash_type="sha256"):
            hash_call_count[0] += 1
            return hash_file(path, hash_type=hash_type)
        
        import workers.tasks
        original_hash_file = workers.tasks.hash_file
        
        try:
            workers.tasks.hash_file = mock_hash_file
            
            # Call method with cached_hashes and disc_id/db
            paths_result, hashes_result = task.gather_final_outputs(
                tmp_path,
                final_paths=ripped_files,
                cached_hashes=cached_hashes,
                disc_id="test-disc-123",
                db=db
            )
            
            # Verify cached hashes were used
            assert hash_call_count[0] == 0
            assert hashes_result == cached_hashes
            # Verify keys are title_ids
            for key in paths_result.keys():
                assert len(key) == 36 and '-' in key, f"Expected UUID format, got {key}"
            
        finally:
            workers.tasks.hash_file = original_hash_file

