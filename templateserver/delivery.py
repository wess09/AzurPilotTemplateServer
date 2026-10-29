"""At-least-once ordered notifications. A network error never discards an event."""

from datetime import timedelta

import httpx
from sqlalchemy import select

from .models import OutboxEvent
from .store import lock, utcnow


class Delivery:
    def __init__(self, store, config, client=None):
        self.store, self.config = store, config
        self.client = client or httpx.Client(timeout=10, follow_redirects=False, trust_env=False)

    def close(self):
        self.client.close()

    def run_once(self):
        if not self.config.main_event_url or not self.config.event_token:
            return False
        with self.store.session.begin() as session:
            if lock(session, 3, skip_locked=True) is None:
                return False
            event = session.scalar(
                select(OutboxEvent)
                .where(OutboxEvent.acknowledged_at.is_(None))
                .order_by(OutboxEvent.sequence)
                .limit(1)
            )
            if event is None or event.next_attempt > utcnow():
                return False
            body = {
                "event_id": str(event.event_id),
                "sequence": event.sequence,
                "type": event.event_type,
                "revision": event.revision,
                "payload": event.payload,
                "created_at": event.created_at.isoformat(),
            }
            event.attempts += 1
            try:
                response = self.client.post(
                    self.config.main_event_url,
                    json=body,
                    headers={"Authorization": "Bearer " + self.config.event_token},
                )
                response.raise_for_status()
                if response.json().get("ack") != str(event.event_id):
                    raise ValueError("Missing event acknowledgement")
            except (httpx.HTTPError, ValueError):
                event.last_error = "Delivery failed or acknowledgement missing"
                event.next_attempt = utcnow() + timedelta(seconds=min(300, 2 ** min(event.attempts, 8)))
                return False
            event.acknowledged_at, event.last_error = utcnow(), ""
            return True
