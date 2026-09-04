import os
import sys
# Ensure project root is on sys.path when running the script directly
root = os.path.dirname(os.path.dirname(__file__))
if root not in sys.path:
    sys.path.insert(0, root)
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
import django
django.setup()
from station.models import Transaction
qs = Transaction.objects.all().order_by('-created_at')[:10]
count = Transaction.objects.count()
print('Total transactions in DB:', count)
print('Latest transactions:')
for t in qs:
    print('ID', t.id, 'txn_id', t.transaction_id, 'ref', t.reference_number, 'status', t.status, 'method', t.payment_method, 'amount', t.total_amount, 'created', t.created_at.isoformat())
    p = getattr(t, 'payment', None)
    if p:
        print('  -> Payment:', p.reference, p.status, p.amount, 'paid_at', p.paid_at)
    print('-'*40)
