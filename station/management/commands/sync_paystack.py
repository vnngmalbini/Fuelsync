"""Management command: sync successful Paystack transactions into the local DB."""

from datetime import datetime
from django.core.management.base import BaseCommand
from django.conf import settings
import requests

from payment.views import _upsert_dashboard_transaction
from station.models import Transaction


class Command(BaseCommand):
    help = 'Pull successful Paystack transactions and save them to the local DB.'

    def add_arguments(self, parser):
        parser.add_argument('--pages', type=int, default=3, help='How many pages of 50 to fetch (default 3)')

    def handle(self, *args, **options):
        pages = options['pages']
        headers = {'Authorization': f'Bearer {settings.PAYSTACK_SECRET_KEY}'}

        imported = 0
        skipped = 0
        errors = 0

        for page in range(1, pages + 1):
            resp = requests.get(
                'https://api.paystack.co/transaction',
                params={'perPage': 50, 'status': 'success', 'page': page},
                headers=headers,
                timeout=20,
            )
            data = resp.json()
            if not data.get('status'):
                self.stderr.write(f'Paystack API error on page {page}: {data.get("message")}')
                break

            txns = data.get('data') or []
            if not txns:
                break

            for t in txns:
                ref = t.get('reference')
                if not ref:
                    continue

                amount_pesewas = t.get('amount', 0)
                amount_ghs = float(amount_pesewas) / 100.0
                metadata = t.get('metadata') or {}
                phone = str(metadata.get('phone') or metadata.get('phone_display') or '')
                fuel_type_name = str(metadata.get('fuel_type') or '')
                liters_raw = metadata.get('liters')

                try:
                    liters = float(liters_raw) if liters_raw not in (None, '', 'undefined') else None
                except (TypeError, ValueError):
                    liters = None

                customer = t.get('customer') or {}
                fn = str(customer.get('first_name') or '')
                ln = str(customer.get('last_name') or '')
                cust_name = (fn + ' ' + ln).strip() or None
                cust_email = str(customer.get('email') or '') or None

                try:
                    txn = _upsert_dashboard_transaction(
                        reference=ref,
                        phone=phone or '0000000000',
                        amount=amount_ghs,
                        paystack_data=t,
                        liters=liters,
                        fuel_type_name=fuel_type_name if fuel_type_name and fuel_type_name not in ('undefined', 'null') else None,
                        customer_name=cust_name,
                        customer_email=cust_email,
                    )

                    # Fix created_at to match Paystack's paid_at
                    paid_at_str = t.get('paid_at')
                    if paid_at_str:
                        try:
                            paid_at = datetime.fromisoformat(paid_at_str.replace('Z', '+00:00'))
                            Transaction.objects.filter(reference_number=ref).update(created_at=paid_at)
                        except Exception:
                            pass

                    self.stdout.write(f'  Saved {ref}: GHS {amount_ghs:.2f} id={getattr(txn, "id", "?")}')
                    imported += 1
                except Exception as e:
                    self.stderr.write(f'  Error saving {ref}: {e}')
                    errors += 1

        self.stdout.write(self.style.SUCCESS(f'Done: {imported} imported, {skipped} skipped, {errors} errors'))
