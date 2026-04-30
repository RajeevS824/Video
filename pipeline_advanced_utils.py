#############################################################
# PIPELINE v3.1 - ADVANCED UTILITIES
# All 10 Critical Production Fixes
#############################################################

import os
import asyncio
import subprocess
import logging
import time
import json
import multiprocessing
from pathlib import Path
from typing import Dict, Any, Optional, Callable
from functools import wraps
from datetime import datetime, timedelta
from collections import defaultdict

logger = logging.getLogger(__name__)

#############################################################
# FIX #1: STREAMING FILE UPLOAD (Memory-Safe)
#############################################################

async def save_upload_file_streaming(
    upload_file,
    destination: str,
    chunk_size: int = 1048576,  # 1MB
    max_file_size: int = 500 * 1024 * 1024  # 500MB
) -> Dict[str, Any]:
    """
    Save uploaded file in chunks to avoid memory bloat.
    
    Args:
        upload_file: FastAPI UploadFile
        destination: Where to save
        chunk_size: Bytes per chunk (1MB default)
        max_file_size: Max allowed file size
    
    Returns:
        Dict with file info and success status
    """
    
    total_size = 0
    chunks_written = 0
    
    try:
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        
        with open(destination, "wb") as buffer:
            while True:
                chunk = await upload_file.read(chunk_size)
                
                if not chunk:
                    break
                
                # Check size limit
                total_size += len(chunk)
                if total_size > max_file_size:
                    raise ValueError(
                        f"FILE_TOO_LARGE ({total_size / (1024*1024):.1f}MB "
                        f"> {max_file_size / (1024*1024):.0f}MB)"
                    )
                
                buffer.write(chunk)
                chunks_written += 1
                
                # Log progress for large files
                if chunks_written % 100 == 0:
                    logger.debug(
                        f"Upload progress: {total_size / (1024*1024):.1f}MB, "
                        f"{chunks_written} chunks"
                    )
        
        logger.info(
            f"Upload complete: {total_size / (1024*1024):.1f}MB in {chunks_written} chunks"
        )
        
        return {
            "success": True,
            "path": destination,
            "size_bytes": total_size,
            "chunks": chunks_written
        }
    
    except Exception as e:
        logger.error(f"Upload failed: {str(e)}")
        if os.path.exists(destination):
            os.remove(destination)
        raise

#############################################################
# FIX #2: SUBPROCESS WITH TIMEOUT + ERROR HANDLING
#############################################################

class SubprocessExecutor:
    """Execute subprocesses safely with timeout and error handling."""
    
    def __init__(
        self,
        timeout_sec: int = 30,
        max_retries: int = 3,
        log_output: bool = True
    ):
        self.timeout_sec = timeout_sec
        self.max_retries = max_retries
        self.log_output = log_output
    
    def run(self, cmd: list, working_dir: Optional[str] = None) -> Dict[str, Any]:
        """
        Run command with timeout, retry, and error handling.
        
        Args:
            cmd: Command list (e.g., ["ffmpeg", "-i", "input.mp4"])
            working_dir: Optional working directory
        
        Returns:
            Dict with stdout, stderr, return_code, success
        """
        
        last_error = None
        
        for attempt in range(1, self.max_retries + 1):
            try:
                logger.info(
                    f"Subprocess attempt {attempt}/{self.max_retries}: {' '.join(cmd)}"
                )
                
                result = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=self.timeout_sec,
                    check=False,  # Don't raise, handle manually
                    text=True,
                    cwd=working_dir
                )
                
                if result.returncode == 0:
                    if self.log_output and result.stdout:
                        logger.debug(f"Subprocess stdout: {result.stdout[:200]}")
                    
                    return {
                        "success": True,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                        "return_code": 0,
                        "attempt": attempt
                    }
                
                else:
                    error_msg = result.stderr[:500] if result.stderr else "Unknown error"
                    last_error = f"SUBPROCESS_FAILED ({result.returncode}): {error_msg}"
                    logger.warning(f"Attempt {attempt} failed: {last_error}")
                    
                    if attempt < self.max_retries:
                        backoff = 2 ** attempt
                        logger.info(f"Retrying in {backoff}s...")
                        time.sleep(backoff)
            
            except subprocess.TimeoutExpired as e:
                last_error = f"SUBPROCESS_TIMEOUT ({self.timeout_sec}s exceeded)"
                logger.warning(f"Attempt {attempt} timeout: {last_error}")
                
                if attempt < self.max_retries:
                    backoff = 2 ** attempt
                    logger.info(f"Retrying in {backoff}s...")
                    time.sleep(backoff)
            
            except Exception as e:
                last_error = f"SUBPROCESS_ERROR: {str(e)}"
                logger.error(f"Attempt {attempt} error: {last_error}")
                raise
        
        # All retries exhausted
        return {
            "success": False,
            "error": last_error,
            "return_code": 1,
            "attempts": self.max_retries
        }

#############################################################
# FIX #3: DYNAMIC WORKER COUNT
#############################################################

def get_optimal_worker_count(
    min_workers: int = 2,
    max_workers: int = 32,
    multiplier: float = 2.0
) -> int:
    """
    Calculate optimal number of workers based on CPU count.
    
    Formula: min(max_workers, cpu_count * multiplier)
    """
    
    cpu_count = multiprocessing.cpu_count()
    optimal = min(int(cpu_count * multiplier), max_workers)
    optimal = max(optimal, min_workers)  # At least min_workers
    
    logger.info(
        f"Optimal workers: {optimal} "
        f"(cpu_count={cpu_count}, multiplier={multiplier})"
    )
    
    return optimal

#############################################################
# FIX #4: DETERMINISTIC DEMUCS OUTPUT PARSING
#############################################################

def get_demucs_vocals_path(output_dir: str) -> str:
    """
    Safely extract vocals.wav from Demucs output.
    
    Handles non-deterministic output by sorting and validating.
    
    Args:
        output_dir: Demucs output directory
    
    Returns:
        Path to vocals.wav
    
    Raises:
        FileNotFoundError: If structure is invalid
    """
    
    output_path = Path(output_dir)
    
    if not output_path.exists():
        raise FileNotFoundError(f"Output directory not found: {output_dir}")
    
    # Find all subdirectories, sorted by modification time (most recent first)
    dirs = sorted(
        [d for d in output_path.iterdir() if d.is_dir()],
        key=lambda x: x.stat().st_mtime,
        reverse=True
    )
    
    if not dirs:
        raise FileNotFoundError(f"No model directories in {output_dir}")
    
    model_dir = dirs[0]
    logger.info(f"Using latest Demucs output: {model_dir.name}")
    
    # Find track directory (usually only one)
    track_dirs = sorted([d for d in model_dir.iterdir() if d.is_dir()])
    
    if not track_dirs:
        raise FileNotFoundError(f"No track directories in {model_dir}")
    
    track_dir = track_dirs[0]
    logger.info(f"Using track: {track_dir.name}")
    
    # Validate vocals.wav exists
    vocals_path = track_dir / "vocals.wav"
    
    if not vocals_path.exists():
        available_files = list(track_dir.iterdir())
        raise FileNotFoundError(
            f"vocals.wav not found in {track_dir}. "
            f"Available: {[f.name for f in available_files]}"
        )
    
    logger.info(f"Found vocals: {vocals_path}")
    return str(vocals_path)

#############################################################
# FIX #5: RATE LIMITING
#############################################################

class RateLimiter:
    """
    Token bucket rate limiter with per-IP and per-user support.
    """
    
    def __init__(
        self,
        requests_per_minute: int = 60,
        burst_allowed: int = 10
    ):
        self.requests_per_minute = requests_per_minute
        self.burst_allowed = burst_allowed
        self.tokens: Dict[str, list] = defaultdict(list)
        self.lock = asyncio.Lock()
    
    async def check_limit(self, identifier: str) -> Dict[str, Any]:
        """
        Check if request is allowed.
        
        Args:
            identifier: IP address or user ID
        
        Returns:
            {allowed: bool, remaining: int, reset_in_sec: float}
        """
        
        async with self.lock:
            now = time.time()
            window_start = now - 60  # 1 minute window
            
            # Clean old tokens
            self.tokens[identifier] = [
                t for t in self.tokens[identifier] if t > window_start
            ]
            
            token_count = len(self.tokens[identifier])
            allowed = token_count < self.requests_per_minute
            
            if allowed:
                self.tokens[identifier].append(now)
                remaining = self.requests_per_minute - token_count - 1
                reset_in = 60 - (now - self.tokens[identifier][0])
            else:
                remaining = 0
                oldest_token = self.tokens[identifier][0]
                reset_in = 60 - (now - oldest_token)
            
            logger.debug(
                f"Rate limit check: {identifier} "
                f"({token_count}/{self.requests_per_minute})"
            )
            
            return {
                "allowed": allowed,
                "remaining": max(0, remaining),
                "reset_in_sec": max(0, reset_in),
                "tokens": token_count
            }

#############################################################
# FIX #6: SAFE LOGGING SETUP (Prevent Duplication)
#############################################################

def setup_logger_safe(
    name: str,
    level: str = "INFO",
    log_file: Optional[str] = None
) -> logging.Logger:
    """
    Setup logger safely without duplication.
    """
    
    logger_instance = logging.getLogger(name)
    logger_instance.setLevel(level)
    
    # Only add handlers if none exist
    if logger_instance.handlers:
        logger.debug(f"Logger {name} already has {len(logger_instance.handlers)} handler(s)")
        return logger_instance
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)s | %(message)s"
    )
    console_handler.setFormatter(formatter)
    logger_instance.addHandler(console_handler)
    
    # File handler (optional)
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        logger_instance.addHandler(file_handler)
    
    logger.info(f"Logger {name} setup complete with {len(logger_instance.handlers)} handler(s)")
    return logger_instance

#############################################################
# FIX #7: CIRCUIT BREAKER PATTERN
#############################################################

class CircuitBreaker:
    """
    Circuit breaker: prevents cascading failures.
    
    States: CLOSED (normal) → OPEN (failing) → HALF_OPEN (recovering) → CLOSED
    """
    
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout_sec: int = 60
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout_sec = recovery_timeout_sec
        
        self.failure_count = 0
        self.last_failure_time = None
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
    
    def record_success(self) -> None:
        """Record successful operation."""
        if self.state == "HALF_OPEN":
            logger.info(f"Circuit {self.name}: HALF_OPEN → CLOSED")
            self.state = "CLOSED"
        
        self.failure_count = 0
        self.last_failure_time = None
    
    def record_failure(self) -> None:
        """Record failed operation."""
        self.failure_count += 1
        self.last_failure_time = time.time()
        
        if self.failure_count >= self.failure_threshold:
            logger.warning(
                f"Circuit {self.name}: CLOSED → OPEN "
                f"({self.failure_count} failures)"
            )
            self.state = "OPEN"
    
    def check(self) -> bool:
        """
        Check if circuit allows operation.
        
        Returns:
            True if operation allowed, False if circuit is OPEN
        """
        
        if self.state == "CLOSED":
            return True
        
        if self.state == "OPEN":
            # Try to recover after timeout
            if time.time() - self.last_failure_time > self.recovery_timeout_sec:
                logger.info(f"Circuit {self.name}: OPEN → HALF_OPEN (recovery)")
                self.state = "HALF_OPEN"
                self.failure_count = 0
                return True
            
            return False
        
        # HALF_OPEN: allow one attempt
        return True

#############################################################
# FIX #8: SINGLE WORKER VALIDATION
#############################################################

def validate_single_worker_config(workers: int) -> None:
    """
    Validate that workers is set to 1 (CRITICAL for model loading).
    
    Raises:
        ValueError: If workers > 1
    """
    
    if workers > 1:
        raise ValueError(
            f"CRITICAL: FastAPI workers must be 1, got {workers}. "
            f"Models are loaded at startup and shared across processes. "
            f"Multiple workers = multiple model loads = excessive memory. "
            f"Scale using Kubernetes, not uvicorn workers."
        )
    
    logger.info("✓ Single worker validation passed")

#############################################################
# FIX #9: REDIS QUEUE HANDLER
#############################################################

class RedisQueueHandler:
    """Handle job queuing with Redis."""
    
    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        password: Optional[str] = None
    ):
        self.host = host
        self.port = port
        self.db = db
        self.password = password
        self.redis = None
    
    async def connect(self) -> None:
        """Connect to Redis."""
        try:
            import aioredis
            
            self.redis = await aioredis.create_redis_pool(
                f"redis://{self.host}:{self.port}",
                db=self.db,
                password=self.password,
                timeout=5
            )
            logger.info(f"Connected to Redis: {self.host}:{self.port}")
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            raise
    
    async def enqueue_job(
        self,
        job_id: str,
        job_data: Dict[str, Any],
        queue_name: str = "audio_jobs"
    ) -> bool:
        """
        Add job to queue.
        
        Args:
            job_id: Unique job ID
            job_data: Job payload
            queue_name: Queue name
        
        Returns:
            True if enqueued successfully
        """
        
        try:
            job_json = json.dumps({
                "id": job_id,
                "data": job_data,
                "timestamp": datetime.now().isoformat(),
                "status": "QUEUED"
            })
            
            await self.redis.lpush(queue_name, job_json)
            logger.info(f"Enqueued job {job_id}")
            return True
        
        except Exception as e:
            logger.error(f"Failed to enqueue job: {e}")
            return False
    
    async def dequeue_job(
        self,
        queue_name: str = "audio_jobs",
        timeout_sec: int = 1
    ) -> Optional[Dict[str, Any]]:
        """
        Get next job from queue.
        
        Args:
            queue_name: Queue name
            timeout_sec: Blocking timeout
        
        Returns:
            Job dict or None if queue empty
        """
        
        try:
            result = await self.redis.brpop(queue_name, timeout=timeout_sec)
            
            if result:
                job_json = result[1].decode() if isinstance(result[1], bytes) else result[1]
                job = json.loads(job_json)
                logger.info(f"Dequeued job {job.get('id')}")
                return job
            
            return None
        
        except Exception as e:
            logger.error(f"Failed to dequeue job: {e}")
            return None

#############################################################
# FIX #10: CONFIG VALIDATION
#############################################################

def validate_config(config_dict: Dict[str, Any]) -> List[str]:
    """
    Validate configuration on startup.
    
    Returns:
        List of validation errors (empty if valid)
    """
    
    errors = []
    
    # Required keys (configurable)
    required_keys = [
        "models.whisper.name",
        "audio.max_duration_sec",
        "quality.dnsmos.pass_threshold",
        "latency_sla.max_inference_sec",
        "deployment.workers"
    ]
    
    for key in required_keys:
        keys = key.split(".")
        value = config_dict
        
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                value = None
                break
        
        if value is None:
            errors.append(f"MISSING_CONFIG: {key}")
    
    # Worker count validation
    if config_dict.get("deployment", {}).get("workers", 0) > 1:
        errors.append(
            "INVALID_CONFIG: deployment.workers must be 1 "
            "(scale using Kubernetes instead)"
        )
    
    # Threshold validations
    if "quality" in config_dict:
        quality = config_dict["quality"]
        if "dnsmos" in quality:
            dnsmos = quality["dnsmos"]
            pass_thresh = dnsmos.get("pass_threshold", 0)
            enhance_thresh = dnsmos.get("enhance_threshold", 0)
            
            if pass_thresh < enhance_thresh:
                errors.append(
                    f"INVALID_CONFIG: pass_threshold ({pass_thresh}) must be >= "
                    f"enhance_threshold ({enhance_thresh})"
                )
    
    if errors:
        logger.error(f"Configuration validation failed:\n  " + "\n  ".join(errors))
    else:
        logger.info("✓ Configuration validation passed")
    
    return errors
