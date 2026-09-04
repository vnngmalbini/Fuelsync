from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.files.base import ContentFile
from django.http import HttpResponse, StreamingHttpResponse, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.db import transaction as db_transaction
from django.db.models import Case, IntegerField, Sum, When
from django.views.decorators.http import require_POST
from decimal import Decimal
from datetime import timedelta
import base64
import uuid
import json
import logging

import cv2
import numpy as np
from station.face_recognition_service import recognize_frame

logger = logging.getLogger(__name__)

from station.models import Transaction
from payment.services import build_receipt_pdf
from dashboard.permissions import role_required, get_user_role
from .models import (
    FuelType, FuelInventory, Pump, Transaction as StationTransaction,
    Receipt, Customer, Staff, Attendance, StaffRole
)
from dashboard.models import UserProfile
from .forms import TransactionForm


def _site_page_context(title, tag, heading, summary):
    return {
        "page_title": title,
        "page_tag": tag,
        "page_heading": heading,
        "page_summary": summary,
    }


@login_required
@role_required('admin', 'attendant')
def buy_fuel(request):
    fuel_types = FuelType.objects.exclude(name__iexact='lpg').order_by(
        Case(
            When(name='petrol', then=0),
            When(name='diesel', then=1),
            When(name='super', then=2),
            default=3,
            output_field=IntegerField(),
        )
    )
    fuel_type_data = [
        {
            'value': f'{fuel.current_price:.2f}',
            'label': f'{fuel.get_name_display()} - GHS {fuel.current_price:.2f}/L',
            'fuelType': fuel.name,
        }
        for fuel in fuel_types
    ]
    return render(request, "buy_fuel.html", {
        "fuel_types": fuel_types,
        "fuel_type_data": fuel_type_data,
    })


# ==================== FUEL SALE WORKFLOW ====================

@login_required
@role_required('attendant', 'admin')
def start_fuel_sale(request):
    """Start a new fuel sale transaction"""
    pumps = Pump.objects.filter(status='operational')
    fuel_types = FuelType.objects.exclude(name__iexact='lpg')
    
    context = {
        'pumps': pumps,
        'fuel_types': fuel_types,
        'page_title': 'Start Fuel Sale',
    }
    return render(request, 'fuel_sale_start.html', context)


@login_required
@role_required('attendant', 'admin')
def fuel_sale_form(request, pump_id):
    """Fuel sale form - enter quantity and select payment method"""
    pump = get_object_or_404(Pump, id=pump_id, status='operational')
    
    if request.method == 'POST':
        form = TransactionForm(request.POST)
        if form.is_valid():
            # Store transaction data in session for completion
            request.session['fuel_sale_data'] = {
                'pump_id': pump.id,
                'fuel_type_id': form.cleaned_data['fuel_type'].id,
                'liters': str(form.cleaned_data['liters_dispensed']),
                'payment_method': form.cleaned_data['payment_method'],
                'customer_phone': form.cleaned_data.get('customer_phone', ''),
                'notes': form.cleaned_data.get('notes', ''),
            }
            return redirect('fuel_sale_review')
    else:
        form = TransactionForm()
        # Hide LPG from the fuel type choices
        try:
            form.fields['fuel_type'].queryset = FuelType.objects.exclude(name__iexact='lpg')
        except Exception:
            pass
    
    context = {
        'pump': pump,
        'form': form,
        'page_title': f'Fuel Sale - Pump {pump.pump_number}',
    }
    # Provide a map of fuel_type id -> current_price for client-side preview
    try:
        fuel_prices = {str(ft.id): float(ft.current_price) for ft in FuelType.objects.exclude(name__iexact='lpg')}
    except Exception:
        fuel_prices = {}
    context['fuel_prices_json'] = json.dumps(fuel_prices)
    return render(request, 'fuel_sale_form.html', context)


@login_required
@role_required('attendant', 'admin')
def fuel_sale_review(request):
    """Review transaction before completion"""
    fuel_sale_data = request.session.get('fuel_sale_data')
    
    if not fuel_sale_data:
        messages.error(request, 'No transaction data found. Please start a new sale.')
        return redirect('start_fuel_sale')
    
    # Get related objects
    pump = get_object_or_404(Pump, id=fuel_sale_data['pump_id'])
    fuel_type = get_object_or_404(FuelType, id=fuel_sale_data['fuel_type_id'])
    
    liters = Decimal(fuel_sale_data['liters'])
    total_amount = liters * fuel_type.current_price
    
    # Get or create customer if phone provided
    customer = None
    if fuel_sale_data.get('customer_phone'):
        customer, created = Customer.objects.get_or_create(
            phone=fuel_sale_data['customer_phone'],
            defaults={
                'first_name': 'Customer',
                'last_name': fuel_sale_data['customer_phone'],
            }
        )
    
    # Get current staff
    try:
        staff = request.user.staff_profile
    except:
        staff = None
    
    context = {
        'pump': pump,
        'fuel_type': fuel_type,
        'liters': liters,
        'price_per_liter': fuel_type.current_price,
        'total_amount': total_amount,
        'payment_method': fuel_sale_data['payment_method'],
        'customer': customer,
        'staff': staff,
        'page_title': 'Review Transaction',
    }
    
    return render(request, 'fuel_sale_review.html', context)


@login_required
@role_required('attendant', 'admin')
def fuel_sale_complete(request):
    """Complete the fuel sale transaction"""
    logger.info(f"fuel_sale_complete called - Method: {request.method}, User: {request.user.username}")
    
    if request.method != 'POST':
        logger.warning(f"fuel_sale_complete called with {request.method} instead of POST")
        return redirect('start_fuel_sale')
    
    fuel_sale_data = request.session.get('fuel_sale_data')
    logger.info(f"Session fuel_sale_data: {fuel_sale_data}")
    
    if not fuel_sale_data:
        logger.error('No transaction data found in session')
        messages.error(request, 'No transaction data found.')
        return redirect('start_fuel_sale')
    
    try:
        with db_transaction.atomic():
            logger.info(f"Starting transaction creation with data: {fuel_sale_data}")
            
            # Get objects
            pump = Pump.objects.get(id=fuel_sale_data['pump_id'])
            logger.info(f"Found pump: {pump.pump_number}")
            
            fuel_type = FuelType.objects.get(id=fuel_sale_data['fuel_type_id'])
            logger.info(f"Found fuel type: {fuel_type.name}")
            
            liters = Decimal(fuel_sale_data['liters'])
            logger.info(f"Liters to dispense: {liters}")
            
            # Check fuel availability
            fuel_inventory = FuelInventory.objects.select_for_update().get(fuel_type=fuel_type)
            logger.info(f"Fuel inventory: {fuel_inventory.quantity_liters}L available")
            
            if fuel_inventory.quantity_liters < liters:
                logger.warning(f"Insufficient fuel: need {liters}L, have {fuel_inventory.quantity_liters}L")
                messages.error(request, f'Insufficient fuel. Available: {fuel_inventory.quantity_liters}L')
                return redirect('fuel_sale_form', pump_id=pump.id)
            
            # Create transaction
            transaction_id = str(uuid.uuid4())[:8].upper()
            total_amount = liters * fuel_type.current_price
            logger.info(f"Calculated - Transaction ID: {transaction_id}, Amount: {total_amount}")
            
            # Get customer if exists
            customer = None
            if fuel_sale_data.get('customer_phone'):
                customer, _ = Customer.objects.get_or_create(
                    phone=fuel_sale_data['customer_phone'],
                    defaults={'first_name': 'Customer', 'last_name': fuel_sale_data['customer_phone']}
                )
                logger.info(f"Customer: {customer.phone}")
            
            # Get staff
            staff = request.user.staff_profile if hasattr(request.user, 'staff_profile') else None
            logger.info(f"Staff: {staff}")
            
            # Create transaction
            logger.info(f"Creating transaction with status=completed")
            txn = StationTransaction.objects.create(
                transaction_id=transaction_id,
                pump=pump,
                customer=customer,
                fuel_type=fuel_type,
                liters_dispensed=liters,
                price_per_liter=fuel_type.current_price,
                total_amount=total_amount,
                payment_method=fuel_sale_data['payment_method'],
                status='completed',
                staff=staff,
                reference_number=f"REF-{transaction_id}",
                notes=fuel_sale_data.get('notes', ''),
                completed_at=timezone.now(),
            )
            logger.info(f"✅ Transaction created - ID: {txn.id}, DB transaction_id: {txn.transaction_id}")
            
            # Update fuel inventory
            fuel_inventory.quantity_liters -= liters
            fuel_inventory.save()
            logger.info(f"Fuel inventory updated - new amount: {fuel_inventory.quantity_liters}L")
            
            # Update pump metrics
            pump.total_liters_dispensed += liters
            pump.total_revenue += total_amount
            pump.save()
            logger.info(f"Pump metrics updated - revenue: {pump.total_revenue}")
            
            # Create receipt
            receipt_number = f"RCP-{transaction_id}"
            loyalty_points = int(liters)  # 1 point per liter
            
            receipt = Receipt.objects.create(
                transaction=txn,
                receipt_number=receipt_number,
                loyalty_points_earned=loyalty_points,
                final_amount=total_amount,
            )
            logger.info(f"Receipt created - {receipt_number}")
            
            # Update customer loyalty points if exists
            if customer:
                customer.loyalty_points += loyalty_points
                customer.total_purchases += total_amount
                customer.total_liters += liters
                customer.save()
                logger.info(f"Customer loyalty points updated")
            
            # Clear session data
            if 'fuel_sale_data' in request.session:
                del request.session['fuel_sale_data']
                request.session.modified = True
            
            success_msg = f'Fuel sale completed successfully. Receipt: {receipt_number}'
            logger.info(f"✅ TRANSACTION COMPLETE: {success_msg}")
            messages.success(request, success_msg)
            return redirect('fuel_sale_receipt', receipt_id=receipt.id)
            
    except Pump.DoesNotExist:
        logger.error('Pump not found.')
        messages.error(request, 'Pump not found.')
    except FuelType.DoesNotExist:
        logger.error('Fuel type not found.')
        messages.error(request, 'Fuel type not found.')
    except FuelInventory.DoesNotExist:
        logger.error('Fuel inventory not found.')
        messages.error(request, 'Fuel inventory not found.')
    except Exception as e:
        logger.exception(f'Unexpected error completing sale: {str(e)}')
        messages.error(request, f'Error completing sale: {str(e)}')
    
    logger.error('Transaction failed - redirecting to start_fuel_sale')
    return redirect('start_fuel_sale')


@login_required
@role_required('attendant', 'admin')
def fuel_sale_receipt(request, receipt_id):
    """Display fuel sale receipt"""
    receipt_obj = get_object_or_404(Receipt, id=receipt_id)
    transaction = receipt_obj.transaction
    
    context = {
        'receipt': receipt_obj,
        'transaction': transaction,
        'page_title': 'Transaction Receipt',
    }
    
    return render(request, 'fuel_sale_receipt.html', context)


@login_required
@role_required('attendant', 'admin')
def fuel_sale_print_receipt(request, receipt_id):
    """Generate printable receipt"""
    receipt_obj = get_object_or_404(Receipt, id=receipt_id)
    transaction = receipt_obj.transaction
    
    context = {
        'receipt': receipt_obj,
        'transaction': transaction,
    }
    
    return render(request, 'fuel_sale_print_receipt.html', context)


@login_required
@role_required('attendant', 'admin')
def shift_summary(request):
    """End-of-shift summary for attendants"""
    try:
        staff = request.user.staff_profile
    except:
        messages.error(request, 'Staff profile not found.')
        return redirect('dashboard')
    
    today = timezone.localdate()
    start_dt = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
    end_dt = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.max.time()))
    
    # Shift transactions
    shift_transactions = StationTransaction.objects.filter(
        staff=staff,
        created_at__gte=start_dt,
        created_at__lte=end_dt,
        status='completed'
    ).select_related('fuel_type', 'customer', 'pump')
    
    # Calculate totals
    total_revenue = shift_transactions.aggregate(total=Sum('total_amount'))['total'] or 0
    total_liters = shift_transactions.aggregate(total=Sum('liters_dispensed'))['total'] or 0
    transaction_count = shift_transactions.count()
    average_transaction_amount = (total_revenue / transaction_count) if transaction_count else 0
    
    # Payment method breakdown
    payment_breakdown = {}
    for txn in shift_transactions:
        method = txn.payment_method
        if method not in payment_breakdown:
            payment_breakdown[method] = {'count': 0, 'amount': 0}
        payment_breakdown[method]['count'] += 1
        payment_breakdown[method]['amount'] += float(txn.total_amount)
    
    # Fuel type breakdown
    fuel_breakdown = {}
    for txn in shift_transactions:
        fuel_name = txn.fuel_type.get_name_display()
        if fuel_name not in fuel_breakdown:
            fuel_breakdown[fuel_name] = {'liters': 0, 'amount': 0}
        fuel_breakdown[fuel_name]['liters'] += float(txn.liters_dispensed)
        fuel_breakdown[fuel_name]['amount'] += float(txn.total_amount)
    
    context = {
        'staff': staff,
        'total_revenue': total_revenue,
        'total_liters': total_liters,
        'transaction_count': transaction_count,
        'average_transaction_amount': average_transaction_amount,
        'shift_transactions': shift_transactions,
        'payment_breakdown': payment_breakdown,
        'fuel_breakdown': fuel_breakdown,
        'page_title': "Shift Summary",
    }
    
    return render(request, 'shift_summary.html', context)


def receipt(request):
    query = request.GET.get("q")
    transaction = None

    if query:
        transaction = Transaction.objects.filter(phone=query).first()

        if transaction:
            pdf = build_receipt_pdf(transaction)

            response = HttpResponse(pdf, content_type="application/pdf")
            response["Content-Disposition"] = f'inline; filename="receipt-{transaction.reference}.pdf"'
            return response

    return render(request, "receipt.html", {
        "transaction": transaction,
        "q": query,
        "search_only": True,
    })


def _get_or_create_own_staff(user):
    staff = Staff.objects.filter(user=user, is_active=True).first()
    if staff:
        return staff

    role_name = get_user_role(user) or 'attendant'
    if user.is_superuser:
        role_name = 'admin'
    if role_name not in {'admin', 'manager', 'attendant'}:
        return None

    staff_role, _ = StaffRole.objects.get_or_create(
        name=role_name,
        defaults={'permissions': 'dashboard,inventory,pumps,sales,payments'},
    )
    staff, _ = Staff.objects.update_or_create(
        user=user,
        defaults={
            'role': staff_role,
            'phone': getattr(UserProfile.objects.filter(user=user).first(), 'phone', '') or '',
            'date_hired': timezone.localdate(),
            'salary': 0,
            'is_active': True,
        },
    )
    return staff


def home(request):
    welcome_first_name = ""

    if request.user.is_authenticated:
        user_first = (request.user.first_name or "").strip()

        profile_first = ""
        try:
            profile_first = (request.user.customer_profile.first_name or "").strip()
        except Exception:
            profile_first = ""

        username_first = (request.user.username or "").split(".")[0].split("_")[0].strip()

        welcome_first_name = user_first or profile_first or username_first or "there"

    try:
        completed = Transaction.objects.filter(status='completed')
        total_transactions = completed.count()
        total_revenue = completed.aggregate(s=Sum('total_amount'))['s'] or Decimal('0')
        total_customers = Customer.objects.count()
    except Exception:
        total_transactions = 0
        total_revenue = Decimal('0')
        total_customers = 0

    attendance_rows = Attendance.objects.select_related('staff__user').order_by('-created_at')[:50]
    staff_list = Staff.objects.filter(is_active=True).select_related('user', 'role')
    enrolled_count = sum(1 for s in staff_list if s.profile_picture)

    attendance_widget = {
        'show': True,
        'can_mark': True,
        'staff_without_photo_count': len(staff_list) - enrolled_count,
    }

    return render(request, "home.html", {
        "welcome_first_name": welcome_first_name,
        "stat_transactions": total_transactions,
        "stat_revenue": total_revenue,
        "stat_customers": total_customers,
        "attendance_widget": attendance_widget,
        "attendance_rows": attendance_rows,
    })


@require_POST
def attendance_public_capture(request):
    """Public kiosk endpoint: recognize any enrolled staff member and log clock-in/out."""
    try:
        payload = json.loads(request.body)
        header, b64data = payload['image'].split(',', 1)
        img_bytes = base64.b64decode(b64data)
        frame = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError('could not decode image')
    except (KeyError, ValueError, json.JSONDecodeError):
        return JsonResponse({'status': 'error', 'message': 'Invalid image payload'}, status=400)

    result = recognize_frame(frame)
    if not result['matched']:
        return JsonResponse({'status': 'no_match', 'reason': result['reason']})

    staff = Staff.objects.select_related('user', 'role').get(id=result['staff_id'])
    last_event = Attendance.objects.filter(staff=staff).order_by('-created_at').first()
    event_type = 'clock_in' if (last_event is None or last_event.event_type == 'clock_out') else 'clock_out'

    yesterday = timezone.now() - timedelta(hours=24)
    if event_type == 'clock_in':
        if Attendance.objects.filter(staff=staff, event_type='clock_in', created_at__gte=yesterday).exists():
            return JsonResponse({
                'status': 'error',
                'message': 'You can only log in once.'
            })
    else:
        if Attendance.objects.filter(staff=staff, event_type='clock_out', created_at__gte=yesterday).exists():
            return JsonResponse({
                'status': 'error',
                'message': 'You have already clocked out within the last 24 hours.'
            })

    attendance = Attendance.objects.create(
        staff=staff,
        event_type=event_type,
        confidence=result['confidence'],
        captured_image=ContentFile(img_bytes, name=f'{staff.id}_{timezone.now():%Y%m%d%H%M%S}.jpg'),
    )

    local_dt = timezone.localtime(attendance.created_at)
    return JsonResponse({
        'status': 'success',
        'staff_name': staff.user.get_full_name() or staff.user.username,
        'event_type': attendance.event_type,
        'confidence': result['confidence'],
        'timestamp': attendance.created_at.isoformat(),
        'date': local_dt.strftime('%b %d, %Y'),
        'time': local_dt.strftime('%I:%M %p'),
        'staff_phone': staff.phone,
        'staff_email': staff.user.email,
        'staff_photo_url': staff.profile_picture.url if staff.profile_picture else None,
        'staff_role': staff.role.get_name_display(),
    })


def about(request):
    return render(
        request,
        "about.html",
        _site_page_context(
            "About | FuelSync",
            "About",
            "Built for modern fueling",
            "FuelSync helps drivers and fleets manage fuel purchases with transparent payments, fast receipts, and a simple workflow.",
        ),
    )


def how_it_works(request):
    return render(
        request,
        "how_it_works.html",
        _site_page_context(
            "How It Works | FuelSync",
            "How it works",
            "A simple three-step fuel flow",
            "Follow the same workflow every time: choose your fuel, pay securely, and receive a digital receipt.",
        ),
    )


def contact(request):
    return render(
        request,
        "contact.html",
        _site_page_context(
            "Contact | FuelSync",
            "Contact",
            "Get help or send feedback",
            "Use this page to reach the FuelSync team for support, questions, or partnership requests.",
        ),
    )


def server_output_page(request):
    """Render a simple page that connects to the server output stream."""
    return render(request, "server_output.html")


def server_output_stream(request):
    """Server-Sent Events stream that tails the server log file.

    This reads from `logs/server_output.log` in the project root and yields
    new lines as SSE `data:` events.
    """
    log_path = "logs/server_output.log"

    def event_stream():
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                # Seek to the end of file and then yield new lines as they arrive
                f.seek(0, 2)
                while True:
                    line = f.readline()
                    if line:
                        # SSE format: lines prefixed with 'data:' and double newline
                        yield f"data: {line.rstrip()}\n\n"
                    else:
                        import time

                        time.sleep(0.2)
        except FileNotFoundError:
            yield "data: (no log file found)\n\n"

    return StreamingHttpResponse(event_stream(), content_type="text/event-stream")
