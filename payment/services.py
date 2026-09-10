from __future__ import annotations

import base64
import logging
from io import BytesIO

import qrcode
import requests
from django.conf import settings
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


logger = logging.getLogger(__name__)


def normalize_phone_number(phone: str) -> str:
    digits = ''.join(character for character in (phone or '') if character.isdigit())

    if not digits:
        return ''

    if digits.startswith('0'):
        return '233' + digits[1:]

    if digits.startswith('233'):
        return digits

    return '233' + digits


def pretty_phone_formats(phone_normalized: str) -> dict:
    """Return dict with 'international' and 'local' pretty formats.

    Examples:
        phone_normalized='233535022447' ->
            {'international': '+233 535 022 447', 'local': '053 502 2447'}
    """
    if not phone_normalized:
        return {'international': '', 'local': ''}

    digits = ''.join(ch for ch in phone_normalized if ch.isdigit())
    if digits.startswith('233'):
        rest = digits[3:]
        # local format: leading 0 + rest
        local = '0' + rest
        # group local as 3-3-4 if length 10
        if len(local) == 10:
            local_pretty = f"{local[0:3]} {local[3:6]} {local[6:10]}"
        else:
            # fallback: simple grouping every 3
            local_pretty = ' '.join([local[i:i+3] for i in range(0, len(local), 3)])

        # international pretty: +233 + grouped rest
        if len(rest) == 9:
            intl_pretty = f"+233 {rest[0:3]} {rest[3:6]} {rest[6:9]}"
        else:
            intl_pretty = '+233 ' + ' '.join([rest[i:i+3] for i in range(0, len(rest), 3)])

        return {'international': intl_pretty, 'local': local_pretty}

    # If input isn't starting with country code, return raw
    return {'international': phone_normalized, 'local': phone_normalized}


def build_qr_code_data_uri(payload: str) -> str:
    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(payload)
    qr.make(fit=True)

    image = qr.make_image(fill_color='black', back_color='white')
    buffer = BytesIO()
    image.save(buffer, format='PNG')
    encoded = base64.b64encode(buffer.getvalue()).decode('utf-8')
    return f'data:image/png;base64,{encoded}'


def build_receipt_pdf(transaction) -> bytes:
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'ReceiptTitle',
        parent=styles['Title'],
        fontName='Helvetica-Bold',
        fontSize=20,
        leading=24,
        textColor=colors.HexColor('#1f2937'),
        alignment=1,
    )
    subtitle_style = ParagraphStyle(
        'ReceiptSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        leading=13,
        textColor=colors.HexColor('#6b7280'),
        alignment=1,
    )
    label_style = ParagraphStyle(
        'ReceiptLabel',
        parent=styles['BodyText'],
        fontName='Helvetica-Bold',
        fontSize=11,
        leading=14,
        textColor=colors.HexColor('#111827'),
    )
    value_style = ParagraphStyle(
        'ReceiptValue',
        parent=styles['BodyText'],
        fontName='Helvetica',
        fontSize=11,
        leading=14,
        textColor=colors.HexColor('#111827'),
    )

    story = [
        Paragraph('FuelSync Receipt', title_style),
        Spacer(1, 6),
        Paragraph('Payment confirmation generated from your transaction record.', subtitle_style),
        Spacer(1, 18),
    ]

    created_at = timezone.localtime(transaction.created_at)
    customer = getattr(transaction, 'customer', None)
    payment = getattr(transaction, 'payment', None)
    phone = getattr(customer, 'phone', '') or getattr(payment, 'customer_phone', '') or 'N/A'
    amount = getattr(transaction, 'total_amount', None)
    if amount is None:
        amount = getattr(payment, 'amount', 0)
    reference = transaction.reference_number or getattr(payment, 'reference', '')
    details = [
        [Paragraph('Phone', label_style), Paragraph(str(phone), value_style)],
        [Paragraph('Amount', label_style), Paragraph(f'GHS {amount:.2f}', value_style)],
        [Paragraph('Reference', label_style), Paragraph(reference, value_style)],
        [Paragraph('Status', label_style), Paragraph(transaction.status.title(), value_style)],
        [Paragraph('Date', label_style), Paragraph(created_at.strftime('%d %b %Y, %I:%M %p'), value_style)],
    ]

    table = Table(details, colWidths=[35 * mm, 120 * mm], hAlign='LEFT')
    table.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.75, colors.HexColor('#d1d5db')),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e5e7eb')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))

    story.append(table)
    story.append(Spacer(1, 18))
    story.append(Paragraph('Keep this receipt for your records.', subtitle_style))

    document.build(story)
    return buffer.getvalue()


def send_sms_message(phone: str, message: str) -> tuple[bool, str]:
    """Send SMS using the configured provider.

    Returns (success, provider_message).
    """
    sms_enabled = bool(getattr(settings, 'SMS_ENABLED', False))
    if not sms_enabled:
        return False, 'SMS is disabled in settings.'

    provider = (getattr(settings, 'SMS_PROVIDER', 'twilio') or 'twilio').strip().lower()
    account_sid = (getattr(settings, 'TWILIO_ACCOUNT_SID', '') or '').strip()
    auth_token = (getattr(settings, 'TWILIO_AUTH_TOKEN', '') or '').strip()
    from_number = (getattr(settings, 'TWILIO_FROM_NUMBER', '') or '').strip()
    sender_id = (getattr(settings, 'SMS_SENDER_ID', '') or '').strip()

    normalized_phone = normalize_phone_number(phone)
    if not normalized_phone:
        return False, 'Recipient phone number is missing.'

    to_number = f'+{normalized_phone}'

    if provider == 'twilio':
        if not account_sid or not auth_token or not from_number:
            return False, 'Twilio credentials are not configured.'

        endpoint = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json'

        try:
            response = requests.post(
                endpoint,
                data={
                    'To': to_number,
                    'From': from_number,
                    'Body': message,
                },
                auth=(account_sid, auth_token),
                timeout=15,
            )
        except requests.RequestException as error:
            logger.warning('SMS send request failed: %s', error)
            return False, str(error)

        if response.status_code >= 400:
            logger.warning('SMS provider rejected message: %s %s', response.status_code, response.text)
            return False, f'HTTP {response.status_code}'

        return True, 'sent'

    if provider == 'hubtel':
        client_id = (getattr(settings, 'HUBTEL_CLIENT_ID', '') or '').strip()
        client_secret = (getattr(settings, 'HUBTEL_CLIENT_SECRET', '') or '').strip()
        hubtel_url = (getattr(settings, 'HUBTEL_SMS_URL', '') or '').strip() or 'https://sms.hubtel.com/v1/messages/send'

        if not client_id or not client_secret:
            return False, 'Hubtel credentials are not configured.'
        if not sender_id:
            return False, 'SMS_SENDER_ID is required for Hubtel.'

        payload = {
            'From': sender_id,
            'To': to_number,
            'Content': message,
            'RegisteredDelivery': True,
        }

        try:
            response = requests.post(
                hubtel_url,
                json=payload,
                auth=(client_id, client_secret),
                headers={'Content-Type': 'application/json'},
                timeout=15,
            )
        except requests.RequestException as error:
            logger.warning('Hubtel SMS send request failed: %s', error)
            return False, str(error)

        if response.status_code >= 400:
            logger.warning('Hubtel rejected message: %s %s', response.status_code, response.text)
            return False, f'HTTP {response.status_code}'

        return True, 'sent'

    if provider == 'arkesel':
        api_key = (getattr(settings, 'ARKESEL_API_KEY', '') or '').strip()
        arkesel_url = (getattr(settings, 'ARKESEL_SMS_URL', '') or '').strip() or 'https://sms.arkesel.com/api/v2/sms/send'

        if not api_key:
            return False, 'Arkesel API key is not configured.'
        if not sender_id:
            return False, 'SMS_SENDER_ID is required for Arkesel.'

        payload = {
            'sender': sender_id,
            'message': message,
            'recipients': [to_number],
        }

        try:
            response = requests.post(
                arkesel_url,
                json=payload,
                headers={
                    'Content-Type': 'application/json',
                    'api-key': api_key,
                },
                timeout=15,
            )
        except requests.RequestException as error:
            logger.warning('Arkesel SMS send request failed: %s', error)
            return False, str(error)

        if response.status_code >= 400:
            logger.warning('Arkesel rejected message: %s %s', response.status_code, response.text)
            return False, f'HTTP {response.status_code}'

        return True, 'sent'

    return False, f'Unsupported SMS provider: {provider}'
