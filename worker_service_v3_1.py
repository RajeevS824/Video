#############################################################
# WORKER SERVICE v3.1
# Process jobs from Redis queue asynchronously
#############################################################

import asyncio
import logging
import json
import sys
from typing import Dict, Any, Optional
from pathlib import Path
import yaml
import time

# Async Redis
try:
    import aioredis
except ImportError:
    print("ERROR: aioredis not installed. Run: pip install aioredis")
    sys.exit(1)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("AudioWorker")

#############################################################
# CONFIG LOADER
#############################################################

def load_config(config_path: str = "config_v3.1.yaml") -> Dict[str, Any]:
    """Load configuration."""
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

config = load_config()

#############################################################
# WORKER CLASS
#############################################################

class AudioWorker:
    """
    Worker service that processes jobs from Redis queue.
    
    Handles:
    - Dequeue jobs
    - Process audio
    - Store results
    - Error handling with retry
    """
    
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
        self.running = False
    
    async def connect(self) -> None:
        """Connect to Redis."""
        try:
            redis_url = f"redis://{self.host}:{self.port}"
            if self.password:
                redis_url = f"redis://:{self.password}@{self.host}:{self.port}"
            
            self.redis = await aioredis.create_redis_pool(
                redis_url,
                db=self.db,
                timeout=5
            )
            logger.info(f"✓ Connected to Redis: {self.host}:{self.port}")
        
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            raise
    
    async def disconnect(self) -> None:
        """Disconnect from Redis."""
        if self.redis:
            self.redis.close()
            await self.redis.wait_closed()
            logger.info("✓ Disconnected from Redis")
    
    async def process_job(self, job: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process a single job.
        
        This is a placeholder - implement actual processing logic here.
        """
        
        job_id = job.get("id")
        logger.info(f"Processing job: {job_id}")
        
        try:
            job_data = job.get("data", {})
            video_path = job_data.get("video_path")
            
            # Placeholder processing
            logger.info(f"  Processing video: {video_path}")
            await asyncio.sleep(2)  # Simulate processing
            
            result = {
                "id": job_id,
                "status": "READY",
                "result": {
                    "transcript": "Sample transcript",
                    "confidence": 0.95,
                    "dnsmos_ovrl": 4.2
                },
                "processed_at": time.time()
            }
            
            logger.info(f"✓ Job {job_id} completed")
            return result
        
        except Exception as e:
            logger.error(f"✗ Job {job_id} failed: {e}")
            return {
                "id": job_id,
                "status": "ERROR",
                "error": str(e),
                "processed_at": time.time()
            }
    
    async def store_result(
        self,
        result: Dict[str, Any],
        queue_name: str = "audio_results"
    ) -> bool:
        """Store job result in Redis."""
        try:
            result_json = json.dumps(result)
            await self.redis.lpush(queue_name, result_json)
            logger.info(f"✓ Result stored for job {result['id']}")
            return True
        
        except Exception as e:
            logger.error(f"Failed to store result: {e}")
            return False
    
    async def run(self, max_workers: int = 4) -> None:
        """
        Run worker loop processing jobs from queue.
        
        Args:
            max_workers: Maximum concurrent jobs
        """
        
        self.running = True
        queue_name = config.get("redis", {}).get("queue_name", "audio_jobs")
        timeout = config.get("redis", {}).get("queue_timeout_sec", 300)
        
        logger.info(f"✓ Worker started (max_workers={max_workers}, timeout={timeout}s)")
        
        active_tasks = set()
        
        try:
            while self.running:
                try:
                    # Try to dequeue
                    result = await self.redis.brpop(queue_name, timeout=1)
                    
                    if not result:
                        # Queue empty, wait a bit
                        await asyncio.sleep(0.5)
                        continue
                    
                    job_json = result[1].decode() if isinstance(result[1], bytes) else result[1]
                    job = json.loads(job_json)
                    
                    logger.info(f"Dequeued job: {job.get('id')}")
                    
                    # Wait if at max capacity
                    while len(active_tasks) >= max_workers:
                        done, active_tasks = await asyncio.wait(
                            active_tasks,
                            return_when=asyncio.FIRST_COMPLETED
                        )
                        
                        # Store results
                        for task in done:
                            try:
                                result = await task
                                await self.store_result(result)
                            except Exception as e:
                                logger.error(f"Task failed: {e}")
                    
                    # Create task for this job
                    task = asyncio.create_task(self.process_job(job))
                    active_tasks.add(task)
                
                except asyncio.TimeoutError:
                    continue
                
                except Exception as e:
                    logger.error(f"Worker error: {e}")
                    await asyncio.sleep(1)
        
        finally:
            # Wait for remaining tasks
            if active_tasks:
                logger.info(f"Waiting for {len(active_tasks)} remaining tasks...")
                done, _ = await asyncio.wait(active_tasks)
                
                for task in done:
                    try:
                        result = await task
                        await self.store_result(result)
                    except Exception as e:
                        logger.error(f"Final task failed: {e}")
            
            self.running = False
            logger.info("✓ Worker stopped")
    
    def stop(self) -> None:
        """Stop the worker."""
        logger.info("Stopping worker...")
        self.running = False

#############################################################
# MAIN
#############################################################

async def main():
    """Main worker entry point."""
    
    logger.info("=" * 70)
    logger.info("AUDIO PIPELINE WORKER SERVICE v3.1")
    logger.info("=" * 70)
    
    # Get config
    redis_config = config.get("redis", {})
    worker_config = config.get("deployment", {})
    
    # Create worker
    worker = AudioWorker(
        host=redis_config.get("host", "localhost"),
        port=redis_config.get("port", 6379),
        db=redis_config.get("db", 0),
        password=redis_config.get("password")
    )
    
    try:
        # Connect to Redis
        await worker.connect()
        
        # Run worker
        max_workers = worker_config.get("worker_concurrency", 2)
        await worker.run(max_workers=max_workers)
    
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    
    except Exception as e:
        logger.error(f"Fatal error: {e}")
    
    finally:
        await worker.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
