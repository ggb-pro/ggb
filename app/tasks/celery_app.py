import os
os.environ.setdefault("CELERY_BROKER_URL", "redis://localhost:6379/1")

from celery import Celery
from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "knspace",
    broker=settings.redis_url.replace("/0", "/1"),  # Use DB 1 for Celery
    backend=settings.redis_url.replace("/0", "/2"),  # Use DB 2 for results,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)
