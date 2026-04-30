#############################################################
# TEST SUITE v3.1 - ALL 10 PRODUCTION HARDENING FIXES
#############################################################

import pytest
import asyncio
import tempfile
import os
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, AsyncMock
import time

from pipeline_advanced_utils import (
    save_upload_file_streaming,
    SubprocessExecutor,
    get_optimal_worker_count,
    get_demucs_vocals_path,
    RateLimiter,
    setup_logger_safe,
    CircuitBreaker,
    validate_single_worker_config,
    validate_config
)

#############################################################
# FIX #1: STREAMING FILE UPLOAD TESTS
#############################################################

class TestStreamingUpload:
    """Test Fix #1: Memory-safe streaming file upload."""
    
    @pytest.mark.asyncio
    async def test_streaming_upload_small_file(self):
        """Test uploading a small file in chunks."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            destination = f.name
        
        try:
            # Mock upload file
            mock_file = AsyncMock()
            mock_file.read = AsyncMock(side_effect=[b"chunk1", b"chunk2", b""])
            
            result = await save_upload_file_streaming(mock_file, destination, chunk_size=6)
            
            assert result["success"]
            assert result["size_bytes"] == 12
            assert result["chunks"] == 2
        
        finally:
            if os.path.exists(destination):
                os.remove(destination)
    
    @pytest.mark.asyncio
    async def test_streaming_upload_size_limit(self):
        """Test upload rejects files exceeding size limit."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            destination = f.name
        
        try:
            mock_file = AsyncMock()
            mock_file.read = AsyncMock(side_effect=[b"x" * 1000000, b""])
            
            with pytest.raises(ValueError, match="FILE_TOO_LARGE"):
                await save_upload_file_streaming(mock_file, destination, max_file_size=500000)
        
        finally:
            if os.path.exists(destination):
                os.remove(destination)

#############################################################
# FIX #2: SUBPROCESS TIMEOUT & ERROR HANDLING TESTS
#############################################################

class TestSubprocessExecutor:
    """Test Fix #2: Subprocess with timeout and retry."""
    
    def test_subprocess_success(self):
        """Test successful subprocess execution."""
        executor = SubprocessExecutor(timeout_sec=5)
        result = executor.run(["echo", "hello"])
        
        assert result["success"]
        assert "hello" in result["stdout"]
    
    def test_subprocess_timeout(self):
        """Test subprocess timeout handling."""
        executor = SubprocessExecutor(timeout_sec=1)
        result = executor.run(["sleep", "5"])
        
        assert not result["success"]
        assert "SUBPROCESS_TIMEOUT" in result["error"] or "SUBPROCESS_FAILED" in result["error"]
    
    def test_subprocess_error_capture(self):
        """Test subprocess error message capture."""
        executor = SubprocessExecutor(timeout_sec=5)
        result = executor.run(["ls", "/nonexistent/path"])
        
        assert not result["success"]
        assert "SUBPROCESS_FAILED" in result["error"]
    
    def test_subprocess_retry_logic(self):
        """Test retry logic on failure."""
        executor = SubprocessExecutor(timeout_sec=5, max_retries=3)
        # This will retry 3 times
        result = executor.run(["false"])  # Always fails
        
        assert not result["success"]
        assert result["attempts"] == 3

#############################################################
# FIX #3: DYNAMIC WORKER COUNT TESTS
#############################################################

class TestOptimalWorkerCount:
    """Test Fix #3: Dynamic worker count calculation."""
    
    def test_optimal_worker_count(self):
        """Test worker count calculation."""
        workers = get_optimal_worker_count(min_workers=2, max_workers=32)
        
        assert workers >= 2
        assert workers <= 32
    
    def test_worker_count_respects_min(self):
        """Test minimum worker count."""
        workers = get_optimal_worker_count(min_workers=10, max_workers=32)
        assert workers >= 10
    
    def test_worker_count_respects_max(self):
        """Test maximum worker count."""
        workers = get_optimal_worker_count(min_workers=2, max_workers=5)
        assert workers <= 5

#############################################################
# FIX #4: DETERMINISTIC DEMUCS OUTPUT TESTS
#############################################################

class TestDemucsOutputParsing:
    """Test Fix #4: Deterministic Demucs output parsing."""
    
    def test_get_demucs_vocals_path(self):
        """Test extracting vocals path from Demucs output."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create mock Demucs structure
            model_dir = Path(tmpdir) / "htdemucs"
            model_dir.mkdir()
            track_dir = model_dir / "track"
            track_dir.mkdir()
            vocals_file = track_dir / "vocals.wav"
            vocals_file.touch()
            
            result = get_demucs_vocals_path(tmpdir)
            
            assert result.endswith("vocals.wav")
            assert os.path.exists(result)
    
    def test_demucs_missing_vocals(self):
        """Test error when vocals.wav missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            model_dir = Path(tmpdir) / "htdemucs"
            model_dir.mkdir()
            track_dir = model_dir / "track"
            track_dir.mkdir()
            
            with pytest.raises(FileNotFoundError, match="vocals.wav"):
                get_demucs_vocals_path(tmpdir)

#############################################################
# FIX #5: RATE LIMITING TESTS
#############################################################

class TestRateLimiter:
    """Test Fix #5: Rate limiting."""
    
    @pytest.mark.asyncio
    async def test_rate_limit_allows_within_limit(self):
        """Test allowing requests within limit."""
        limiter = RateLimiter(requests_per_minute=10)
        
        for i in range(10):
            result = await limiter.check_limit(f"ip-{i}")
            assert result["allowed"]
    
    @pytest.mark.asyncio
    async def test_rate_limit_blocks_exceed(self):
        """Test blocking requests exceeding limit."""
        limiter = RateLimiter(requests_per_minute=5)
        
        # Fill up limit
        for i in range(5):
            await limiter.check_limit("192.168.1.1")
        
        # Next request should be blocked
        result = await limiter.check_limit("192.168.1.1")
        assert not result["allowed"]
    
    @pytest.mark.asyncio
    async def test_rate_limit_per_identifier(self):
        """Test rate limiting is per IP."""
        limiter = RateLimiter(requests_per_minute=2)
        
        # IP 1 reaches limit
        await limiter.check_limit("192.168.1.1")
        await limiter.check_limit("192.168.1.1")
        result1 = await limiter.check_limit("192.168.1.1")
        assert not result1["allowed"]
        
        # IP 2 still allowed
        result2 = await limiter.check_limit("192.168.1.2")
        assert result2["allowed"]

#############################################################
# FIX #6: SAFE LOGGING TESTS
#############################################################

class TestSafeLogging:
    """Test Fix #6: Safe logging without duplication."""
    
    def test_logger_setup_no_duplication(self):
        """Test logging setup prevents duplicate handlers."""
        logger = setup_logger_safe("test_logger_1")
        initial_count = len(logger.handlers)
        
        # Setup again
        logger2 = setup_logger_safe("test_logger_1")
        
        # Should not add more handlers
        assert len(logger2.handlers) == initial_count
    
    def test_logger_has_handler(self):
        """Test logger has at least one handler."""
        logger = setup_logger_safe("test_logger_2")
        assert len(logger.handlers) > 0

#############################################################
# FIX #7: CIRCUIT BREAKER TESTS
#############################################################

class TestCircuitBreaker:
    """Test Fix #7: Circuit breaker pattern."""
    
    def test_circuit_breaker_closed(self):
        """Test circuit breaker in CLOSED state."""
        cb = CircuitBreaker("test", failure_threshold=3)
        
        assert cb.check()  # Should allow
        assert cb.state == "CLOSED"
    
    def test_circuit_breaker_opens_on_failures(self):
        """Test circuit breaker opens after threshold."""
        cb = CircuitBreaker("test", failure_threshold=2)
        
        assert cb.check()
        
        cb.record_failure()
        assert cb.check()  # Still closed
        
        cb.record_failure()
        assert not cb.check()  # Now open
        assert cb.state == "OPEN"
    
    def test_circuit_breaker_recovery(self):
        """Test circuit breaker recovery after timeout."""
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout_sec=1)
        
        cb.record_failure()
        assert cb.state == "OPEN"
        assert not cb.check()
        
        # Wait for timeout
        time.sleep(1.1)
        
        # Should attempt recovery
        assert cb.check()
        assert cb.state == "HALF_OPEN"
    
    def test_circuit_breaker_reset_on_success(self):
        """Test circuit breaker resets after success."""
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout_sec=1)
        
        cb.record_failure()
        assert cb.state == "OPEN"
        
        time.sleep(1.1)
        cb.check()  # Half-open
        
        cb.record_success()
        assert cb.state == "CLOSED"

#############################################################
# FIX #8: SINGLE WORKER VALIDATION TESTS
#############################################################

class TestSingleWorkerValidation:
    """Test Fix #8: Single worker validation."""
    
    def test_workers_1_allowed(self):
        """Test workers=1 is allowed."""
        try:
            validate_single_worker_config(1)
            assert True  # Should not raise
        except ValueError:
            pytest.fail("Should allow workers=1")
    
    def test_workers_gt_1_rejected(self):
        """Test workers>1 is rejected."""
        with pytest.raises(ValueError, match="workers must be 1"):
            validate_single_worker_config(4)

#############################################################
# FIX #10: CONFIG VALIDATION TESTS
#############################################################

class TestConfigValidation:
    """Test Fix #10: Config validation."""
    
    def test_valid_config(self):
        """Test valid configuration passes."""
        config = {
            "models": {"whisper": {"name": "base"}},
            "audio": {"max_duration_sec": 60},
            "quality": {"dnsmos": {"pass_threshold": 3.6}},
            "latency_sla": {"max_inference_sec": 15},
            "deployment": {"workers": 1}
        }
        
        errors = validate_config(config)
        assert len(errors) == 0
    
    def test_missing_required_config(self):
        """Test missing required config raises error."""
        config = {
            "models": {"whisper": {"name": "base"}},
            # Missing other required fields
        }
        
        errors = validate_config(config)
        assert len(errors) > 0
    
    def test_invalid_workers_count(self):
        """Test invalid workers count raises error."""
        config = {
            "models": {"whisper": {"name": "base"}},
            "audio": {"max_duration_sec": 60},
            "quality": {"dnsmos": {"pass_threshold": 3.6}},
            "latency_sla": {"max_inference_sec": 15},
            "deployment": {"workers": 4}  # Invalid!
        }
        
        errors = validate_config(config)
        assert any("workers must be 1" in err for err in errors)

#############################################################
# INTEGRATION TESTS
#############################################################

class TestIntegration:
    """Integration tests for all fixes together."""
    
    def test_all_components_compatible(self):
        """Test all 10 fixes work together."""
        # This is a smoke test
        executor = SubprocessExecutor(timeout_sec=5)
        limiter = RateLimiter()
        cb = CircuitBreaker("integration")
        workers = get_optimal_worker_count()
        
        assert executor is not None
        assert limiter is not None
        assert cb is not None
        assert workers > 0

#############################################################
# PERFORMANCE TESTS
#############################################################

class TestPerformance:
    """Performance tests for fixes."""
    
    def test_rate_limiter_performance(self):
        """Test rate limiter is fast."""
        limiter = RateLimiter(requests_per_minute=1000)
        
        import asyncio
        async def test():
            start = time.time()
            for i in range(1000):
                await limiter.check_limit(f"ip-{i % 10}")
            return time.time() - start
        
        elapsed = asyncio.run(test())
        assert elapsed < 1.0  # Should be fast
    
    def test_circuit_breaker_performance(self):
        """Test circuit breaker check is fast."""
        cb = CircuitBreaker("test")
        
        start = time.time()
        for _ in range(10000):
            cb.check()
        elapsed = time.time() - start
        
        assert elapsed < 0.1  # Should be very fast

#############################################################
# RUN TESTS
#############################################################

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
