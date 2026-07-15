"""Order processing tasks — Celery async workers."""

import structlog
from celery import shared_task

from app.services.order_service import fulfill_order, notify_customer

logger = structlog.get_logger()

TOPIC = "order.events"


@shared_task(bind=True, max_retries=3)
def process_order(self, order_id: str):
    """Process a newly created order — validate inventory and fulfill."""
    logger.info("processing_order", order_id=order_id)
    try:
        fulfill_order(order_id)
        notify_customer(order_id, status="processing")
    except Exception as exc:
        logger.error("order_processing_failed", order_id=order_id, error=str(exc))
        self.retry(exc=exc, countdown=60)


@shared_task
def send_order_confirmation(order_id: str, email: str):
    """Send order confirmation email to customer."""
    logger.info("sending_confirmation", order_id=order_id, email=email)
    # Email sending logic here


@shared_task
def cleanup_expired_orders():
    """Periodic task to clean up orders that have been pending too long."""
    logger.info("cleaning_expired_orders")
    # Cleanup logic here
