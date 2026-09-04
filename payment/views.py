import requests
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.mail import EmailMessage
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.db import transaction as db_transaction
from dashboard.permissions import role_required
from dashboard.models import refresh_dashboard_summary, refresh_daily_sales_report
from station.models import Transaction, FuelType, FuelInventory, Pump, Customer, Payment
from .notifications import (
    build_payment_confirmation_message,
    build_test_sms_message,
    get_sms_provider_status,
    queue_sms_delivery,
    record_payment_audit,
)
from .services import build_qr_code_data_uri, build_receipt_pdf, normalize_phone_number
from .services import pretty_phone_formats
from .services import send_sms_message
import json
import logging
import hmac
import hashlib
import uuid
import threading
from decimal import Decimal

logger = logging.getLogger(__name__)


def _dispatch_receipt_async(transaction, paystack_data, request=None):
    """Fire receipt dispatch in a daemon thread so the HTTP response is not delayed."""
    ref = getattr(transaction, 'reference_number', '')
    def _run():
        try:
            _dispatch_receipt_to_payment_contact(transaction, paystack_data, request=request)
        except Exception as exc:
            logger.warning('Async receipt dispatch failed for %s: %s', ref, exc)
    t = threading.Thread(target=_run, daemon=True)
    t.start()


def _first_operational_pump():
    try:
        return Pump.objects.filter(status='operational').first()
    except Exception:
        return None


def _first_non_lpg_fuel_type():
    try:
        return FuelType.objects.exclude(name__iexact='lpg').first()
    except Exception:
        return None


def _clean_text(value):
    return str(value).strip() if value not in (None, '') else ''


def _infer_momo_provider(phone_normalized):
    """Infer Ghana MoMo network for Paystack from known phone prefixes."""
    digits = ''.join(ch for ch in str(phone_normalized or '') if ch.isdigit())
    if not digits:
        return ''

    if digits.startswith('233'):
        local = '0' + digits[3:]
    elif digits.startswith('0'):
        local = digits
    else:
        local = digits

    prefix = local[:3]
    prefix_map = {
        'mtn': {'024', '025', '053', '054', '055', '059'},
        'vod': {'020', '050'},
        # AirtelTigo numbers are typically routed via Tigo in Paystack provider naming.
        'tgo': {'026', '027', '056', '057'},
    }

    for provider, prefixes in prefix_map.items():
        if prefix in prefixes:
            return provider

    return ''


def _build_mobile_money_payload(phone_normalized):
    payload = {
        'phone': phone_normalized,
    }
    provider = _infer_momo_provider(phone_normalized)
    if provider:
        payload['provider'] = provider
    return payload


def _split_customer_name(full_name, fallback_phone=''):
    candidate = _clean_text(full_name)
    if not candidate:
        candidate = 'Customer'

    parts = [part for part in candidate.split() if part]
    if not parts:
        parts = ['Customer']

    first_name = parts[0]
    last_name = ' '.join(parts[1:]).strip()

    return first_name, last_name


def _extract_customer_details_from_payload(payload):
    payload = payload if isinstance(payload, dict) else {}
    metadata = payload.get('metadata') if isinstance(payload.get('metadata'), dict) else {}
    customer = payload.get('customer') if isinstance(payload.get('customer'), dict) else {}

    customer_name = _clean_text(
        metadata.get('customer_name')
        or metadata.get('full_name')
        or metadata.get('name')
        or customer.get('name')
        or customer.get('customer_name')
    )
    customer_email = _clean_text(
        metadata.get('customer_email')
        or metadata.get('email')
        or customer.get('email')
    )
    customer_phone = _clean_text(
        metadata.get('customer_phone')
        or metadata.get('phone')
        or metadata.get('phone_display')
        or customer.get('phone')
    )
    if not customer_phone:
        customer_phone = _phone_from_paystack_verify_data(payload)

    first_name = _clean_text(metadata.get('customer_first_name') or customer.get('first_name'))
    last_name = _clean_text(metadata.get('customer_last_name') or customer.get('last_name'))

    if not customer_name and (first_name or last_name):
        customer_name = ' '.join(part for part in [first_name, last_name] if part).strip()

    return {
        'customer_name': customer_name,
        'customer_email': customer_email,
        'customer_phone': customer_phone,
        'first_name': first_name,
        'last_name': last_name,
    }


def _resolve_customer_record(phone, customer_name=None, customer_email=None, first_name=None, last_name=None):
    normalized_phone = normalize_phone_number(phone)
    if not normalized_phone:
        return None, {
            'customer_name': _clean_text(customer_name),
            'customer_email': _clean_text(customer_email),
            'customer_phone': '',
            'first_name': _clean_text(first_name),
            'last_name': _clean_text(last_name),
        }

    existing_customer = Customer.objects.filter(phone=normalized_phone).first()
    resolved_name = _clean_text(customer_name)
    resolved_email = _clean_text(customer_email)

    if not resolved_name and (first_name or last_name):
        resolved_name = ' '.join(part for part in [_clean_text(first_name), _clean_text(last_name)] if part).strip()

    if not resolved_name and existing_customer:
        resolved_name = ' '.join(part for part in [existing_customer.first_name, existing_customer.last_name] if part).strip()

    if not resolved_name:
        resolved_name = 'Customer'

    resolved_first, resolved_last = _split_customer_name(resolved_name, normalized_phone)

    if not resolved_email and existing_customer and existing_customer.email:
        resolved_email = existing_customer.email
    if not resolved_email:
        resolved_email = f'{normalized_phone}@fuelsync.com'

    customer, _ = Customer.objects.get_or_create(
        phone=normalized_phone,
        defaults={
            'first_name': resolved_first,
            'last_name': resolved_last,
            'email': resolved_email,
        },
    )

    update_fields = []
    if resolved_first and customer.first_name != resolved_first:
        customer.first_name = resolved_first
        update_fields.append('first_name')
    if resolved_last and customer.last_name != resolved_last:
        customer.last_name = resolved_last
        update_fields.append('last_name')
    if resolved_email and customer.email != resolved_email:
        customer.email = resolved_email
        update_fields.append('email')

    if update_fields:
        customer.save(update_fields=update_fields)

    return customer, {
        'customer_name': ' '.join(part for part in [customer.first_name, customer.last_name] if part).strip(),
        'customer_email': customer.email or resolved_email,
        'customer_phone': normalized_phone,
        'first_name': customer.first_name,
        'last_name': customer.last_name,
    }


def _fuel_type_from_name(fuel_type_name):
    if not fuel_type_name:
        return None
    try:
        return FuelType.objects.filter(name__iexact=str(fuel_type_name).strip()).first()
    except Exception:
        return None


def _infer_liters_from_amount(amount, fuel_type_name=None):
    """Infer liters when metadata is missing but fuel type pricing is known."""
    try:
        amount_decimal = Decimal(str(amount or 0))
    except Exception:
        return None

    if amount_decimal <= 0:
        return None

    fuel_type = _fuel_type_from_name(fuel_type_name) or _first_non_lpg_fuel_type()
    if not fuel_type or not getattr(fuel_type, 'current_price', None):
        return None

    try:
        price = Decimal(str(fuel_type.current_price))
    except Exception:
        return None

    if price <= 0:
        return None

    return (amount_decimal / price).quantize(Decimal('0.01'))


def _apply_transaction_stock_and_pump_delta(fuel_type, pump, liters_delta, amount_delta):
    """Apply signed inventory and pump metric deltas for a completed sale."""
    if liters_delta == 0 and amount_delta == 0:
        return

    with db_transaction.atomic():
        if fuel_type and liters_delta != 0:
            try:
                inventory = FuelInventory.objects.select_for_update().get(fuel_type=fuel_type)
                # Positive sale delta reduces stock; negative delta restores stock.
                inventory.quantity_liters -= liters_delta
                inventory.save(update_fields=['quantity_liters'])
            except FuelInventory.DoesNotExist:
                logger.warning('Fuel inventory missing for fuel type id=%s', getattr(fuel_type, 'id', None))

        if pump and (liters_delta != 0 or amount_delta != 0):
            locked_pump = Pump.objects.select_for_update().filter(id=pump.id).first()
            if locked_pump:
                locked_pump.total_liters_dispensed += liters_delta
                locked_pump.total_revenue += amount_delta
                locked_pump.save(update_fields=['total_liters_dispensed', 'total_revenue'])


def _upsert_dashboard_transaction(reference, phone, amount, paystack_data=None, liters=None, fuel_type_name=None, customer_name=None, customer_email=None):
    """Save a successful Paystack payment into the local transaction table."""
    if not reference:
        return None

    normalized_phone = str(phone or '').strip() or None
    payload_customer = _extract_customer_details_from_payload(paystack_data)
    customer, customer_details = _resolve_customer_record(
        normalized_phone,
        customer_name=customer_name or payload_customer.get('customer_name'),
        customer_email=customer_email or payload_customer.get('customer_email'),
        first_name=payload_customer.get('first_name'),
        last_name=payload_customer.get('last_name'),
    )

    pump = _first_operational_pump()
    fuel_type = _fuel_type_from_name(fuel_type_name) or _first_non_lpg_fuel_type()
    transaction_id = f"PAY-{str(reference).replace('-', '')[:12].upper()}"
    amount_decimal = Decimal(str(amount or 0))
    liters_decimal = Decimal(str(liters or 0))
    price_per_liter = (amount_decimal / liters_decimal) if liters_decimal > 0 else Decimal('0')

    previous_liters = Decimal('0')
    previous_amount = Decimal('0')
    previous_status = None
    previous_pump = None
    previous_fuel_type = None

    txn, created = Transaction.objects.get_or_create(
        reference_number=reference,
        defaults={
            'transaction_id': transaction_id,
            'pump': pump,
            'customer': customer,
            'fuel_type': fuel_type,
            'liters_dispensed': liters_decimal,
            'price_per_liter': price_per_liter,
            'total_amount': amount_decimal,
            'payment_method': 'paystack',
            'status': 'completed',
            'staff': None,
            'completed_at': timezone.now(),
        },
    )

    if not created:
        previous_liters = txn.liters_dispensed or Decimal('0')
        previous_amount = txn.total_amount or Decimal('0')
        previous_status = txn.status
        previous_pump = txn.pump
        previous_fuel_type = txn.fuel_type

        update_fields = []
        if customer and txn.customer_id != customer.id:
            txn.customer = customer
            update_fields.append('customer')
        if pump and txn.pump_id != pump.id:
            txn.pump = pump
            update_fields.append('pump')
        if fuel_type and txn.fuel_type_id != fuel_type.id:
            txn.fuel_type = fuel_type
            update_fields.append('fuel_type')
        if liters_decimal and txn.liters_dispensed != liters_decimal:
            txn.liters_dispensed = liters_decimal
            update_fields.append('liters_dispensed')
        if price_per_liter and txn.price_per_liter != price_per_liter:
            txn.price_per_liter = price_per_liter
            update_fields.append('price_per_liter')
        if txn.total_amount != amount_decimal:
            txn.total_amount = amount_decimal
            update_fields.append('total_amount')
        if txn.payment_method != 'paystack':
            txn.payment_method = 'paystack'
            update_fields.append('payment_method')
        if txn.status != 'completed':
            txn.status = 'completed'
            update_fields.append('status')
        if not txn.completed_at:
            txn.completed_at = timezone.now()
            update_fields.append('completed_at')

        if update_fields:
            txn.save(update_fields=update_fields)

    payment_defaults = {
        'amount': amount_decimal,
        'payment_method': 'paystack',
        'status': 'completed',
        'customer_name': customer_details.get('customer_name', ''),
        'customer_email': customer_details.get('customer_email', ''),
        'customer_phone': customer_details.get('customer_phone', normalized_phone or ''),
        'paid_at': timezone.now(),
        'provider_response': paystack_data or {},
    }
    Payment.objects.update_or_create(
        transaction=txn,
        defaults={
            **payment_defaults,
            'reference': reference,
        },
    )

    try:
        record_payment_audit(
            'payment_saved',
            'Payment stored from Paystack confirmation.',
            transaction=txn,
            payment=getattr(txn, 'payment', None),
            reference=reference,
            metadata={'provider': 'paystack', 'amount': float(amount_decimal), 'phone': normalized_phone},
        )
    except Exception:
        logger.exception('Failed to record payment audit for reference %s', reference)

    # Keep inventory and pump analytics in sync with completed Paystack transactions.
    # This must never block Payment persistence.
    liters_delta = Decimal('0')
    amount_delta = Decimal('0')
    delta_pump = txn.pump or previous_pump
    delta_fuel_type = txn.fuel_type or previous_fuel_type

    allocation_changed = (
        not created
        and previous_status == 'completed'
        and txn.status == 'completed'
        and (
            getattr(previous_pump, 'id', None) != getattr(txn.pump, 'id', None)
            or getattr(previous_fuel_type, 'id', None) != getattr(txn.fuel_type, 'id', None)
        )
    )

    if created:
        if txn.status == 'completed':
            liters_delta = txn.liters_dispensed or Decimal('0')
            amount_delta = txn.total_amount or Decimal('0')
    elif allocation_changed:
        # A webhook may arrive before browser verification and initially use
        # fallback fuel/pump data. Reverse that allocation before applying the
        # verified selection so inventory and pump revenue are not duplicated.
        try:
            _apply_transaction_stock_and_pump_delta(
                previous_fuel_type,
                previous_pump,
                -(previous_liters or Decimal('0')),
                -(previous_amount or Decimal('0')),
            )
        except Exception as reverse_error:
            logger.exception('Previous payment allocation reversal failed for reference %s: %s', reference, reverse_error)
        liters_delta = txn.liters_dispensed or Decimal('0')
        amount_delta = txn.total_amount or Decimal('0')
    else:
        if previous_status != 'completed' and txn.status == 'completed':
            liters_delta = txn.liters_dispensed or Decimal('0')
            amount_delta = txn.total_amount or Decimal('0')
        elif previous_status == 'completed' and txn.status == 'completed':
            liters_delta = (txn.liters_dispensed or Decimal('0')) - previous_liters
            amount_delta = (txn.total_amount or Decimal('0')) - previous_amount

    try:
        _apply_transaction_stock_and_pump_delta(delta_fuel_type, delta_pump, liters_delta, amount_delta)
    except Exception as sync_error:
        logger.exception('Post-payment stock/pump sync failed for reference %s: %s', reference, sync_error)

    try:
        summary_date = timezone.localdate(txn.created_at or timezone.now())
        refresh_dashboard_summary(summary_date)
        refresh_daily_sales_report(summary_date)
    except Exception as summary_error:
        logger.exception('Failed to refresh dashboard summary for reference %s: %s', reference, summary_error)

    # Update customer's cached totals so the Customers page reflects payments.
    # We apply the same delta we used for pump/inventory so values stay consistent.
    try:
        if customer:
            # Ensure Decimal types
            from decimal import Decimal as _D
            amt_delta = _D(str(amount_delta or 0))
            ltr_delta = _D(str(liters_delta or 0))
            if amt_delta != _D('0') or ltr_delta != _D('0'):
                customer.total_purchases = (customer.total_purchases or _D('0')) + amt_delta
                customer.total_liters = (customer.total_liters or _D('0')) + ltr_delta
                customer.save(update_fields=['total_purchases', 'total_liters'])
    except Exception as cust_err:
        logger.exception('Failed to update customer totals for phone=%s reference=%s: %s', normalized_phone, reference, cust_err)
    return txn


def _extract_payer_contacts(data):
    """Extract payer phone/email from Paystack payload safely."""
    if not isinstance(data, dict):
        return "", ""

    phone = _phone_from_paystack_verify_data(data)

    customer = data.get('customer') if isinstance(data.get('customer'), dict) else {}
    email = customer.get('email') or ""

    if not email:
        metadata = data.get('metadata') if isinstance(data.get('metadata'), dict) else {}
        email = metadata.get('email') or metadata.get('customer_email') or ""

    return str(phone or "").strip(), str(email or "").strip()


def _send_receipt_email(transaction, recipient_email):
    """Send PDF receipt via email. Returns True when sent without exception."""
    if not recipient_email:
        return False

    if recipient_email.lower().endswith('@fuelsync.com'):
        # Skip synthetic fallback emails generated from phone numbers.
        return False

    pdf_bytes = build_receipt_pdf(transaction)
    subject = f"FuelSync Receipt - {transaction.reference_number}"
    body = (
        "Your payment was successful. "
        "Please find your receipt attached as PDF."
    )
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', None) or 'no-reply@fuelsync.com'

    message = EmailMessage(
        subject=subject,
        body=body,
        from_email=from_email,
        to=[recipient_email],
    )
    message.attach(
        filename=f"receipt-{transaction.reference_number}.pdf",
        content=pdf_bytes,
        mimetype='application/pdf',
    )

    try:
        message.send(fail_silently=False)
        return True
    except Exception as error:
        logger.warning('Receipt email send failed for %s: %s', transaction.reference_number, error)
        return False


def _build_public_receipt_url(reference, request=None):
    path = reverse('payment_receipt', args=[reference])
    if request is not None:
        try:
            return request.build_absolute_uri(path)
        except Exception:
            pass

    site_base_url = (getattr(settings, 'SITE_BASE_URL', '') or '').rstrip('/')
    if site_base_url:
        return f"{site_base_url}{path}"

    return path


def _send_receipt_sms(transaction, phone, receipt_url):
    if not phone:
        return False, 'No payer phone found in payment metadata.'

    amount_text = f"GHS {float(transaction.total_amount):.2f}"
    message = (
        f"FuelSync receipt for payment {transaction.reference_number} ({amount_text}). "
        f"View/download: {receipt_url}"
    )
    return send_sms_message(phone, message)


def _dispatch_payment_confirmation_to_payment_contact(transaction, paystack_data=None):
    payment = getattr(transaction, 'payment', None)
    phone = ''
    customer_name = ''
    customer_email = ''

    if payment and getattr(payment, 'customer_phone', None):
        phone = payment.customer_phone
        customer_name = payment.customer_name or ''
        customer_email = payment.customer_email or ''
    elif transaction.customer and getattr(transaction.customer, 'phone', None):
        phone = transaction.customer.phone
        customer_name = ' '.join(part for part in [transaction.customer.first_name, transaction.customer.last_name] if part).strip()
        customer_email = transaction.customer.email or ''
    elif isinstance(paystack_data, dict):
        customer_payload = _extract_customer_details_from_payload(paystack_data)
        phone = customer_payload.get('customer_phone') or ''
        customer_name = customer_payload.get('customer_name') or ''
        customer_email = customer_payload.get('customer_email') or ''

    if not phone:
        record_payment_audit(
            'sms_failed',
            'Payment confirmation SMS could not be queued because no recipient phone was available.',
            transaction=transaction,
            payment=payment,
            reference=transaction.reference_number,
            metadata={'reason': 'missing_phone'},
        )
        return

    message = build_payment_confirmation_message(transaction)
    log, queued = queue_sms_delivery(
        transaction=transaction,
        reference=transaction.reference_number,
        recipient_phone=phone,
        customer_name=customer_name,
        customer_email=customer_email,
        message=message,
        purpose='payment_confirmation',
        metadata={'source': 'paystack_payment_confirmation'},
    )

    # Save confirmation tracking state on Payment.provider_response (Transaction has no such field)
    payment = getattr(transaction, 'payment', None)
    if payment:
        provider_response = getattr(payment, 'provider_response', None) or {}
        confirmation_state = provider_response.get('sms_confirmation', {})
        confirmation_state.update({
            'attempted_at': timezone.localtime(timezone.now()).isoformat(),
            'phone': phone,
            'customer_name': customer_name,
            'message': message,
            'queued': bool(queued),
            'sms_delivery_log_id': getattr(log, 'id', None),
        })
        provider_response['sms_confirmation'] = confirmation_state
        payment.provider_response = provider_response
        payment.save(update_fields=['provider_response'])


def _dispatch_receipt_to_payment_contact(transaction, paystack_data, request=None):
    """Best-effort dispatch of receipt to the payer contact used for payment."""
    # Use Payment.provider_response for tracking state (Transaction has no such field)
    payment = getattr(transaction, 'payment', None)
    provider_response = (getattr(payment, 'provider_response', None) or {}) if payment else {}
    receipt_dispatch = provider_response.get('receipt_dispatch', {})

    # Avoid duplicate sends if verify/webhook are both called.
    try:
        _dispatch_payment_confirmation_to_payment_contact(transaction, paystack_data=paystack_data)
    except Exception as sms_error:
        ref = getattr(transaction, 'reference_number', '')
        logger.exception('Failed to send payment confirmation SMS for reference %s: %s', ref, sms_error)

    if receipt_dispatch.get('email_sent') or receipt_dispatch.get('sms_sent'):
        return

    phone, email = _extract_payer_contacts(paystack_data)
    receipt_url = _build_public_receipt_url(transaction.reference_number, request=request)

    email_sent = _send_receipt_email(transaction, email)
    sms_sent, sms_message = _send_receipt_sms(transaction, phone, receipt_url)

    receipt_dispatch.update({
        'attempted_at': timezone.localtime(timezone.now()).isoformat(),
        'phone': phone,
        'email': email,
        'receipt_url': receipt_url,
        'email_sent': bool(email_sent),
        'sms_sent': bool(sms_sent),
        'sms_message': sms_message,
        'channel': 'email+sms' if (email_sent and sms_sent) else 'email' if email_sent else 'sms' if sms_sent else 'none',
    })
    if payment:
        provider_response['receipt_dispatch'] = receipt_dispatch
        payment.provider_response = provider_response
        payment.save(update_fields=['provider_response'])


@require_http_methods(["GET"])
def payment_receipt(request, reference):
    """Public receipt endpoint keyed by payment reference for SMS/email links."""
    transaction = get_object_or_404(Transaction, reference_number=reference)
    if transaction.status.lower() != 'completed':
        return render(request, 'failed.html', {'error': 'Receipt is available after a completed payment only.'})

    pdf = build_receipt_pdf(transaction)
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="receipt-{transaction.reference_number}.pdf"'
    return response


def _phone_from_paystack_verify_data(data):
    """Best-effort phone from Paystack verify `data`; avoids KeyError on sparse payloads."""
    if not isinstance(data, dict):
        return ""
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        found = metadata.get("phone") or metadata.get("customer_phone") or metadata.get("phone_display")
        if found:
            return str(found)
    return ""


def _build_paystack_callback_url(request):
    """Build a callback URL that can be overridden for external/mobile access."""
    site_base_url = (getattr(settings, 'SITE_BASE_URL', '') or '').strip().rstrip('/')
    host = (request.get_host() or '').lower()

    # When app is opened on localhost during development, prefer SITE_BASE_URL
    # so external devices can reach the callback URL if configured.
    if site_base_url and ('127.0.0.1' in host or 'localhost' in host):
        return f"{site_base_url}/payment/verify/"

    return request.build_absolute_uri('/payment/verify/')


def _initialize_paystack_transaction(request, amount, phone, liters=None, fuel_type_name=None, customer_name=None, customer_email=None, first_name=None, last_name=None):
    phone_normalized = normalize_phone_number(phone)
    if not amount or not phone_normalized:
        return None, None, None, None

    # Build readable phone/email displays
    phone_formats = pretty_phone_formats(phone_normalized)

    request_customer_name = _clean_text(
        customer_name
        or request.GET.get('customer_name')
        or request.GET.get('name')
        or request.GET.get('full_name')
        or request.POST.get('customer_name')
        or request.POST.get('name')
        or request.POST.get('full_name')
    )
    request_customer_email = _clean_text(
        customer_email
        or request.GET.get('customer_email')
        or request.GET.get('email')
        or request.POST.get('customer_email')
        or request.POST.get('email')
    )
    request_first_name = _clean_text(first_name or request.GET.get('first_name') or request.POST.get('first_name'))
    request_last_name = _clean_text(last_name or request.GET.get('last_name') or request.POST.get('last_name'))

    existing_customer = Customer.objects.filter(phone=phone_normalized).first()
    if not request_customer_name and existing_customer:
        request_customer_name = ' '.join(part for part in [existing_customer.first_name, existing_customer.last_name] if part).strip()
    if not request_customer_email and existing_customer and existing_customer.email:
        request_customer_email = existing_customer.email
    if not request_customer_name and (request_first_name or request_last_name):
        request_customer_name = ' '.join(part for part in [request_first_name, request_last_name] if part).strip()
    if not request_customer_name:
        request_customer_name = 'Customer'
    if not request_customer_email:
        request_customer_email = f'{phone_normalized}@fuelsync.com'

    email = request_customer_email

    # Build an email_display for UI (more readable). If using a real email, show it as-is.
    if email.endswith('@fuelsync.com') and (email.startswith(phone_normalized)):
        email_display = f"{phone_formats.get('local') or phone_normalized}@fuelsync.com"
    else:
        email_display = email

    data = {
        "email": email,
        "amount": int(float(amount) * 100),
        "currency": "GHS",
        "channels": ["mobile_money"],
        "phone": phone_normalized,
        "mobile_money": _build_mobile_money_payload(phone_normalized),
        "callback_url": _build_paystack_callback_url(request),
        "metadata": {
            "customer_name": request_customer_name,
            "customer_email": request_customer_email,
            "customer_phone": phone_normalized,
            "momo_provider": _infer_momo_provider(phone_normalized),
            "customer_first_name": request_first_name,
            "customer_last_name": request_last_name,
            "phone": phone_normalized,
            "phone_display": phone,
            "liters": str(liters or ""),
            "fuel_type": str(fuel_type_name or ""),
        },
        "custom_fields": [
            {"display_name": "Customer Name", "variable_name": "customer_name", "value": request_customer_name},
            {"display_name": "Customer Email", "variable_name": "customer_email", "value": request_customer_email},
            {"display_name": "Customer Phone", "variable_name": "customer_phone", "value": phone_normalized},
        ],
    }

    headers = {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"
    }

    response = requests.post(
        "https://api.paystack.co/transaction/initialize",
        json=data,
        headers=headers,
        timeout=15,
    )

    res = response.json()
    logger.info("Paystack initialize response: %s", res)

    try:
        request.session['paystack_init'] = res
    except Exception:
        logger.warning('Could not save paystack init in session')

    if not res.get("status"):
        return None, None, None, res

    auth_url = res["data"].get("authorization_url")
    reference = res["data"].get("reference")
    amount_in_pesewas = int(float(amount) * 100)
    return {
        "public_key": settings.PAYSTACK_PUBLIC_KEY,
        "email": email,
        "email_display": email_display,
        "customer_name": request_customer_name,
        "customer_email": request_customer_email,
        "customer_phone": phone_normalized,
        "amount": float(amount),
        "amount_in_pesewas": amount_in_pesewas,
        "reference": reference,
        "phone": phone_normalized,
        "phone_display": phone,
        "liters": str(liters or ""),
        "fuel_type": str(fuel_type_name or ""),
        "phone_pretty_local": phone_formats.get('local'),
        "phone_pretty_international": phone_formats.get('international'),
        "access_code": res["data"].get("access_code"),
        "authorization_url": auth_url,
        "qr_code_image": build_qr_code_data_uri(auth_url or res["data"].get("reference", "")),
        "verify_api_url": reverse("verify_payment_api"),
        "dashboard_url": reverse("dashboard"),
    }, res, phone_normalized, reference

@login_required
@role_required('admin', 'attendant')
def initialize_payment(request):
    amount = request.GET.get("amount")
    phone = request.GET.get("phone")
    liters = request.GET.get("liters")
    fuel_type_name = request.GET.get("fuel_type")

    context, res, phone_normalized, reference = _initialize_paystack_transaction(request, amount, phone, liters=liters, fuel_type_name=fuel_type_name)
    if not context:
        if res is None:
            return render(request, "failed.html", {"error": "Amount and phone are required"})
        logger.error("Failed to initialize Paystack transaction: %s", res)
        message = "Failed to initialize payment"
        if isinstance(res, dict):
            message = res.get("message") or message
        return render(request, "failed.html", {"error": message})

    return render(request, "checkout.html", context)


@login_required
@role_required('admin', 'attendant')
def qr_payment(request):
    amount = request.GET.get("amount")
    phone = request.GET.get("phone")
    liters = request.GET.get("liters")
    fuel_type_name = request.GET.get("fuel_type")

    context, res, phone_normalized, reference = _initialize_paystack_transaction(request, amount, phone, liters=liters, fuel_type_name=fuel_type_name)
    if not context:
        if res is None:
            return render(request, "failed.html", {"error": "Amount and phone are required"})
        logger.error("Failed to initialize Paystack transaction (qr): %s", res)
        message = "Failed to initialize payment"
        if isinstance(res, dict):
            message = res.get("message") or message
        return render(request, "failed.html", {"error": message})

    return render(request, "payment.html", {
        "amount": context["amount"],
        "reference": reference,
        "verify_api_url": reverse("verify_payment_api"),
        "dashboard_url": reverse("dashboard"),
        "qr_code": context["qr_code_image"].split(",", 1)[1] if "," in context["qr_code_image"] else context["qr_code_image"],
    })


@require_http_methods(["POST"])
@csrf_exempt
def initialize_payment_api(request):
    """Backend endpoint for pump or AI systems to initialize payment and receive QR data."""
    try:
        payload = json.loads(request.body or "{}")
        amount = payload.get("amount")
        phone = payload.get("phone")
        liters = payload.get("liters")
        fuel_type_name = payload.get("fuel_type")
        customer_name = payload.get("customer_name") or payload.get("name") or payload.get("full_name")
        customer_email = payload.get("customer_email") or payload.get("email")
        customer_first_name = payload.get("first_name")
        customer_last_name = payload.get("last_name")

        phone_normalized = normalize_phone_number(phone)
        if not amount or not phone_normalized:
            return JsonResponse({"status": False, "message": "Amount and phone are required"}, status=400)

        existing_customer = Customer.objects.filter(phone=phone_normalized).first()
        if not customer_name and existing_customer:
            customer_name = ' '.join(part for part in [existing_customer.first_name, existing_customer.last_name] if part).strip()
        if not customer_email and existing_customer and existing_customer.email:
            customer_email = existing_customer.email
        if not customer_name and (customer_first_name or customer_last_name):
            customer_name = ' '.join(part for part in [customer_first_name, customer_last_name] if part).strip()
        if not customer_name:
            customer_name = 'Customer'
        if not customer_email:
            customer_email = f'{phone_normalized}@fuelsync.com'

        email = customer_email
        data = {
            "email": email,
            "amount": int(float(amount) * 100),
            "currency": "GHS",
            "channels": ["mobile_money"],
            "phone": phone_normalized,
            "mobile_money": _build_mobile_money_payload(phone_normalized),
            "callback_url": _build_paystack_callback_url(request),
            "metadata": {
                "customer_name": customer_name,
                "customer_email": customer_email,
                "customer_phone": phone_normalized,
                "momo_provider": _infer_momo_provider(phone_normalized),
                "customer_first_name": customer_first_name or "",
                "customer_last_name": customer_last_name or "",
                "phone": phone_normalized,
                "phone_display": phone,
                "liters": str(liters or ""),
                "fuel_type": str(fuel_type_name or ""),
            },
            "custom_fields": [
                {"display_name": "Customer Name", "variable_name": "customer_name", "value": customer_name},
                {"display_name": "Customer Email", "variable_name": "customer_email", "value": customer_email},
                {"display_name": "Customer Phone", "variable_name": "customer_phone", "value": phone_normalized},
            ],
        }

        headers = {
            "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"
        }

        response = requests.post(
            "https://api.paystack.co/transaction/initialize",
            json=data,
            headers=headers,
            timeout=15,
        )

        res = response.json()
        logger.info("Paystack initialize API response: %s", res)

        if not res.get("status"):
            return JsonResponse({"status": False, "message": res.get("message", "Failed to initialize payment")}, status=400)

        try:
            request.session['paystack_init'] = res
        except Exception:
            logger.warning('Could not save paystack init in session')

        authorization_url = res["data"].get("authorization_url")
        reference = res["data"].get("reference")

        return JsonResponse({
            "status": True,
            "message": "Payment initialized",
            "data": {
                "email": email,
                "customer_name": customer_name,
                "customer_email": customer_email,
                "customer_phone": phone_normalized,
                "amount": float(amount),
                "phone": phone_normalized,
                "liters": liters,
                "fuel_type": fuel_type_name,
                "reference": reference,
                "authorization_url": authorization_url,
                "access_code": res["data"].get("access_code"),
                "qr_code_image": build_qr_code_data_uri(authorization_url or reference or ""),
            },
        })
    except Exception as error:
        logger.error("initialize_payment_api failed: %s", error)
        return JsonResponse({"status": False, "message": str(error)}, status=400)

@require_http_methods(["POST"])
@csrf_exempt
def charge_mobile_money(request):
    """Charge mobile money with OTP verification"""
    try:
        data = json.loads(request.body)
        reference = data.get("reference")
        phone = data.get("phone")
        
        if not reference or not phone:
            return JsonResponse({"status": False, "message": "Missing reference or phone"})

        # Normalize phone number (remove any non-digit characters except +)
        phone_normalized = ''.join(c for c in phone if c.isdigit() or c == '+')
        
        # Ensure it starts with country code
        if phone_normalized.startswith('0'):
            phone_normalized = '233' + phone_normalized[1:]
        elif not phone_normalized.startswith('233'):
            phone_normalized = '233' + phone_normalized

        charge_data = {
            "reference": reference,
            "authorization": {
                "pin": None  # Let Paystack handle PIN prompt
            }
        }

        headers = {
            "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"
        }

        # Attempt to charge the authorization
        response = requests.post(
            "https://api.paystack.co/transaction/charge_authorization",
            json=charge_data,
            headers=headers,
            timeout=10
        )
        
        res = response.json()
        
        if res["status"]:
            return JsonResponse({
                "status": True,
                "message": "Payment initiated successfully",
                "data": res.get("data")
            })
        else:
            error_msg = res.get("message", "Charge failed")
            return JsonResponse({
                "status": False,
                "message": error_msg
            })
            
    except Exception as e:
        return JsonResponse({
            "status": False,
            "message": f"Error: {str(e)}"
        })

@require_http_methods(["GET", "POST"])
def verify_payment(request):
    """Verify payment from Paystack callback."""
    reference = request.GET.get("reference")

    if not reference:
        return render(request, "failed.html", {"error": "No reference provided"})

    headers = {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"
    }

    try:
        response = requests.get(
            f"https://api.paystack.co/transaction/verify/{reference}",
            headers=headers,
            timeout=10
        )
        try:
            res = response.json()
        except ValueError:
            logger.error("Paystack verify response was not valid JSON for %s", reference)
            return render(request, "failed.html", {"error": "Payment verification returned an invalid response."})
        data = res.get("data") if isinstance(res, dict) else None
        if not isinstance(data, dict):
            data = {}

        logger.info(f"Paystack verify response for {reference}: status={res.get('status')}, payment_status={data.get('status')}")

        if res.get("status") and data.get("status") == "success":
            phone = _phone_from_paystack_verify_data(data) or "Unknown"
            amount_raw = data.get("amount")
            metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            liters_raw = metadata.get("liters")
            fuel_type_name = metadata.get("fuel_type")
            if amount_raw is None:
                return render(request, "failed.html", {"error": "Payment verified but amount is missing."})
            try:
                amount = float(amount_raw) / 100
            except (TypeError, ValueError):
                return render(request, "failed.html", {"error": "Payment verified but amount is invalid."})

            try:
                liters = float(liters_raw) if liters_raw not in (None, "") else None
            except (TypeError, ValueError):
                liters = None

            if liters is None:
                inferred_liters = _infer_liters_from_amount(amount, fuel_type_name=fuel_type_name)
                liters = float(inferred_liters) if inferred_liters is not None else None

            record_payment_audit(
                'payment_verified',
                'Paystack transaction verified successfully.',
                reference=reference,
                metadata={'phone': phone, 'amount': amount, 'source': 'verify_payment'},
            )
            transaction = _upsert_dashboard_transaction(reference, phone, amount, data, liters=liters, fuel_type_name=fuel_type_name)
            logger.info(f"Saved local transaction for reference {reference} (id={getattr(transaction, 'id', None)})")

            _dispatch_receipt_async(transaction, data, request=request)

            from urllib.parse import urlencode
            from django.urls import reverse as _reverse
            success_url = _reverse('payment_success') + '?' + urlencode({'reference': reference})
            return HttpResponseRedirect(success_url)
        else:
            status_info = data.get("status", "unknown")
            return render(request, "failed.html", {
                "error": f"Payment not completed. Status: {status_info}"
            })

    except requests.exceptions.RequestException as e:
        return render(request, "failed.html", {
            "error": f"Failed to verify payment. Please try again. Error: {str(e)}"
        })

@require_http_methods(["GET"])
def verify_payment_api(request):
    """API endpoint for AJAX payment verification"""
    reference = request.GET.get("reference")

    if not reference:
        return JsonResponse({"status": False, "message": "No reference provided"})

    headers = {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"
    }

    try:
        response = requests.get(
            f"https://api.paystack.co/transaction/verify/{reference}",
            headers=headers,
            timeout=10
        )
        try:
            res = response.json()
        except ValueError:
            logger.error("Paystack verify API response was not valid JSON for %s", reference)
            return JsonResponse({
                "status": False,
                "message": "Payment verification returned an invalid response."
            }, status=502)
        data = res.get("data") if isinstance(res, dict) else None
        if not isinstance(data, dict):
            data = {}

        if res.get("status") and data.get("status") == "success":
            phone = _phone_from_paystack_verify_data(data) or "Unknown"
            amount_raw = data.get("amount")
            metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            liters_raw = metadata.get("liters")
            fuel_type_name = metadata.get("fuel_type")
            if amount_raw is None:
                return JsonResponse(
                    {"status": False, "message": "Payment verified but amount is missing."},
                    status=502,
                )
            try:
                amount = float(amount_raw) / 100
            except (TypeError, ValueError):
                return JsonResponse(
                    {"status": False, "message": "Payment verified but amount is invalid."},
                    status=502,
                )

            try:
                liters = float(liters_raw) if liters_raw not in (None, "") else None
            except (TypeError, ValueError):
                liters = None

            if liters is None:
                inferred_liters = _infer_liters_from_amount(amount, fuel_type_name=fuel_type_name)
                liters = float(inferred_liters) if inferred_liters is not None else None

            record_payment_audit(
                'payment_verified',
                'Paystack transaction verified successfully via API.',
                reference=reference,
                metadata={'phone': phone, 'amount': amount, 'source': 'verify_payment_api'},
            )
            transaction = _upsert_dashboard_transaction(reference, phone, amount, data, liters=liters, fuel_type_name=fuel_type_name)

            _dispatch_receipt_async(transaction, data, request=request)

            return JsonResponse({
                "status": True,
                "message": "Payment verified successfully",
                "amount": amount,
                "phone": phone,
                "reference": reference
            })
        else:
            status_info = data.get("status", "unknown")
            return JsonResponse({
                "status": False,
                "message": f"Payment status: {status_info}"
            })

    except requests.exceptions.RequestException as e:
        return JsonResponse({
            "status": False,
            "message": f"Failed to verify payment: {str(e)}"
        })
    except Exception as e:
        logger.exception("Unexpected verify_payment_api error for reference %s", reference)
        return JsonResponse({
            "status": False,
            "message": "Unexpected server error while verifying payment."
        }, status=500)


def payment_success(request):
    """Show a branded success page after a confirmed Paystack payment, then redirect to dashboard."""
    reference = request.GET.get('reference', '').strip()

    if not reference:
        return HttpResponseRedirect(reverse('dashboard'))

    transaction = Transaction.objects.filter(reference_number=reference, status='completed').first()

    if not transaction:
        # Reference not found or not completed — try a quick live verify before giving up.
        try:
            headers = {"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"}
            resp = requests.get(
                f"https://api.paystack.co/transaction/verify/{reference}",
                headers=headers,
                timeout=8,
            )
            res = resp.json()
            data = res.get('data') if isinstance(res, dict) else {}
            if isinstance(data, dict) and res.get('status') and data.get('status') == 'success':
                amount_raw = data.get('amount')
                if amount_raw is not None:
                    amount = float(amount_raw) / 100
                    metadata = data.get('metadata') or {}
                    phone = _phone_from_paystack_verify_data(data) or ''
                    liters_raw = metadata.get('liters')
                    try:
                        liters = float(liters_raw) if liters_raw not in (None, '', 'undefined') else None
                    except (TypeError, ValueError):
                        liters = None
                    fuel_type_name = metadata.get('fuel_type') or ''
                    transaction = _upsert_dashboard_transaction(
                        reference, phone, amount, data,
                        liters=liters,
                        fuel_type_name=fuel_type_name if fuel_type_name not in ('undefined', 'null', '') else None,
                    )
        except Exception:
            pass

    if not transaction:
        return HttpResponseRedirect(reverse('dashboard'))

    if transaction.customer:
        fn = transaction.customer.first_name or ''
        ln = transaction.customer.last_name or ''
        customer_name = (f"{fn} {ln}".strip()) if (fn and fn != 'Customer') or ln else ''
        phone = transaction.customer.phone or ''
    else:
        customer_name = ''
        phone = ''

    context = {
        'reference': reference,
        'amount': transaction.total_amount,
        'fuel_type': transaction.fuel_type.name if transaction.fuel_type else '',
        'liters': transaction.liters_dispensed,
        'phone': phone,
        'customer_name': customer_name,
        'redirect_seconds': 6,
        'dashboard_url': reverse('dashboard'),
    }
    return render(request, 'payment_success.html', context)


def debug_init(request):
    """Render the saved Paystack initialize response for debugging."""
    init = request.session.get('paystack_init')
    # pretty-print JSON if available
    pretty = None
    if init:
        try:
            pretty = json.dumps(init, indent=2)
        except Exception:
            pretty = str(init)

    return render(request, 'debug_init.html', { 'init': pretty })


def debug_verify(request):
    """Call Paystack verify for a provided reference and render the raw response."""
    reference = request.GET.get('reference')
    if not reference:
        # try to pull from last initialize in session
        init = request.session.get('paystack_init')
        if init and isinstance(init, dict):
            reference = init.get('data', {}).get('reference')

    result = None
    if reference:
        headers = {"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"}
        try:
            resp = requests.get(f"https://api.paystack.co/transaction/verify/{reference}", headers=headers, timeout=10)
            result = resp.json()
        except Exception as e:
            result = {'error': str(e)}

    pretty = None
    if result:
        try:
            pretty = json.dumps(result, indent=2)
        except Exception:
            pretty = str(result)

    return render(request, 'debug_verify.html', { 'reference': reference, 'result': pretty })


@require_http_methods(["POST"])
@csrf_exempt
def paystack_webhook(request):
    """Receive Paystack webhook POSTs, verify signature, and process events.

    Paystack sends an `x-paystack-signature` header which is the HMAC SHA512
    of the raw request body using your `PAYSTACK_SECRET_KEY`.
    
    This webhook receives payment completion notifications and updates transactions
    in station.models.Transaction (the main transaction model used by dashboard).
    """
    webhook_id = str(uuid.uuid4())[:8]
    logger.info(f"[WEBHOOK-{webhook_id}] ===== WEBHOOK REQUEST RECEIVED =====")
    
    try:
        raw_body = request.body or b""
        signature = request.META.get('HTTP_X_PAYSTACK_SIGNATURE', '')
        
        logger.info(f"[WEBHOOK-{webhook_id}] Raw body length: {len(raw_body)} bytes")
        logger.info(f"[WEBHOOK-{webhook_id}] Signature header present: {bool(signature)}")

        # Compute HMAC SHA512 of the raw body using the secret key
        secret = (settings.PAYSTACK_SECRET_KEY or '').encode('utf-8')
        computed = hmac.new(secret, raw_body, hashlib.sha512).hexdigest()

        if not signature or not hmac.compare_digest(computed, signature):
            logger.error(f"[WEBHOOK-{webhook_id}] ❌ SIGNATURE VALIDATION FAILED")
            logger.error(f"[WEBHOOK-{webhook_id}] Expected: {computed[:16]}...")
            logger.error(f"[WEBHOOK-{webhook_id}] Got:      {signature[:16] if signature else 'NONE'}...")
            record_payment_audit(
                'webhook_rejected',
                'Rejected Paystack webhook with invalid signature.',
                reference='',
                metadata={'webhook_id': webhook_id, 'signature_present': bool(signature)},
            )
            return HttpResponse(status=400)
        
        logger.info(f"[WEBHOOK-{webhook_id}] ✓ Signature validated successfully")

        try:
            payload = json.loads(raw_body.decode('utf-8') or '{}')
        except json.JSONDecodeError as e:
            logger.error(f"[WEBHOOK-{webhook_id}] Failed to parse JSON payload: {e}")
            logger.error(f"[WEBHOOK-{webhook_id}] Raw body: {raw_body[:200]}")
            return HttpResponse(status=400)
        
        event = payload.get('event')
        data = payload.get('data', {})
        
        logger.info(f"[WEBHOOK-{webhook_id}] Event type: {event}")
        logger.debug(f"[WEBHOOK-{webhook_id}] Full payload: {json.dumps(payload, default=str)}")
        record_payment_audit(
            'webhook_verified',
            'Verified Paystack webhook signature.',
            reference=str(data.get('reference') or data.get('id') or ''),
            metadata={'webhook_id': webhook_id, 'event': event},
        )

        # Handle successful transaction events
        reference = data.get('reference') or data.get('id')
        status = data.get('status')
        extracted_customer = _extract_customer_details_from_payload(data)
        
        logger.info(f"[WEBHOOK-{webhook_id}] Transaction reference: {reference}")
        logger.info(f"[WEBHOOK-{webhook_id}] Transaction status from Paystack: {status}")

        if reference and status and status.lower() == 'success':
            logger.info(f"[WEBHOOK-{webhook_id}] ✓ Processing successful payment")
            
            # Extract transaction details from Paystack
            phone = extracted_customer.get('customer_phone') or None
            amount = None
            metadata = data.get('metadata') or {}
            phone = phone or metadata.get('phone') or metadata.get('phone_display')
            
            # Amount is in pesewas (smallest unit), convert to GHS
            amt = data.get('amount')
            if isinstance(amt, (int, float)):
                try:
                    amount = float(amt) / 100.0
                except Exception as e:
                    logger.error(f"[WEBHOOK-{webhook_id}] Failed to parse amount {amt}: {e}")
                    amount = None

            fuel_type_name = metadata.get('fuel_type') or metadata.get('fuel_type_name')
            liters = metadata.get('liters')
            try:
                liters = float(liters) if liters not in (None, '') else None
            except (TypeError, ValueError):
                liters = None
            if liters is None:
                inferred_liters = _infer_liters_from_amount(amount, fuel_type_name=fuel_type_name)
                liters = float(inferred_liters) if inferred_liters is not None else None
            
            logger.info(f"[WEBHOOK-{webhook_id}] Extracted phone: {phone}")
            logger.info(f"[WEBHOOK-{webhook_id}] Extracted customer name: {extracted_customer.get('customer_name')}")
            logger.info(f"[WEBHOOK-{webhook_id}] Extracted customer email: {extracted_customer.get('customer_email')}")
            logger.info(f"[WEBHOOK-{webhook_id}] Extracted amount: {amount} GHS")
            
            if not phone or not amount:
                logger.warning(f"[WEBHOOK-{webhook_id}] ⚠️  Missing phone ({phone}) or amount ({amount})")
                logger.warning(f"[WEBHOOK-{webhook_id}] Full metadata: {json.dumps(metadata, default=str)}")
                logger.info(f"[WEBHOOK-{webhook_id}] Acknowledging webhook anyway (returning 200)")
                return HttpResponse(status=200)
            
            # Use atomic transaction to ensure consistency
            with db_transaction.atomic():
                # Try to get existing transaction by reference_number
                txn = _upsert_dashboard_transaction(
                    reference,
                    phone,
                    amount,
                    data,
                    liters=liters,
                    fuel_type_name=fuel_type_name,
                    customer_name=extracted_customer.get('customer_name'),
                    customer_email=extracted_customer.get('customer_email'),
                )
                logger.info(f"[WEBHOOK-{webhook_id}] ✓ Local transaction upserted: {getattr(txn, 'id', None)}")
                try:
                    if txn is not None:
                        _dispatch_receipt_to_payment_contact(txn, data)
                except Exception as dispatch_err:
                    logger.exception('[WEBHOOK-%s] Failed to dispatch receipt for reference %s: %s', webhook_id, reference, dispatch_err)
        else:
            logger.info(f"[WEBHOOK-{webhook_id}] Skipping non-successful transaction (status: {status})")

        logger.info(f"[WEBHOOK-{webhook_id}] ===== WEBHOOK PROCESSING COMPLETED =====")
        return HttpResponse(status=200)

    except Exception as e:
        logger.exception(f"[WEBHOOK-{webhook_id}] ❌ ERROR processing Paystack webhook: {e}")
        logger.error(f"[WEBHOOK-{webhook_id}] Request body: {request.body}")
        return HttpResponse(status=500)
