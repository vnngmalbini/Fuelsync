from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction as db_transaction
from django.utils import timezone

from .models import SMSDeliveryLog
from .notifications import record_payment_audit
from .services import send_sms_message


logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=0)
def process_sms_delivery(self, sms_delivery_log_id):
    """Deliver one SMS log entry and retry it if the provider fails temporarily."""
    try:
        with db_transaction.atomic():
            log = SMSDeliveryLog.objects.select_for_update(of=('self',)).select_related('transaction', 'transaction__payment', 'transaction__customer').get(pk=sms_delivery_log_id)

            if log.status == 'sent':
                return {'status': 'sent', 'log_id': log.id}

            if log.status == 'processing':
                return {'status': 'processing', 'log_id': log.id}

            if log.attempt_count >= log.max_attempts:
                log.status = 'failed'
                log.error_message = log.error_message or 'Maximum retry attempts reached.'
                log.save(update_fields=['status', 'error_message', 'updated_at'])
                record_payment_audit(
                    'sms_failed',
                    'SMS delivery failed after maximum retry attempts.',
                    transaction=log.transaction,
                    reference=log.reference,
                    metadata={'sms_delivery_log_id': log.id, 'provider': log.provider, 'attempts': log.attempt_count},
                )
                return {'status': 'failed', 'log_id': log.id}

            log.status = 'processing'
            log.attempt_count += 1
            log.save(update_fields=['status', 'attempt_count', 'updated_at'])

        success, provider_message = send_sms_message(log.recipient_phone, log.message)

        if success:
            with db_transaction.atomic():
                log = SMSDeliveryLog.objects.select_for_update().get(pk=sms_delivery_log_id)
                log.status = 'sent'
                log.sent_at = timezone.now()
                log.error_message = ''
                log.provider_response = {'provider_message': provider_message}
                log.save(update_fields=['status', 'sent_at', 'error_message', 'provider_response', 'updated_at'])

            record_payment_audit(
                'sms_sent',
                'SMS delivery completed successfully.',
                transaction=log.transaction,
                reference=log.reference,
                metadata={'sms_delivery_log_id': log.id, 'provider': log.provider, 'recipient_phone': log.recipient_phone},
            )
            return {'status': 'sent', 'log_id': log.id}

        retry_delay_seconds = min(30 * (2 ** max(log.attempt_count - 1, 0)), 300)

        with db_transaction.atomic():
            log = SMSDeliveryLog.objects.select_for_update().get(pk=sms_delivery_log_id)
            log.error_message = provider_message
            log.next_retry_at = timezone.now() + timedelta(seconds=retry_delay_seconds)
            if log.attempt_count < log.max_attempts:
                log.status = 'retrying'
            else:
                log.status = 'failed'
            log.save(update_fields=['error_message', 'next_retry_at', 'status', 'updated_at'])

        if log.attempt_count < log.max_attempts:
            record_payment_audit(
                'sms_retry',
                'SMS delivery scheduled for retry.',
                transaction=log.transaction,
                reference=log.reference,
                metadata={'sms_delivery_log_id': log.id, 'provider': log.provider, 'attempts': log.attempt_count, 'retry_delay_seconds': retry_delay_seconds},
            )
            process_sms_delivery.apply_async(args=[log.id], countdown=retry_delay_seconds)
            return {'status': 'retrying', 'log_id': log.id}

        record_payment_audit(
            'sms_failed',
            'SMS delivery failed and will not be retried.',
            transaction=log.transaction,
            reference=log.reference,
            metadata={'sms_delivery_log_id': log.id, 'provider': log.provider, 'attempts': log.attempt_count},
        )
        return {'status': 'failed', 'log_id': log.id}

    except SMSDeliveryLog.DoesNotExist:
        logger.warning('SMS delivery log %s not found', sms_delivery_log_id)
        return {'status': 'missing', 'log_id': sms_delivery_log_id}
    except Exception as error:
        logger.exception('Unexpected error while processing SMS delivery %s: %s', sms_delivery_log_id, error)
        return {'status': 'error', 'log_id': sms_delivery_log_id, 'error': str(error)}