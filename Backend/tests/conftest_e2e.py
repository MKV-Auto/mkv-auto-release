"""
Comprehensive mocking framework for end-to-end testing.

This module provides fixtures that mock:
- Redis/Celery (synchronous task execution)
- Database (SQLite in-memory)
- Drive/Disc operations (makemkv, scanning, etc.)

This allows testing the entire flow from API → Gatekeeper → Celery Task → Disc Operations
without requiring external services or hardware.
"""
import os
import uuid
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from typing import Dict, Any, Optional
import pytest

from tests.fixtures.mock_drive import MockDrive
from tests.fixtures.mock_mkv import MockMKV


class MockCeleryTask:
    """Mock Celery task that executes synchronously."""
    
    def __init__(self, task_func, task_id: str):
        self.task_func = task_func
        self.task_id = task_id
        self.id = task_id
        self.state = "PENDING"
        self.result = None
        self._executed = False
        self.name = getattr(task_func, '__name__', 'unknown_task')
    
    def apply_async(self, args=None, kwargs=None, task_id=None, **options):
        """Execute task synchronously instead of queuing."""
        if task_id:
            self.task_id = task_id
            self.id = task_id
        
        # Store task_id for later execution
        self._args = args or ()
        self._kwargs = kwargs or {}
        self._options = options
        
        # Return self to allow .id access
        return self
    
    def delay(self, *args, **kwargs):
        """Delay method that calls apply_async."""
        return self.apply_async(args=args, kwargs=kwargs)
    
    def get(self, timeout=None):
        """Get task result (synchronous execution)."""
        if not self._executed:
            # Execute the task function directly
            try:
                # Create a mock request object for the task
                mock_request = MagicMock()
                mock_request.id = self.task_id
                
                # If task_func is bound, call it with self
                if hasattr(self.task_func, '__self__'):
                    # Bound method - call with self and args
                    result = self.task_func(*self._args, **self._kwargs)
                else:
                    # Unbound - need to create task instance
                    # For rip_disc, it's a bound method, so we need to handle it differently
                    if len(self._args) > 0:
                        # First arg is usually self (the task instance)
                        # We'll call the underlying function directly
                        result = self.task_func(*self._args, **self._kwargs)
                    else:
                        result = self.task_func(**self._kwargs)
                
                self.result = result
                self.state = "SUCCESS"
            except Exception as e:
                self.result = e
                self.state = "FAILURE"
                raise
            finally:
                self._executed = True
        
        if self.state == "FAILURE":
            raise self.result
        return self.result


class MockCeleryApp:
    """Mock Celery app that executes tasks synchronously."""
    
    def __init__(self):
        self.tasks: Dict[str, MockCeleryTask] = {}
        self.backend = Mock()
        self.broker = Mock()
    
    def task(self, *args, **kwargs):
        """Decorator that registers a task."""
        def decorator(func):
            task_name = kwargs.get('name') or func.__name__
            # Create a mock task that executes synchronously
            mock_task = MockCeleryTask(func, f"mock_task_{uuid.uuid4().hex[:8]}")
            self.tasks[task_name] = mock_task
            
            # Return a callable that mimics Celery task behavior
            def task_wrapper(*task_args, **task_kwargs):
                return mock_task.apply_async(args=task_args, kwargs=task_kwargs)
            
            task_wrapper.apply_async = mock_task.apply_async
            task_wrapper.delay = mock_task.apply_async
            task_wrapper.run = func  # Direct execution
            task_wrapper.name = task_name
            
            return task_wrapper
        return decorator


@pytest.fixture
def mock_celery(monkeypatch):
    """Mock Celery to execute tasks synchronously."""
    # Create a mock that wraps the real rip_disc function
    from workers import tasks
    
    # Store original task
    original_rip_disc = tasks.rip_disc
    
    # Create mock task result
    class MockTaskResult:
        def __init__(self, task_id):
            self.id = task_id
            self.state = "PENDING"
        
        def get(self, timeout=None):
            return None
    
    # Mock apply_async to execute synchronously
    def mock_apply_async(self, args=None, kwargs=None, task_id=None, **options):
        """Execute task synchronously and return mock result."""
        task_id = task_id or f"mock_task_{uuid.uuid4().hex[:8]}"
        result = MockTaskResult(task_id)
        
        # Store task info for later execution if needed
        result._args = args
        result._kwargs = kwargs
        result._task_func = original_rip_disc
        
        return result
    
    # Patch rip_disc.apply_async
    monkeypatch.setattr(tasks.rip_disc, "apply_async", lambda *a, **kw: mock_apply_async(None, *a, **kw))
    
    # Also patch celery_app.task decorator to return tasks that execute synchronously
    original_task_decorator = tasks.celery_app.task
    
    def mock_task_decorator(*args, **kwargs):
        def decorator(func):
            # Return a callable that mimics Celery task
            task_wrapper = func
            task_wrapper.apply_async = lambda *a, **kw: mock_apply_async(None, *a, **kw)
            task_wrapper.delay = lambda *a, **kw: mock_apply_async(None, args=a, kwargs=kw)
            task_wrapper.run = func
            return task_wrapper
        return decorator
    
    monkeypatch.setattr(tasks.celery_app, "task", mock_task_decorator)
    
    return {"app": tasks.celery_app, "rip_disc": tasks.rip_disc}


@pytest.fixture
def mock_redis(monkeypatch):
    """Mock Redis to avoid connection errors."""
    class MockRedis:
        def __init__(self, *args, **kwargs):
            self.data = {}
            self.connected = True
        
        def get(self, key):
            return self.data.get(key)
        
        def set(self, key, value, *args, **kwargs):
            self.data[key] = value
            return True
        
        def delete(self, key):
            return self.data.pop(key, None) is not None
        
        def exists(self, key):
            return key in self.data
        
        def flushall(self):
            self.data.clear()
            return True
        
        def ping(self):
            return True

        def pipeline(self, *args, **kwargs):
            """Celery / Redis client API: ``pipeline()`` returns a
            chainable transaction object. The tests don't exercise the
            transaction path; a no-op chain that swallows method calls
            and returns itself is enough."""
            class _NoopPipeline:
                def execute(self_): return []
                # Pipeline is used as a context manager by the rip-complete
                # callback's transactional state writes (``with conn.pipeline()
                # as pipe: pipe.set(...); pipe.execute()``). Implement the
                # protocol so ``with`` doesn't raise.
                def __enter__(self_): return self_
                def __exit__(self_, *exc): return False
                def __getattr__(self_, _name): return lambda *a, **kw: self_
            return _NoopPipeline()

        @staticmethod
        def pubsub(*args, **kwargs):
            """Celery's RedisBackend.start calls
            ``self.backend.client.pubsub(...)`` to wire result polling.
            Without this stub the test fails (#401). Marked @staticmethod
            because the monkeypatch on line 200 makes ``RedisBackend.client``
            the MockRedis class itself (not an instance), so Celery calls
            ``MockRedis.pubsub(...)`` unbound. A no-op object with the
            methods Celery touches is enough — these tests don't exercise
            the chord/result-polling path."""
            class _NoopPubSub:
                def subscribe(self_, *a, **kw): pass
                def unsubscribe(self_, *a, **kw): pass
                def get_message(self_, *a, **kw): return None
                def close(self_): pass
                def execute_command(self_, *a, **kw): pass
                def parse_response(self_, *a, **kw): return None
            return _NoopPubSub()

        def __getattr__(self, name):
            """Catch-all for the long-tail of Redis methods the rip-complete
            callback's state writes might reach for (sadd / srem / hset /
            hget / hgetall / smembers / etc.). The tests don't assert on
            these — they just need them to not blow up. Return a callable
            that returns 0/None/empty-list depending on the method's
            typical signature.

            Listed methods above (get/set/delete/exists/flushall/ping/
            pipeline/pubsub) take precedence over this fallback because
            ``__getattr__`` only fires for missing attributes."""
            def _safe_default(*_a, **_kw):
                # 0 is a safe default for the *adders / *removers
                # (sadd, srem, hset, zadd) that return the number of
                # affected rows; None for getters; [] for scans.
                if name.startswith(('h', 's', 'z')) and name.endswith(('add', 'rem', 'set')):
                    return 0
                if name in ('smembers', 'hgetall', 'lrange', 'scan_iter', 'keys'):
                    return []
                return None
            return _safe_default

    # Mock redis connection
    monkeypatch.setattr("celery.backends.redis.RedisBackend.client", MockRedis)
    monkeypatch.setattr("redis.Redis", MockRedis)
    
    return MockRedis()


@pytest.fixture
def e2e_db(test_db):
    """Deprecated: alias for test_db. Use test_db in new tests."""
    return test_db


@pytest.fixture
def enhanced_fake_drive_manager(monkeypatch, tmp_path):
    """MockDrive-based drive fixture for e2e: patches list_drives, get_disc_info, refresh_disc_info, validate_disc_info; seeds real disc_cache. Keeps real scan_disc_info/hash_disc for test_drive_operations_comprehensive."""
    # Avoid cache leak across tests
    try:
        from core import disc_cache
        disc_cache.clear()
        disc_cache.DISK_PERSIST_ENABLED = False
        try:
            disc_cache._cache_file.unlink(missing_ok=True)  # type: ignore[attr-defined]
        except Exception:
            pass
    except Exception:
        pass

    discinfo_payload = {
        "disc_num": "1",
        "mount_point": "/dev/sr0",
        "disc_hash": "test_disc_hash_12345",
        "content_hash": "test_disc_hash_12345",
        "info_title": "Test Movie",
        "format": "Blu-Ray",
        "disc_format": "Blu-Ray",
        "show_title": "Test Show",
        "show_image": None,
        "resolution": "1080p",
        "title_type": "movie",
        "tracks": {"00001.mpls": {"season": "1", "episode": "1", "episode_name": "Pilot", "format": "MainFeature"}},
        "titles": {"00001.mpls": {"file": "00001.mpls", "title": "Test Movie", "description": "Main Feature"}},
        "info_log": "TINFO:0,0,0,\"Test Movie\"\nSINFO:0,0,19,0,\"1920x1080\"\n",
        "raw_info_log": "TINFO:0,0,0,\"Test Movie\"\nSINFO:0,0,19,0,\"1920x1080\"\n",
    }
    mock = MockDrive(drives=[("1", "/dev/sr0")], discinfo_payload=discinfo_payload)

    monkeypatch.setattr("core._drive_operations.list_drives", mock.list_drives)
    monkeypatch.setattr("core._drive_operations.get_disc_info", mock.get_disc_info)
    monkeypatch.setattr("core._drive_operations.refresh_disc_info", mock.refresh_disc_info)
    monkeypatch.setattr("core._drive_operations.validate_disc_info", mock.validate_disc_info)
    monkeypatch.setattr("api.routers.jobs.validate_disc_info", mock.validate_disc_info)

    try:
        from core import _drive_operations
        def _noop_internal_only(allowed_callers=None):
            def _decorator(f):
                return f
            return _decorator
        monkeypatch.setattr("core._drive_operations._internal_only", _noop_internal_only)
    except Exception:
        pass

    # Seed real disc_cache so get_cached_discs works
    mock.refresh_disc_info("1", "/dev/sr0")
    return mock


@pytest.fixture
def mock_makemkv(monkeypatch):
    """MockMKV: replaces run_makemkv at core.utils, core.disc, api.crud for e2e rip flows."""
    mock = MockMKV(titles=[{"file": "00001.mpls"}], progress=True)
    monkeypatch.setattr("core.utils.run_makemkv", mock.run_makemkv)
    monkeypatch.setattr("api.crud.run_makemkv", mock.run_makemkv)
    monkeypatch.setattr("core.disc.run_makemkv", mock.run_makemkv)
    # POST /jobs/rip preflights the MakeMKV installation
    # (validate_makemkv_installation) before dispatch; without this patch
    # every rip test 503s on hosts (and CI) without a real makemkvcon binary.
    monkeypatch.setattr(
        "core.makemkv_updater.validate_makemkv_installation",
        lambda: {
            "is_valid": True,
            "can_rip": True,
            "missing_components": [],
            "error_message": None,
            "installed_version": "1.17.7-mock",
            "binary_path": "/usr/bin/makemkvcon",
        },
    )
    return mock


@pytest.fixture
def mock_disc_cache(monkeypatch):
    """Mock disc cache to control cache behavior in tests."""
    cache_data = {}
    
    def mock_get(disc_num: str):
        return cache_data.get(str(disc_num))
    
    def mock_set(disc_num: str, payload: dict):
        cache_data[str(disc_num)] = payload
    
    monkeypatch.setattr("core.disc_cache.get", mock_get)
    monkeypatch.setattr("core.disc_cache.set_payload", mock_set)
    
    return {"get": mock_get, "set": mock_set, "data": cache_data}


@pytest.fixture
def e2e_test_environment(monkeypatch, tmp_path, test_db, mock_celery, mock_redis,
                          enhanced_fake_drive_manager, mock_makemkv):
    """
    Comprehensive fixture that sets up the entire test environment.

    - Database (test_db / MockDatabase), Celery/Redis (sync), Drive (MockDrive via enhanced_fake_drive_manager),
      Makemkv (mocked). Disc cache is real; enhanced_fake_drive_manager seeds it.
    """
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("workers.tasks.DATA_ROOT", data_root)
    # Writable tmp for disc_locks (acquire_operation_lock uses get_mkvauto_tmp())
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MKVAUTO_TMP_DIR", str(tmp_dir))

    # Worker-side rip-complete / progress callbacks call back into the API via
    # `requests.post(API_URL/...)`. Without intercepting this, the host's real
    # mkv-auto container (or just a dead port 8000) catches the request and
    # the worker sees HTTP 403 / connection error, breaking the whole chain.
    # The shared `stage_callback_intercept` helper replaces workers.tasks.requests
    # with a TestClient-backed fake that routes callbacks at the in-process API.
    from tests.fixtures.stage_callback_intercept import make_stage_callback_fake_requests
    _fake_requests = make_stage_callback_fake_requests()
    monkeypatch.setattr("workers.tasks.requests", _fake_requests)

    yield {
        "db": test_db,
        "celery": mock_celery,
        "redis": mock_redis,
        "drive_manager": enhanced_fake_drive_manager,
        "makemkv": mock_makemkv,
        "data_root": data_root,
        "tmp_path": tmp_path,
    }

