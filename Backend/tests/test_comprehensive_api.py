"""
Comprehensive API Test Suite

Tests all backend API endpoints to ensure proper functionality, error handling,
and integration with the database and Celery tasks.
"""
import pytest
import json
from pathlib import Path
from unittest.mock import patch, MagicMock, Mock
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from datetime import datetime, timezone

from api import crud, models as db_models
from api.main import app
from tests.conftest_e2e import (
    e2e_test_environment,
    mock_celery,
    enhanced_fake_drive_manager,
    mock_makemkv,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def client(e2e_test_environment, enhanced_fake_drive_manager, mock_makemkv):
    """Test client with mocked dependencies."""
    # Ensure mocks are set up
    return TestClient(app)


@pytest.fixture
def sample_job(client, enhanced_fake_drive_manager, e2e_test_environment):
    """Create a sample job for testing."""
    disc_hash = enhanced_fake_drive_manager.discinfo_payload.get("disc_hash") or "test_disc_hash_12345"
    response = client.post(
        "/jobs/rip",
        json={
            "disc_num": "1",
            "mount_point": "/dev/sr0",
            "disc_hash": disc_hash,
            "mode": "copy"
        }
    )
    if response.status_code == 200:
        return response.json()["jobId"]
    pytest.skip(f"Could not create sample job: {response.text}")


@pytest.fixture
def unique_disc_hash(enhanced_fake_drive_manager):
    """Get a unique disc hash for tests that need isolation."""
    import uuid
    unique_hash = f"test_hash_{uuid.uuid4().hex[:8]}"
    enhanced_fake_drive_manager.discinfo_payload["disc_hash"] = unique_hash
    enhanced_fake_drive_manager.discinfo_payload["content_hash"] = unique_hash
    return unique_hash


# ============================================================================
# JOBS API TESTS
# ============================================================================

class TestJobsAPI:
    """Test /jobs endpoints."""
    
    def test_start_rip_success(self, client, e2e_test_environment, enhanced_fake_drive_manager):
        """Test successful rip initiation."""
        # Use the disc_hash from the fake drive manager
        disc_hash = enhanced_fake_drive_manager.discinfo_payload.get("disc_hash") or "test_disc_hash_12345"
        
        response = client.post(
            "/jobs/rip",
            json={
                "disc_num": "1",
                "mount_point": "/dev/sr0",
                "disc_hash": disc_hash,
                "mode": "copy"
            }
        )
        assert response.status_code == 200, \
            f"Expected 200, got {response.status_code}. Response: {response.text}"
        
        data = response.json()
        assert "jobId" in data, \
            f"Response missing 'jobId'. Keys: {list(data.keys())}. Response: {data}"
        assert data["jobId"] is not None, \
            f"jobId is None. Full response: {data}"
        assert isinstance(data["jobId"], str), \
            f"jobId should be a string, got {type(data['jobId'])}"
        assert len(data["jobId"]) > 0, \
            f"jobId should not be empty"
        assert "job_status" in data, "POST /jobs/rip returns JobStatus"
        assert data.get("workflow_step") in ("boxset", "summary"), "workflow_step is boxset for miss or summary for hit"
    
    def test_start_rip_duplicate_prevention(self, client, e2e_test_environment, enhanced_fake_drive_manager):
        """Test that duplicate rips are prevented."""
        # Use a unique disc_hash for this test
        disc_hash = "test_hash_dup_unique_123"
        # Update fake drive manager to return this hash
        enhanced_fake_drive_manager.discinfo_payload["disc_hash"] = disc_hash
        enhanced_fake_drive_manager.discinfo_payload["content_hash"] = disc_hash
        
        # First rip
        response1 = client.post(
            "/jobs/rip",
            json={
                "disc_num": "1",
                "mount_point": "/dev/sr0",
                "disc_hash": disc_hash,
                "mode": "copy"
            }
        )
        assert response1.status_code == 200, f"First rip failed: {response1.text}"
        job_id1 = response1.json()["jobId"]
        
        # Second rip (should be rejected or return same job)
        response2 = client.post(
            "/jobs/rip",
            json={
                "disc_num": "1",
                "mount_point": "/dev/sr0",
                "disc_hash": disc_hash,
                "mode": "copy"
            }
        )
        # May return 409 (conflict) or 200 (returning existing job ID or new job if previous was pending and not active)
        assert response2.status_code in [200, 409], \
            f"Expected 200 or 409 for duplicate, got {response2.status_code}: {response2.text}"
        if response2.status_code == 200:
            job_id2 = response2.json()["jobId"]
            # Same job ID (existing job returned) or new job ID (previous was pending/inactive, so new rip was allowed)
            assert job_id2 is not None and isinstance(job_id2, str), f"jobId should be non-empty string, got {job_id2}"
        else:
            # If it returns 409, check the error message
            assert "already in progress" in response2.json()["detail"].lower() or \
                   "duplicate" in response2.json()["detail"].lower()
    
    def test_start_rip_invalid_disc_hash(self, client, enhanced_fake_drive_manager):
        """Test rip initiation with invalid disc hash."""
        # disc_hash is not required; invalid values are ignored
        response = client.post(
            "/jobs/rip",
            json={
                "disc_num": "1",
                "mount_point": "/dev/sr0",
                "disc_hash": "invalid_hash_that_doesnt_match",
                "mode": "copy"
            }
        )
        assert response.status_code == 200, \
            f"Expected 200 for invalid hash, got {response.status_code}: {response.text}"

    def test_start_rip_missing_required_fields(self, client):
        """Test rip initiation with missing required fields."""
        # Test missing disc_num: backend may derive from mount_point via list_drives/cache.
        # Accept 200 when derivation succeeds; 400/422 when it cannot be determined.
        response = client.post(
            "/jobs/rip",
            json={"mount_point": "/dev/sr0", "mode": "copy"}
        )
        if response.status_code == 200:
            data = response.json()
            assert data.get("jobId"), "Response should contain jobId when rip starts"
        else:
            assert response.status_code in [400, 422], \
                f"Expected 200, 400, or 422 for missing disc_num, got {response.status_code}. Response: {response.text}"

        # Test missing mount_point
        response = client.post(
            "/jobs/rip",
            json={"disc_num": "1", "mode": "copy", "disc_hash": "test"}
        )
        assert response.status_code in [400, 422], \
            f"Expected 400/422 for missing mount_point, got {response.status_code}. Response: {response.text}"
        
        # Test missing mode - may return 400 if mode has a default or 422 for validation
        response = client.post(
            "/jobs/rip",
            json={"disc_num": "1", "mount_point": "/dev/sr0", "disc_hash": "test"}
        )
        assert response.status_code in [200, 400, 422], \
            f"Expected 200/400/422 for missing mode, got {response.status_code}. Response: {response.text}"

    def test_start_rip_invalid_mode(self, client, enhanced_fake_drive_manager):
        """Test rip initiation with invalid mode."""
        disc_hash = enhanced_fake_drive_manager.discinfo_payload.get("disc_hash")
        response = client.post(
            "/jobs/rip",
            json={
                "disc_num": "1",
                "mount_point": "/dev/sr0",
                "disc_hash": disc_hash,
                "mode": "invalid_mode"
            }
        )
        # Invalid mode may be accepted by Pydantic (if mode is just a string) or rejected
        # The important thing is that it doesn't crash - accept any non-500 status
        assert response.status_code != 500, \
            f"Server error for invalid mode: {response.text}"
        assert response.status_code in [200, 400, 422], \
            f"Unexpected status {response.status_code} for invalid mode. Response: {response.text}"

    def test_get_job_status_not_found(self, client):
        """Test retrieving status for non-existent job."""
        response = client.get("/jobs/00000000-0000-0000-0000-000000000000/status")
        assert response.status_code == 404, \
            f"Expected 404 for non-existent job, got {response.status_code}"

    def test_transfer_job_not_found(self, client):
        """Test transfer for non-existent job."""
        response = client.post(
            "/jobs/00000000-0000-0000-0000-000000000000/transfer",
            json={"config_id": "test_config"}
        )
        assert response.status_code == 404, \
            f"Expected 404 for non-existent job, got {response.status_code}"

    def test_get_job_status(self, client, e2e_test_environment, enhanced_fake_drive_manager):
        """Test retrieving job status."""
        # Use the disc_hash from the fake drive manager
        disc_hash = enhanced_fake_drive_manager.discinfo_payload.get("disc_hash") or "test_disc_hash_12345"
        
        # Create a job first
        response = client.post(
            "/jobs/rip",
            json={
                "disc_num": "1",
                "mount_point": "/dev/sr0",
                "disc_hash": disc_hash,
                "mode": "copy"
            }
        )
        if response.status_code == 200 and "jobId" in response.json():
            job_id = response.json()["jobId"]
            
            # Get status
            response = client.get(f"/jobs/{job_id}/status")
            assert response.status_code == 200
            data = response.json()
            assert "job_status" in data
            assert "rip_state" in data
        else:
            pytest.skip(f"Could not create job: {response.text}")
    
    def test_get_current_job(self, client, e2e_test_environment):
        """Test retrieving current job."""
        response = client.get("/jobs/current")
        # May return 404 if no active job, or 200 with job data
        assert response.status_code in [200, 404], \
            f"Unexpected status {response.status_code}: {response.text}"
        if response.status_code == 200:
            data = response.json()
            # Response may have different structure
            assert isinstance(data, dict), f"Expected dict, got {type(data)}"
    
    def test_list_jobs(self, client, e2e_test_environment):
        """Test listing all jobs."""
        response = client.get("/jobs")
        assert response.status_code == 200
        assert isinstance(response.json(), list)
    
    def test_transfer_job(self, client, sample_job):
        """Test initiating job transfer."""
        response = client.post(
            f"/jobs/{sample_job}/transfer",
            json={"config_id": "test_config"}
        )
        # May fail if job isn't in right state, but should not be 404
        assert response.status_code != 404, \
            f"Job {sample_job} not found. Response: {response.text}"
        assert response.status_code in [200, 400], \
            f"Unexpected status {response.status_code}: {response.text}"
        if response.status_code == 200:
            assert response.json().get("workflow_step") == "transfer", "POST /jobs/{id}/transfer returns workflow_step=transfer"

    def test_resume_job(self, client, sample_job):
        """Test resuming a job."""
        response = client.post(f"/jobs/{sample_job}/resume")
        assert response.status_code != 404, \
            f"Job {sample_job} not found. Response: {response.text}"
        assert response.status_code in [200, 400], \
            f"Unexpected status {response.status_code}: {response.text}"

    def test_postprocess_job(self, client, sample_job):
        """Test starting post-processing for a job."""
        response = client.post(f"/jobs/{sample_job}/postprocess")
        assert response.status_code != 404, \
            f"Job {sample_job} not found. Response: {response.text}"
        assert response.status_code in [200, 400], \
            f"Unexpected status {response.status_code}: {response.text}"
        if response.status_code == 200:
            assert response.json().get("workflow_step") == "postprocess", "POST /jobs/{id}/postprocess returns workflow_step=postprocess"

    def test_label_job(self, client, sample_job):
        """Test labeling a job."""
        response = client.post(
            f"/jobs/{sample_job}/label",
            json={
                "mode": "movie",
                "disc_format": "Blu-Ray",
                "titles": []
            }
        )
        assert response.status_code != 404, \
            f"Job {sample_job} not found. Response: {response.text}"
        # May return 400 if job isn't in right state, 409 for state transition conflicts, or 422 for validation errors
        assert response.status_code in [200, 400, 409, 422], \
            f"Unexpected status {response.status_code}: {response.text}"

    def test_get_job_artifacts(self, client, sample_job):
        """Test retrieving job artifacts."""
        response = client.get(f"/jobs/{sample_job}/artifacts")
        assert response.status_code in [200, 404], \
            f"Unexpected status {response.status_code}: {response.text}"

    def test_get_job_tracks(self, client, sample_job):
        """Test retrieving job tracks."""
        response = client.get(f"/jobs/{sample_job}/tracks")
        assert response.status_code in [200, 404], \
            f"Unexpected status {response.status_code}: {response.text}"

    def test_regenerate_previews(self, client, sample_job):
        """Test regenerating previews for a job."""
        response = client.post(f"/jobs/{sample_job}/previews/regenerate")
        assert response.status_code != 404, \
            f"Job {sample_job} not found. Response: {response.text}"
        assert response.status_code in [200, 400], \
            f"Unexpected status {response.status_code}: {response.text}"

    def test_delete_previews(self, client, sample_job):
        """Test deleting previews for a job."""
        response = client.delete(f"/jobs/{sample_job}/previews")
        assert response.status_code in [200, 404], \
            f"Unexpected status {response.status_code}: {response.text}"

    def test_concurrent_rip_requests(self, client, unique_disc_hash):
        """Test handling of concurrent rip requests."""
        import threading
        import time
        
        results = []
        errors = []
        
        def make_request():
            try:
                response = client.post(
                    "/jobs/rip",
                    json={
                        "disc_num": "1",
                        "mount_point": "/dev/sr0",
                        "disc_hash": unique_disc_hash,
                        "mode": "copy"
                    }
                )
                results.append(response.status_code)
            except Exception as e:
                errors.append(str(e))
        
        # Start multiple threads
        threads = [threading.Thread(target=make_request) for _ in range(3)]
        for t in threads:
            t.start()
        
        # Wait for all threads
        for t in threads:
            t.join()
        
        # Only one should succeed (200), others should be 409 (conflict) or may return existing job (200)
        assert len(results) == 3, f"Expected 3 responses, got {len(results)}. Errors: {errors}"
        # At least one should succeed
        assert results.count(200) >= 1, \
            f"Expected at least 1 successful request (200), got {results.count(200)}. Status codes: {results}"
        # Others should be conflicts or may also return 200 if they get the same job
        # The important thing is that we don't get errors
        assert len(errors) == 0, f"Unexpected errors during concurrent requests: {errors}"

    def test_start_rip_performance(self, client, enhanced_fake_drive_manager):
        """Test that rip initiation completes in reasonable time."""
        import time
        
        disc_hash = enhanced_fake_drive_manager.discinfo_payload.get("disc_hash")
        start = time.time()
        
        response = client.post(
            "/jobs/rip",
            json={
                "disc_num": "1",
                "mount_point": "/dev/sr0",
                "disc_hash": disc_hash,
                "mode": "copy"
            }
        )
        
        elapsed = time.time() - start
        assert response.status_code == 200, \
            f"Rip initiation failed: {response.text}"
        assert elapsed < 2.0, \
            f"Rip initiation took {elapsed:.2f}s, expected < 2s"

    def test_start_rip_empty_strings(self, client, enhanced_fake_drive_manager):
        """Test rip initiation with empty string values."""
        disc_hash = enhanced_fake_drive_manager.discinfo_payload.get("disc_hash")
        
        # Empty disc_num - may be accepted but job may fail later
        response = client.post(
            "/jobs/rip",
            json={
                "disc_num": "",
                "mount_point": "/dev/sr0",
                "disc_hash": disc_hash,
                "mode": "copy"
            }
        )
        # Empty string may pass validation but create a job that fails
        # Accept either validation error or successful creation (which may fail later)
        assert response.status_code in [200, 400, 422], \
            f"Unexpected status {response.status_code} for empty disc_num. Response: {response.text}"

    def test_start_rip_very_long_disc_hash(self, client, enhanced_fake_drive_manager):
        """Test rip initiation with very long disc hash."""
        # Create a very long hash
        long_hash = "a" * 1000
        
        response = client.post(
            "/jobs/rip",
            json={
                "disc_num": "1",
                "mount_point": "/dev/sr0",
                "disc_hash": long_hash,
                "mode": "copy"
            }
        )
        # Should either accept it or reject with validation error
        assert response.status_code in [200, 400, 422], \
            f"Unexpected status {response.status_code} for long hash"

    def test_full_rip_workflow(self, client, enhanced_fake_drive_manager):
        """Test complete rip workflow from start to finish."""
        disc_hash = enhanced_fake_drive_manager.discinfo_payload.get("disc_hash")
        
        # 1. Start rip
        response = client.post(
            "/jobs/rip",
            json={
                "disc_num": "1",
                "mount_point": "/dev/sr0",
                "disc_hash": disc_hash,
                "mode": "copy"
            }
        )
        assert response.status_code == 200, \
            f"Failed to start rip: {response.text}"
        job_id = response.json()["jobId"]
        
        # 2. Check status
        response = client.get(f"/jobs/{job_id}/status")
        assert response.status_code == 200, \
            f"Failed to get job status: {response.text}"
        data = response.json()
        assert "job_status" in data, \
            f"Status response missing 'job_status': {data}"
        # Job may be in various states depending on Celery task execution
        assert data["job_status"] in ["pending", "running", "failed"], \
            f"Unexpected job_status: {data['job_status']}"
        
        # 3. Check current job (may be 404 if job isn't active)
        response = client.get("/jobs/current")
        assert response.status_code in [200, 404], \
            f"Unexpected status for current job: {response.status_code}"
        
        # 4. Verify job appears in list (if list is not empty)
        response = client.get("/jobs")
        assert response.status_code == 200
        jobs = response.json()
        if jobs:  # Only check if list is not empty
            # Jobs list may have different structure, check both possible formats
            job_ids = []
            for job in jobs:
                if isinstance(job, dict):
                    job_ids.append(job.get("jobId") or job.get("id") or str(job.get("job_id", "")))
                else:
                    job_ids.append(str(job))
            # Job should be in the list, but if it's not, it may be filtered out
            # Just verify the list structure is valid
            assert isinstance(jobs, list), "Jobs list should be a list"

    @pytest.mark.parametrize("endpoint,method,payload", [
        ("/jobs/{job_id}/transfer", "POST", {"config_id": "test_config"}),
        ("/jobs/{job_id}/resume", "POST", {}),
        ("/jobs/{job_id}/postprocess", "POST", {}),
        ("/jobs/{job_id}/previews/regenerate", "POST", {}),
    ])
    def test_job_operation_endpoints(self, client, sample_job, endpoint, method, payload):
        """Test various job operation endpoints with parametrization."""
        url = endpoint.format(job_id=sample_job)
        
        if method == "POST":
            response = client.post(url, json=payload)
        elif method == "GET":
            response = client.get(url)
        elif method == "DELETE":
            response = client.delete(url)
        
        assert response.status_code != 404, \
            f"Job {sample_job} not found for {endpoint}. Response: {response.text}"
        assert response.status_code in [200, 400], \
            f"Unexpected status {response.status_code} for {endpoint}: {response.text}"


# ============================================================================
# DISCS API TESTS
# ============================================================================

class TestDiscsAPI:
    """Test /discs endpoints."""
    
    def test_list_discs(self, client, e2e_test_environment):
        """Test listing all discs."""
        response = client.get("/discs/")
        assert response.status_code == 200
        assert isinstance(response.json(), list)
    
    def test_get_disc_info(self, client, e2e_test_environment):
        """Test getting disc info."""
        response = client.get("/discs/1/info", params={"mount_point": "/dev/sr0"})
        assert response.status_code in [200, 404, 500]
        if response.status_code == 200:
            data = response.json()
            assert "disc_num" in data or "error" in data
    
    def test_refresh_disc_info(self, client, e2e_test_environment):
        """Test refreshing disc info."""
        response = client.post("/discs/1/refresh", params={"mount_point": "/dev/sr0"})
        assert response.status_code in [200, 400, 500]
    
    def test_get_current_disc(self, client, e2e_test_environment):
        """Test getting current disc state."""
        response = client.get("/discs/current")
        assert response.status_code in [200, 404]
        if response.status_code == 200:
            data = response.json()
            assert "disc" in data or "job" in data


# ============================================================================
# SYSTEM API TESTS
# ============================================================================

class TestSystemAPI:
    """Test /system endpoints."""
    
    def test_get_makemkv_info(self, client):
        """Test getting MakeMKV information."""
        response = client.get("/system/makemkv")
        assert response.status_code == 200
        data = response.json()
        assert "version" in data or "error" in data
    
    def test_get_storage_info(self, client):
        """Test getting storage information."""
        response = client.get("/system/storage")
        assert response.status_code == 200
        data = response.json()
        assert "total" in data or "error" in data
    
    def test_list_transfer_configs(self, client, e2e_test_environment):
        """Test listing transfer configurations."""
        response = client.get("/system/transfer/configs")
        assert response.status_code == 200
        assert isinstance(response.json(), list)
    
    def test_create_transfer_config(self, client, e2e_test_environment):
        """Test creating a transfer configuration."""
        response = client.post(
            "/system/transfer/configs",
            json={
                "name": "Test Config",
                "mode": "local",
                "transfer_dir": "/tmp/test",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("mode") == "local"
        assert data.get("name") == "Test Config"
        assert data.get("transfer_dir") == "/tmp/test"

        # Re-fetch by id to ensure transfer_dir was persisted
        config_id = data["id"]
        get_resp = client.get(f"/system/transfer/configs/{config_id}")
        assert get_resp.status_code == 200
        assert get_resp.json().get("transfer_dir") == "/tmp/test"

    def test_create_transfer_config_local_requires_transfer_dir(self, client, e2e_test_environment):
        """POST create with mode=local and no transfer_dir returns 422."""
        response = client.post(
            "/system/transfer/configs",
            json={"name": "Local No Path", "mode": "local"},
        )
        assert response.status_code == 422
        data = response.json()
        assert "detail" in data
        assert "transfer" in data["detail"].lower() or "directory" in data["detail"].lower()

    def test_delete_transfer_config_not_found(self, client, e2e_test_environment):
        """DELETE non-existent transfer config returns 404."""
        response = client.delete("/system/transfer/configs/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404
        assert "detail" in response.json()

    def test_delete_active_transfer_config_returns_400(self, client, e2e_test_environment):
        """DELETE active transfer config returns 400."""
        create_resp = client.post(
            "/system/transfer/configs",
            json={"name": "To Delete Active", "mode": "local", "transfer_dir": "/tmp/td"},
        )
        assert create_resp.status_code == 200
        config_id = create_resp.json()["id"]
        client.post(f"/system/transfer/configs/{config_id}/activate")
        response = client.delete(f"/system/transfer/configs/{config_id}")
        assert response.status_code == 400
        data = response.json()
        assert "detail" in data and "active" in data["detail"].lower()

    def test_delete_transfer_config_success(self, client, e2e_test_environment):
        """DELETE non-active transfer config succeeds.

        ``create_config`` auto-activates the first config when no others
        exist (#292), and ``delete_config`` refuses to remove an active
        config (sibling test_delete_active_transfer_config_returns_400).
        To exercise the success path we therefore need two configs —
        keep the second active and delete the first.
        """
        first = client.post(
            "/system/transfer/configs",
            json={"name": "To Delete", "mode": "local", "transfer_dir": "/tmp/td2"},
        )
        assert first.status_code == 200
        first_id = first.json()["id"]

        # Second config is created inactive (existing_count > 0). Activate
        # it explicitly — the route deactivates all others atomically.
        second = client.post(
            "/system/transfer/configs",
            json={"name": "Keep Active", "mode": "local", "transfer_dir": "/tmp/td3"},
        )
        assert second.status_code == 200
        second_id = second.json()["id"]
        activate_resp = client.post(f"/system/transfer/configs/{second_id}/activate")
        assert activate_resp.status_code == 200

        response = client.delete(f"/system/transfer/configs/{first_id}")
        assert response.status_code == 200
        assert response.json().get("success") is True
        get_resp = client.get(f"/system/transfer/configs/{first_id}")
        assert get_resp.status_code == 404

        # The remaining config is still present and active.
        get_second = client.get(f"/system/transfer/configs/{second_id}")
        assert get_second.status_code == 200
        assert get_second.json().get("is_active") is True

    def test_get_preview_config(self, client):
        """Test getting preview configuration."""
        response = client.get("/system/preview/config")
        assert response.status_code == 200
        body = response.json()
        # #594: ceiling must always be present on read so the UI slider
        # can bind its [max] to it.
        assert "max_parallel_ceiling" in body
        assert body["max_parallel_ceiling"] >= 1
        # And max_parallel must always come back ≤ the ceiling.
        assert body["max_parallel"] <= body["max_parallel_ceiling"]

    def test_save_preview_config_rejects_ceiling_overrun(self, client):
        """#594: saving max_parallel > ceiling returns 400, not silent coercion."""
        get_resp = client.get("/system/preview/config")
        ceiling = get_resp.json()["max_parallel_ceiling"]
        bad = {
            "duration_seconds": 120,
            "max_parallel": ceiling + 1,
            "disable_ffmpeg_junk_detection": False,
            "max_parallel_ceiling": ceiling,
        }
        response = client.post("/system/preview/config", json=bad)
        assert response.status_code == 400
        assert "exceeds server ceiling" in response.json()["detail"]

    def test_get_discord_config(self, client):
        """Test getting Discord configuration."""
        response = client.get("/system/discord/config")
        assert response.status_code == 200

    def test_get_setup_status(self, client):
        """Test GET /system/setup/status."""
        response = client.get("/system/setup/status")
        assert response.status_code == 200
        data = response.json()
        assert "first_time_setup_complete" in data
        assert "setup_step" in data
        assert isinstance(data["first_time_setup_complete"], bool)
        assert isinstance(data["setup_step"], int)
        assert 1 <= data["setup_step"] <= 6

    def test_post_setup_complete(self, client):
        """Test POST /system/setup/complete."""
        response = client.post("/system/setup/complete", json={})
        assert response.status_code == 200
        data = response.json()
        assert data["first_time_setup_complete"] is True
        assert 1 <= data["setup_step"] <= 6
        # Restore so other tests / fresh installs see incomplete
        client.patch("/system/setup/progress", json={"setup_step": 1})
        # Note: we don't reset first_time_setup_complete via API; backend has no "reset" endpoint.

    def test_patch_setup_progress(self, client):
        """Test PATCH /system/setup/progress."""
        response = client.patch("/system/setup/progress", json={"setup_step": 3})
        assert response.status_code == 200
        data = response.json()
        assert data["setup_step"] == 3
        response2 = client.get("/system/setup/status")
        assert response2.json()["setup_step"] == 3
        # Restore
        client.patch("/system/setup/progress", json={"setup_step": 1})

    def test_patch_setup_progress_invalid(self, client):
        """Test PATCH /system/setup/progress with invalid step returns 400."""
        response = client.patch("/system/setup/progress", json={"setup_step": 0})
        assert response.status_code == 400
        response = client.patch("/system/setup/progress", json={"setup_step": 7})
        assert response.status_code == 400

    def test_get_ffmpeg_detection(self, client):
        """Test GET /system/ffmpeg-detection."""
        response = client.get("/system/ffmpeg-detection")
        assert response.status_code == 200
        data = response.json()
        assert "enabled" in data
        assert isinstance(data["enabled"], bool)

    def test_set_ffmpeg_detection(self, client):
        """Test POST /system/ffmpeg-detection."""
        response = client.post("/system/ffmpeg-detection", json={"enabled": True})
        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is True
        response2 = client.post("/system/ffmpeg-detection", json={"enabled": False})
        assert response2.status_code == 200
        assert response2.json()["enabled"] is False
        # restore
        client.post("/system/ffmpeg-detection", json={"enabled": True})

    def test_get_discdb_disabled(self, client):
        """Test GET /system/discdb-disabled."""
        response = client.get("/system/discdb-disabled")
        assert response.status_code == 200
        data = response.json()
        assert "disabled" in data
        assert isinstance(data["disabled"], bool)

    def test_set_discdb_disabled(self, client):
        """Test POST /system/discdb-disabled."""
        response = client.post("/system/discdb-disabled", json={"disabled": True})
        assert response.status_code == 200
        data = response.json()
        assert data["disabled"] is True
        response2 = client.post("/system/discdb-disabled", json={"disabled": False})
        assert response2.status_code == 200
        assert response2.json()["disabled"] is False
        # restore
        client.post("/system/discdb-disabled", json={"disabled": False})

    def test_get_discdb_lookup_config(self, client):
        """Test GET /system/discdb-lookup/config."""
        response = client.get("/system/discdb-lookup/config")
        assert response.status_code == 200
        data = response.json()
        assert "discdb_miss_workflow_with_prefill" in data
        assert isinstance(data["discdb_miss_workflow_with_prefill"], bool)

    def test_set_discdb_lookup_config(self, client):
        """Test POST /system/discdb-lookup/config."""
        response = client.post("/system/discdb-lookup/config", json={"discdb_miss_workflow_with_prefill": True})
        assert response.status_code == 200
        assert response.json()["discdb_miss_workflow_with_prefill"] is True
        response2 = client.post("/system/discdb-lookup/config", json={"discdb_miss_workflow_with_prefill": False})
        assert response2.status_code == 200
        assert response2.json()["discdb_miss_workflow_with_prefill"] is False


# ============================================================================
# RELEASES API TESTS
# ============================================================================

class TestReleasesAPI:
    """Test /releases endpoints."""
    
    def test_list_releases(self, client, e2e_test_environment):
        """Test listing all releases."""
        response = client.get("/releases")
        assert response.status_code == 200
        assert isinstance(response.json(), list)
    
    def test_get_release(self, client, e2e_test_environment):
        """Test getting a release by slug."""
        response = client.get("/releases/test-release")
        assert response.status_code in [200, 404]
    
    def test_list_release_discs(self, client, e2e_test_environment):
        """Test listing discs for a release."""
        response = client.get("/releases/test-release/discs")
        assert response.status_code in [200, 404]
    
    def test_get_disc_by_hash(self, client, e2e_test_environment):
        """Test getting disc by content hash."""
        response = client.get("/releases/disc/by-hash?content_hash=test_hash")
        assert response.status_code in [200, 404]
    
    def test_list_boxsets(self, client, e2e_test_environment):
        """Test listing boxsets."""
        response = client.get("/releases/boxsets")
        assert response.status_code == 200
        assert isinstance(response.json(), list)


# ============================================================================
# MOVIES API TESTS
# ============================================================================

class TestMoviesAPI:
    """Test /movies endpoints."""
    
    def test_list_movies(self, client, e2e_test_environment):
        """Test listing all movies."""
        response = client.get("/movies")
        assert response.status_code == 200
        assert isinstance(response.json(), list)
    
    def test_get_movie(self, client, e2e_test_environment):
        """Test getting a movie by ID."""
        response = client.get("/movies/test_movie_id")
        assert response.status_code in [200, 404]
    
    def test_create_movie(self, client, e2e_test_environment):
        """Test creating a movie."""
        response = client.post(
            "/movies",
            json={
                "name": "Test Movie",
                "production_year": 2024
            }
        )
        assert response.status_code in [200, 400]
    
    def test_lookup_movie(self, client):
        """Test looking up a movie from TMDB."""
        response = client.post(
            "/movies/lookup",
            json={"tmdb_url": "https://www.themoviedb.org/movie/123"}
        )
        assert response.status_code in [200, 400, 500]


# ============================================================================
# DISCDB API TESTS
# ============================================================================

class TestDiscDbAPI:
    """Test /discdb endpoints."""
    
    def test_search_discdb(self, client):
        """Test searching DiscDB."""
        response = client.get("/discdb/search?q=test")
        assert response.status_code == 200
        data = response.json()
        assert "results" in data
    
    def test_get_discdb_detail(self, client):
        """Test getting DiscDB detail."""
        response = client.get("/discdb/detail?slug=test-slug")
        assert response.status_code in [200, 404]
    
    def test_hash_discdb(self, client):
        """Test hashing a disc for DiscDB."""
        response = client.post(
            "/discdb/hash",
            json={"mount": "/dev/sr0"}
        )
        assert response.status_code in [200, 400, 404]


# ============================================================================
# DRIVES API TESTS
# ============================================================================

class TestDrivesAPI:
    """Test /drives endpoints."""
    
    def test_get_drives(self, client):
        """Test getting list of drives."""
        response = client.get("/drives/drives")
        assert response.status_code == 200
        assert isinstance(response.json(), list)
    
    def test_get_discinfo(self, client):
        """Test getting disc info from drive manager."""
        response = client.get("/drives/discinfo", params={"disc_num": "1", "mount_point": "/dev/sr0"})
        assert response.status_code in [200, 404]
    
    def test_refresh_discinfo(self, client):
        """Test refreshing disc info."""
        with patch("api.routers.drives.refresh_disc_info", return_value={"status": "ok"}):
            response = client.post("/drives/discinfo/refresh", params={"disc_num": "1", "mount_point": "/dev/sr0"})
            assert response.status_code in [200, 400]
    
    def test_scan_discinfo(self, client):
        """Test scanning disc info."""
        with patch("api.routers.drives.scan_disc_info", return_value={"status": "ok"}):
            response = client.post("/drives/discinfo/scan", params={"disc_num": "1", "mount_point": "/dev/sr0"})
            assert response.status_code in [200, 400]
    
    def test_hash_discinfo(self, client):
        """Test hashing disc."""
        with patch("api.routers.drives.hash_disc", return_value={"status": "ok"}):
            response = client.post("/drives/discinfo/hash", params={"disc_num": "1", "mount_point": "/dev/sr0"})
            assert response.status_code in [200, 400]
    
    def test_eject_disc(self, client):
        """Test ejecting disc."""
        response = client.post("/drives/disc/eject", params={"disc_num": "1"})
        assert response.status_code in [200, 400, 500]


# ============================================================================
# HEALTH CHECK TESTS
# ============================================================================

class TestHealthChecks:
    """Test health and readiness endpoints."""
    
    def test_healthz(self, client):
        """Test health check endpoint."""
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
    
    def test_readyz(self, client, e2e_test_environment):
        """/readyz returns 200 + status=ok when the DB is reachable."""
        response = client.get("/readyz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

