from datetime import datetime, timedelta
from collections import defaultdict
import base64
import json
import logging

import numpy as np
import cv2
from django.contrib import messages
from django.contrib.auth import logout, authenticate, login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.files.base import ContentFile
from django.db.models import Sum, Count, Q
from django.shortcuts import redirect, render, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.http import JsonResponse
from decimal import Decimal

logger = logging.getLogger(__name__)

from .forms import (
    AvatarUploadForm,
    PhoneAuthenticationForm,
    PhoneSignupForm,
    ProfilePreferencesForm,
    ProfilePictureForm,
    SMSTestForm,
    UserProfileForm,
)
from .permissions import get_user_role, role_required
from .models import UserProfile
from .models import DashboardDailySummary, refresh_dashboard_summary, refresh_daily_sales_report
from station.models import (
    FuelInventory, Notification, Pump, Transaction,
    FuelType, Staff, StaffRole, Receipt, Customer, DailySalesReport, Payment, FuelPriceHistory,
    Attendance
)
from station.face_recognition_service import recognize_frame
from payment.notifications import build_test_sms_message, get_sms_provider_status, queue_sms_delivery, record_payment_audit
from payment.services import normalize_phone_number


def _get_or_create_own_staff(user):
    """Every logged-in staff-role user (admin/manager/attendant) should be able to use
    attendance, even if a Staff row was never created for them (e.g. accounts provisioned
    outside the normal signup form). Auto-provision one on first use instead of blocking."""
    staff = Staff.objects.filter(user=user, is_active=True).first()
    if staff:
        return staff

    role_name = get_user_role(user) or 'attendant'
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


def _build_dashboard_live_signature():
    """Build a compact signature that changes whenever dashboard KPIs change."""
    today = timezone.localdate()

    completed_txns = Transaction.objects.filter(
        created_at__date=today,
        status='completed',
    ).exclude(
        fuel_type__name__iexact='lpg'
    )

    today_revenue = completed_txns.aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0')
    transaction_count = completed_txns.count()
    pending_payments = Transaction.objects.filter(status='pending').exclude(fuel_type__name__iexact='lpg').count()

    low_stock_count = 0
    for inventory in FuelInventory.objects.select_related('fuel_type').exclude(fuel_type__name__iexact='lpg'):
        if inventory.is_low_stock():
            low_stock_count += 1

    latest_txn = completed_txns.order_by('-created_at').values('transaction_id', 'created_at').first()
    latest_txn_id = latest_txn['transaction_id'] if latest_txn else ''
    latest_ts = latest_txn['created_at'].isoformat() if latest_txn and latest_txn['created_at'] else ''

    todays_payments = Payment.objects.filter(
        transaction__created_at__date=today,
        status='completed',
    )
    latest_payment = todays_payments.order_by('-updated_at').values('reference', 'updated_at').first()
    latest_payment_reference = latest_payment['reference'] if latest_payment else ''
    latest_payment_ts = latest_payment['updated_at'].isoformat() if latest_payment and latest_payment['updated_at'] else ''

    payment_count = todays_payments.count()

    return f"{transaction_count}|{payment_count}|{float(today_revenue):.2f}|{pending_payments}|{low_stock_count}|{latest_txn_id}|{latest_ts}|{latest_payment_reference}|{latest_payment_ts}"


def _month_start(value):
    return value.replace(day=1)


def _next_month_start(value):
    year = value.year + (1 if value.month == 12 else 0)
    month = 1 if value.month == 12 else value.month + 1
    return value.replace(year=year, month=month, day=1)


def _build_revenue_series(transactions, start_date, end_date, bucket="day"):
    """Build serializable revenue series for a time window."""
    timeline = []
    if bucket == "month":
        current = _month_start(start_date)
        while current <= end_date:
            timeline.append(current)
            current = _next_month_start(current)
    else:
        current = start_date
        while current <= end_date:
            timeline.append(current)
            current += timedelta(days=1)

    buckets = {
        value: {"revenue": 0.0, "transactions": 0, "liters": 0.0}
        for value in timeline
    }

    for txn in transactions:
        if not txn.created_at:
            continue

        created_date = txn.created_at.date()
        if bucket == "month":
            created_key = _month_start(created_date)
        else:
            created_key = created_date

        if created_key not in buckets:
            continue

        entry = buckets[created_key]
        entry["revenue"] += float(txn.total_amount or 0)
        entry["transactions"] += 1
        entry["liters"] += float(txn.liters_dispensed or 0)

    labels = []
    revenue_values = []
    transaction_values = []
    liters_values = []
    for value in timeline:
        entry = buckets[value]
        labels.append(value.strftime("%b") if bucket == "month" else value.strftime("%d %b"))
        revenue_values.append(entry["revenue"])
        transaction_values.append(entry["transactions"])
        liters_values.append(entry["liters"])

    return {
        "labels": labels,
        "revenue_values": revenue_values,
        "transaction_values": transaction_values,
        "liters_values": liters_values,
        "total_revenue": sum(revenue_values),
        "transaction_count": sum(transaction_values),
        "average_revenue": (sum(revenue_values) / len(revenue_values)) if revenue_values else 0,
    }


def _build_period_summary(label, transactions, start_date, end_date, bucket="day"):
    series = _build_revenue_series(transactions, start_date, end_date, bucket=bucket)
    return {
        "label": label,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "revenue": float(series["total_revenue"]),
        "transactions": int(series["transaction_count"]),
        "average_daily_revenue": float(series["average_revenue"]),
        "chart": {
            "labels": series["labels"],
            "revenue": series["revenue_values"],
            "transactions": series["transaction_values"],
            "liters": series["liters_values"],
        },
    }


class PhoneLoginView(LoginView):
    template_name = "login.html"
    authentication_form = PhoneAuthenticationForm
    redirect_authenticated_user = True
    
    def get_success_url(self):
        """Always send authenticated users to the unified dashboard."""
        return '/dashboard/'


@login_required
@role_required('admin', 'manager', 'attendant')
def dashboard_home(request):
    today = timezone.localdate()
    user_role = get_user_role(request.user) or 'attendant'
    can_view_revenue_analytics = user_role in {'admin', 'manager'} or request.user.is_superuser

    period_transactions = Transaction.objects.filter(
        created_at__date=today,
        status='completed',
    ).filter(_exclude_lpg_filter())

    today_revenue = period_transactions.aggregate(Sum('total_amount'))['total_amount__sum'] or 0
    transaction_count = period_transactions.count()
    total_fuel_dispensed = period_transactions.aggregate(Sum('liters_dispensed'))['liters_dispensed__sum'] or 0
    pending_payments = Transaction.objects.filter(status='pending').filter(_exclude_lpg_filter()).count()

    yesterday = today - timedelta(days=1)
    yesterday_transactions = Transaction.objects.filter(
        created_at__date=yesterday,
        status='completed',
    ).filter(_exclude_lpg_filter())
    yesterday_revenue = yesterday_transactions.aggregate(Sum('total_amount'))['total_amount__sum'] or 0
    yesterday_transaction_count = yesterday_transactions.count()
    yesterday_fuel_dispensed = yesterday_transactions.aggregate(Sum('liters_dispensed'))['liters_dispensed__sum'] or 0
    
    fuel_inventories = FuelInventory.objects.select_related('fuel_type').exclude(fuel_type__name__iexact='lpg')
    low_stock_count = sum(1 for inv in fuel_inventories if inv.is_low_stock())
    total_fuel_types = fuel_inventories.count()

    fuel_details = []
    for inventory in fuel_inventories:
        fuel_type_name = inventory.fuel_type.get_name_display()
        fuel_details.append({
            'name': fuel_type_name,
            'quantity': inventory.quantity_liters,
            'max_capacity': inventory.max_capacity,
            'percentage': (inventory.quantity_liters / inventory.max_capacity * 100) if inventory.max_capacity > 0 else 0,
            'is_low_stock': inventory.is_low_stock(),
            'status_badge': 'Low Stock' if inventory.is_low_stock() else 'OK'
        })

    recent_transactions = period_transactions.select_related(
        'pump', 'fuel_type', 'customer', 'staff'
    ).order_by('-created_at')[:5]
    
    transactions_data = []
    for txn in recent_transactions:
        transactions_data.append({
            'transaction_id': txn.transaction_id,
            'customer_name': txn.customer.first_name + ' ' + txn.customer.last_name if txn.customer else 'N/A',
            'fuel_type': txn.fuel_type.get_name_display() if txn.fuel_type else 'N/A',
            'amount': txn.total_amount,
            'status': txn.status.capitalize(),
            'time': txn.created_at.strftime('%I:%M %p') if txn.created_at else 'N/A',
        })

    # Show recent payments that belong to transactions in the same date window (today)
    recent_payments = Payment.objects.select_related('transaction', 'transaction__customer').filter(
        transaction__created_at__date=today,
        transaction__status='completed',
    ).order_by('-created_at')[:5]
    payment_data = []
    for payment in recent_payments:
        transaction = payment.transaction
        customer_name = payment.customer_name or (transaction.customer.first_name + ' ' + transaction.customer.last_name if transaction.customer else 'N/A')
        payment_data.append({
            'reference': payment.reference,
            'customer_name': customer_name,
            'amount': payment.amount,
            'status': payment.status.capitalize(),
            'method': payment.payment_method.replace('_', ' ').title(),
            'time': payment.created_at.strftime('%I:%M %p') if payment.created_at else 'N/A',
        })

    pumps = Pump.objects.select_related('fuel_type').exclude(fuel_type__name__iexact='lpg')
    pump_data = []
    for pump in pumps:
        pump_data.append({
            'pump_number': pump.pump_number,
            'fuel_type': pump.fuel_type.get_name_display(),
            'status': pump.status.capitalize(),
            'status_class': pump.status.lower(),
            'total_liters_dispensed': pump.total_liters_dispensed,
            'total_revenue': pump.total_revenue,
        })

    active_pumps = pumps.filter(status='operational').count()
    total_pumps = pumps.count()

    completed_customers = period_transactions.exclude(customer__isnull=True).values('customer').distinct().count()
    average_customer_spend = float(today_revenue) / completed_customers if completed_customers else 0

    daily_transactions = list(period_transactions.order_by('created_at'))
    revenue_buckets = []
    for start_hour in range(6, 22, 2):
        revenue_buckets.append({'label': f'{start_hour:02d}:00', 'revenue': 0.0, 'transactions': 0, 'liters': 0.0})

    for txn in daily_transactions:
        bucket_index = min(max((txn.created_at.hour - 6) // 2, 0), len(revenue_buckets) - 1)
        revenue_buckets[bucket_index]['revenue'] += float(txn.total_amount)
        revenue_buckets[bucket_index]['transactions'] += 1
        revenue_buckets[bucket_index]['liters'] += float(txn.liters_dispensed)

    revenue_trend_labels = [bucket['label'] for bucket in revenue_buckets]
    revenue_trend_values = [bucket['revenue'] for bucket in revenue_buckets]
    transaction_trend_values = [bucket['transactions'] for bucket in revenue_buckets]
    fuel_dispensed_trend_values = [bucket['liters'] for bucket in revenue_buckets]

    fuel_type_totals = {}
    weekly_labels = []
    weekly_totals = []
    for txn in daily_transactions:
        fuel_name = txn.fuel_type.get_name_display() if txn.fuel_type else 'Unknown'
        fuel_type_totals[fuel_name] = fuel_type_totals.get(fuel_name, 0) + float(txn.total_amount)
        weekly_labels.append(txn.created_at.strftime('%a'))
        weekly_totals.append(float(txn.total_amount))

    if not weekly_labels:
        weekly_labels = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        weekly_totals = [0, 0, 0, 0, 0, 0, 0]

    if not fuel_type_totals:
        fuel_type_totals = {
            'Petrol': 0,
            'Diesel': 0,
        }

    most_purchased_fuel = max(fuel_type_totals.items(), key=lambda item: item[1])[0]

    def percent_change(current, previous):
        if previous in (None, 0):
            return 100.0 if current else 0.0
        return ((float(current) - float(previous)) / float(previous)) * 100

    revenue_change = percent_change(today_revenue, yesterday_revenue)
    transaction_change = percent_change(transaction_count, yesterday_transaction_count)
    fuel_change = percent_change(total_fuel_dispensed, yesterday_fuel_dispensed)
    low_stock_rate = (low_stock_count / total_fuel_types * 100) if total_fuel_types else 0
    active_pump_rate = (active_pumps / total_pumps * 100) if total_pumps else 0
    pending_payment_rate = (pending_payments / (transaction_count + pending_payments) * 100) if (transaction_count + pending_payments) else 0

    analytics_periods = {}
    analytics_cards = []
    revenue_chart_periods = {
        'today': {
            'label': 'Today',
            'description': 'Hourly revenue from 06:00 to 22:00',
        },
        '7d': {
            'label': '7 Days',
            'description': 'Daily revenue for the last 7 days',
        },
        '30d': {
            'label': '30 Days',
            'description': 'Daily revenue for the last 30 days',
        },
        '90d': {
            'label': '90 Days',
            'description': 'Daily revenue for the last 90 days',
        },
        'year': {
            'label': 'Current Year',
            'description': 'Monthly revenue for the current year',
        },
    }

    if can_view_revenue_analytics:
        current_year_start = today.replace(month=1, day=1)
        revenue_windows = {
            '7d': (today - timedelta(days=6), today, 'day'),
            '30d': (today - timedelta(days=29), today, 'day'),
            '90d': (today - timedelta(days=89), today, 'day'),
            'year': (current_year_start, today, 'month'),
        }

        for key, (start_date, end_date, bucket) in revenue_windows.items():
            window_transactions = Transaction.objects.filter(
                created_at__date__gte=start_date,
                created_at__date__lte=end_date,
                status='completed',
            ).filter(_exclude_lpg_filter()).order_by('created_at')

            analytics_periods[key] = _build_period_summary(
                revenue_chart_periods[key]['label'],
                window_transactions,
                start_date,
                end_date,
                bucket=bucket,
            )

        analytics_cards = [
            {
                'key': '7d',
                'label': 'Past 7 Days',
                'value': f'GH₵{analytics_periods["7d"]["revenue"]:.0f}',
                'subtitle': f'{analytics_periods["7d"]["transactions"]} transactions',
                'tone': 'blue',
            },
            {
                'key': '30d',
                'label': 'Past 30 Days',
                'value': f'GH₵{analytics_periods["30d"]["revenue"]:.0f}',
                'subtitle': f'{analytics_periods["30d"]["transactions"]} transactions',
                'tone': 'green',
            },
            {
                'key': '90d',
                'label': 'Past 90 Days',
                'value': f'GH₵{analytics_periods["90d"]["revenue"]:.0f}',
                'subtitle': f'{analytics_periods["90d"]["transactions"]} transactions',
                'tone': 'orange',
            },
            {
                'key': 'year',
                'label': 'Current Year',
                'value': f'GH₵{analytics_periods["year"]["revenue"]:.0f}',
                'subtitle': f'{analytics_periods["year"]["transactions"]} transactions',
                'tone': 'navy',
            },
        ]

    today_chart = {
        'labels': revenue_trend_labels,
        'revenue': revenue_trend_values,
        'transactions': transaction_trend_values,
        'liters': fuel_dispensed_trend_values,
    }

    revenue_chart_data = {
        'today': today_chart,
    }
    revenue_chart_data.update({key: value['chart'] for key, value in analytics_periods.items()})

    if not can_view_revenue_analytics:
        revenue_chart_periods = {'today': revenue_chart_periods['today']}

    peak_hours = []
    for hour in range(6, 22, 2):
        hour_total = period_transactions.filter(created_at__hour=hour).count()
        peak_hours.append({'label': f'{hour:02d}:00', 'value': hour_total})

    current_time = timezone.localtime(timezone.now())
    display_name_source = request.user.get_short_name() or request.user.first_name or request.user.username
    display_name = str(display_name_source).replace('.', ' ').replace('_', ' ')
    greeting_hour = current_time.hour
    greeting = 'morning' if greeting_hour < 12 else 'afternoon' if greeting_hour < 18 else 'evening'

    own_staff_record = None
    own_last_event = None
    attendance_widget = {
        'show': False,
        'has_face': False,
        'shift_open': False,
        'marked_today': False,
        'marked_time': None,
        'closed_today': False,
        'closed_time': None,
    }
    if user_role in {'admin', 'manager', 'attendant'}:
        own_staff_record = _get_or_create_own_staff(request.user)
        if own_staff_record:
            own_last_event = Attendance.objects.filter(staff=own_staff_record).order_by('-created_at').first()
            own_shift_open = own_last_event is not None and own_last_event.event_type == 'clock_in'
            attendance_widget = {
                'show': True,
                'has_face': bool(own_staff_record.profile_picture),
                'shift_open': own_shift_open,
                'marked_today': own_shift_open,
                'marked_time': timezone.localtime(own_last_event.created_at).strftime('%I:%M %p on %b %d') if own_shift_open and own_last_event else None,
                'closed_today': own_last_event is not None and own_last_event.event_type == 'clock_out',
                'closed_time': timezone.localtime(own_last_event.created_at).strftime('%I:%M %p on %b %d') if own_last_event and own_last_event.event_type == 'clock_out' else None,
            }

    sparkline_map = {
        'revenue': revenue_trend_values[-8:] or [0],
        'transactions': transaction_trend_values[-8:] or [0],
        'fuel_dispensed': fuel_dispensed_trend_values[-8:] or [0],
        'low_stock': [low_stock_count for _ in range(8)] or [0],
        'active_pumps': [active_pumps for _ in range(8)] or [0],
        'pending_payments': [pending_payments for _ in range(8)] or [0],
    }

    kpi_cards = [
        {
            'label': 'Revenue Today',
            'value': f'GH₵{float(today_revenue):.0f}',
            'trend': revenue_change,
            'trend_label': 'vs yesterday',
            'icon': 'cash-coin',
            'tone': 'blue',
            'sparkline_key': 'revenue',
        },
        {
            'label': 'Transactions',
            'value': f'{transaction_count}',
            'trend': transaction_change,
            'trend_label': 'vs yesterday',
            'icon': 'receipt-cutoff',
            'tone': 'green',
            'sparkline_key': 'transactions',
        },
        {
            'label': 'Fuel Dispensed',
            'value': f'{float(total_fuel_dispensed):.2f} L',
            'trend': fuel_change,
            'trend_label': 'vs yesterday',
            'icon': 'droplet-half',
            'tone': 'orange',
            'sparkline_key': 'fuel_dispensed',
        },
        {
            'label': 'Low Stock Alerts',
            'value': f'{low_stock_count}',
            'trend': low_stock_rate,
            'trend_label': 'inventory at risk',
            'icon': 'exclamation-triangle',
            'tone': 'red',
            'sparkline_key': 'low_stock',
        },
        {
            'label': 'Active Pumps',
            'value': f'{active_pumps}',
            'trend': active_pump_rate,
            'trend_label': 'uptime rate',
            'icon': 'fuel-pump',
            'tone': 'navy',
            'sparkline_key': 'active_pumps',
        },
        {
            'label': 'Pending Payments',
            'value': f'{pending_payments}',
            'trend': pending_payment_rate,
            'trend_label': 'of transactions',
            'icon': 'wallet2',
            'tone': 'slate',
            'sparkline_key': 'pending_payments',
        },
    ]

    pump_status_summary = defaultdict(int)
    for pump in pump_data:
        pump_status_summary[pump['status_class']] += 1

    notifications = Notification.objects.filter(is_read=False).order_by('-created_at')[:5]
    notification_data = []
    for notif in notifications:
        notification_data.append({
            'type': notif.notification_type,
            'title': notif.title,
            'message': notif.message,
            'type_badge': notif.notification_type.replace('_', ' ').title()
        })

    recent_alerts = notification_data[:3]

    has_dashboard_data = True
    
    context = {
        'user_role': user_role,
        'user_role_label': user_role.title(),
        'can_view_revenue_analytics': can_view_revenue_analytics,
        'today_revenue': today_revenue,
        'transaction_count': transaction_count,
        'total_fuel_dispensed': total_fuel_dispensed,
        'low_stock_count': low_stock_count,
        'pending_payments': pending_payments,
        'active_pumps': active_pumps,
        'average_customer_spend': average_customer_spend,
        'most_purchased_fuel': most_purchased_fuel,
        'current_time': current_time,
        'greeting': greeting,
        'display_name': display_name,
        'fuel_details': fuel_details,
        'recent_transactions': transactions_data,
        'recent_payments': payment_data,
        'pump_data': pump_data,
        'notifications': notification_data,
        'recent_alerts': recent_alerts,
        'kpi_cards': kpi_cards,
        'sparkline_map': sparkline_map,
        'pump_status_summary': dict(pump_status_summary),
        'has_dashboard_data': has_dashboard_data,
        'revenue_chart_periods': revenue_chart_periods,
        'revenue_chart_data': revenue_chart_data,
        'analytics_cards': analytics_cards,
        'revenue_trend_labels': revenue_trend_labels,
        'revenue_trend_values': revenue_trend_values,
        'transaction_trend_values': transaction_trend_values,
        'fuel_dispensed_trend_values': fuel_dispensed_trend_values,
        'fuel_type_labels': list(fuel_type_totals.keys()),
        'fuel_type_values': list(fuel_type_totals.values()),
        'weekly_labels': weekly_labels,
        'weekly_totals': weekly_totals,
        'peak_hours': peak_hours,
        'attendance_widget': attendance_widget,
        'live_signature': _build_dashboard_live_signature(),
    }

    return render(request, "dashboard_saas.html", context)


@login_required
@role_required('admin', 'manager', 'attendant')
def dashboard_live_signature(request):
    """Return a lightweight signature used by the dashboard for live-refresh checks."""
    return JsonResponse({
        'signature': _build_dashboard_live_signature(),
        'server_time': timezone.localtime(timezone.now()).isoformat(),
    })


@login_required
def logout_view(request):
    if request.method == "POST":
        logout(request)
        return redirect("login")
    return render(request, "logout.html")

def signup(request):
    if request.user.is_authenticated:
        return redirect("home")
    
    form = PhoneSignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        role = get_user_role(user)
        role_label = role.title() if role else "User"
        messages.success(request, f"{role_label} account created successfully. Please log in to continue.")
        return redirect("login")
    
    return render(request, "signup.html", {"form": form})


@login_required
def user_profile(request):
    # Get or create user profile
    profile, created = UserProfile.objects.get_or_create(user=request.user)
    
    if request.method == "POST":
        user_form = UserProfileForm(request.POST, instance=request.user)
        profile_form = ProfilePictureForm(request.POST, request.FILES, instance=profile)
        
        if user_form.is_valid() and profile_form.is_valid():
            user_form.save()
            profile_form.save()
            messages.success(request, "Profile updated successfully!")
            return redirect("profile")
    else:
        user_form = UserProfileForm(instance=request.user)
        profile_form = ProfilePictureForm(instance=profile)
    
    context = {
        'user_form': user_form,
        'profile_form': profile_form,
        'profile': profile,
    }
    return render(request, "profile.html", context)


@login_required
def settings_view(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    display_name = (request.user.get_full_name() or '').strip()
    sms_admin_role = get_user_role(request.user)
    can_manage_sms = sms_admin_role in {'admin', 'manager'}

    if not display_name:
        customer = getattr(request.user, 'customer_profile', None)
        if customer:
            display_name = f"{customer.first_name} {customer.last_name}".strip()

    if not display_name:
        display_name = str(request.user.username).replace('.', ' ').replace('_', ' ')

    sms_test_form = SMSTestForm(initial={
        'recipient_phone': getattr(profile, 'phone', '') or getattr(getattr(request.user, 'customer_profile', None), 'phone', '') or '',
    })
    sms_provider_status = get_sms_provider_status()

    if request.method == "POST":
        if "send_test_sms" in request.POST:
            if not can_manage_sms:
                messages.error(request, "You are not allowed to send test SMS messages.")
                return redirect("settings")

            sms_test_form = SMSTestForm(request.POST)
            if sms_test_form.is_valid():
                recipient_phone = normalize_phone_number(sms_test_form.cleaned_data['recipient_phone'])
                if not recipient_phone:
                    messages.error(request, "Please enter a valid phone number.")
                    return redirect("settings")
                else:
                    message = build_test_sms_message()
                    reference = f"TEST-{request.user.id}-{timezone.now().strftime('%Y%m%d%H%M%S')}"
                    try:
                        log, queued = queue_sms_delivery(
                            reference=reference,
                            recipient_phone=recipient_phone,
                            customer_name=display_name,
                            customer_email=request.user.email or '',
                            message=message,
                            purpose='test_sms',
                            user=request.user,
                            metadata={'source': 'dashboard_settings_test_sms'},
                        )
                        record_payment_audit(
                            'admin_action',
                            'Admin triggered a test SMS from the settings page.',
                            user=request.user,
                            metadata={'sms_delivery_log_id': getattr(log, 'id', None), 'recipient_phone': recipient_phone},
                        )
                        if queued:
                            messages.success(request, f"Test SMS queued for {recipient_phone}.")
                        else:
                            messages.info(request, "That test SMS was already queued or sent.")
                    except Exception as error:
                        messages.error(request, f"Unable to queue test SMS: {error}")
            else:
                messages.error(request, "Please provide a valid recipient phone number for the test SMS.")
            return redirect("settings")
        if "save_preferences" in request.POST:
            avatar_form = AvatarUploadForm(instance=profile)
            preferences_form = ProfilePreferencesForm(request.POST, instance=profile)
            if preferences_form.is_valid():
                preferences_form.save()
                messages.success(request, "Preferences updated successfully.")
                return redirect("settings")
            messages.error(request, "Please review your preference settings and try again.")
        else:
            avatar_form = AvatarUploadForm(request.POST, request.FILES, instance=profile)
            preferences_form = ProfilePreferencesForm(instance=profile)
            if avatar_form.is_valid():
                avatar_form.save()
                messages.success(request, "Profile picture updated successfully.")
                return redirect("settings")
            messages.error(request, "Please select a valid image file.")
    else:
        avatar_form = AvatarUploadForm(instance=profile)
        preferences_form = ProfilePreferencesForm(instance=profile)

    own_staff_record = _get_or_create_own_staff(request.user)

    return render(
        request,
        "settings.html",
        {
            "profile": profile,
            "avatar_form": avatar_form,
            "preferences_form": preferences_form,
            "display_name": display_name,
            "sms_test_form": sms_test_form,
            "sms_provider_status": sms_provider_status,
            "can_manage_sms": can_manage_sms,
            "staff": own_staff_record,
        },
    )


@login_required
@role_required('admin', 'manager', 'attendant')
def inventory_page(request):
    fuel_inventories = FuelInventory.objects.select_related('fuel_type').exclude(fuel_type__name__iexact='lpg')
    low_stock_count = sum(1 for item in fuel_inventories if item.is_low_stock())

    inventory_rows = []
    for item in fuel_inventories:
        percentage = (item.quantity_liters / item.max_capacity * 100) if item.max_capacity else 0
        inventory_rows.append({
            'fuel_name': item.fuel_type.get_name_display(),
            'quantity_liters': item.quantity_liters,
            'max_capacity': item.max_capacity,
            'percentage': percentage,
            'is_low_stock': item.is_low_stock(),
        })

    context = {
        'page_title': 'Inventory',
        'inventory_rows': inventory_rows,
        'total_fuel_types': len(inventory_rows),
        'low_stock_count': low_stock_count,
    }
    return render(request, 'dashboard_inventory.html', context)


@login_required
@role_required('admin', 'manager', 'attendant')
def inventory_live_api(request):
    inventory = FuelInventory.objects.select_related('fuel_type').exclude(
        fuel_type__name__iexact='lpg'
    )
    rows = []
    for item in inventory:
        percentage = (item.quantity_liters / item.max_capacity * 100) if item.max_capacity else 0
        rows.append({
            'fuel_name': item.fuel_type.get_name_display(),
            'quantity_liters': float(item.quantity_liters),
            'max_capacity': float(item.max_capacity),
            'percentage': float(percentage),
            'is_low_stock': item.is_low_stock(),
        })
    return JsonResponse({'status': 'success', 'inventory': rows})


@login_required
@role_required('admin', 'manager', 'attendant')
def pumps_page(request):
    pumps = Pump.objects.select_related('fuel_type').exclude(fuel_type__name__iexact='lpg').order_by('pump_number')
    context = {
        'page_title': 'Pumps',
        'pumps': pumps,
        'operational_count': pumps.filter(status='operational').count(),
        'maintenance_count': pumps.filter(status='maintenance').count(),
        'offline_count': pumps.filter(status='offline').count(),
    }
    return render(request, 'dashboard_pumps.html', context)


@login_required
@role_required('admin', 'manager', 'attendant')
def pumps_live_api(request):
    pumps = Pump.objects.select_related('fuel_type').exclude(
        fuel_type__name__iexact='lpg'
    ).order_by('pump_number')
    return JsonResponse({
        'status': 'success',
        'pumps': [{
            'pump_number': pump.pump_number,
            'fuel_name': pump.fuel_type.get_name_display(),
            'status': pump.status,
            'total_liters_dispensed': float(pump.total_liters_dispensed),
            'total_revenue': float(pump.total_revenue),
        } for pump in pumps],
    })


@login_required
@role_required('admin', 'manager', 'attendant')
def sales_page(request):
    all_sales = Transaction.objects.select_related('fuel_type', 'pump', 'customer', 'staff').filter(
        status='completed'
    ).exclude(
        fuel_type__name__iexact='lpg'
    ).order_by('-created_at')
    sales = all_sales[:50]

    totals = Transaction.objects.filter(status='completed').exclude(fuel_type__name__iexact='lpg').aggregate(
        total_revenue=Sum('total_amount'),
        total_liters=Sum('liters_dispensed'),
        total_count=Count('id'),
    )

    context = {
        'page_title': 'Sales',
        'sales': sales,
        'total_revenue': totals.get('total_revenue') or 0,
        'total_liters': totals.get('total_liters') or 0,
        'total_count': totals.get('total_count') or 0,
        'fuel_totals': [
            {
                'name': fuel.get_name_display(),
                'count': Transaction.objects.filter(
                    status='completed', fuel_type=fuel
                ).exclude(fuel_type__name__iexact='lpg').count(),
                'revenue': Transaction.objects.filter(
                    status='completed', fuel_type=fuel
                ).exclude(fuel_type__name__iexact='lpg').aggregate(
                    total=Sum('total_amount')
                )['total'] or 0,
            }
            for fuel in FuelType.objects.exclude(name__iexact='lpg').order_by('name')
        ],
    }
    return render(request, 'dashboard_sales.html', context)


@login_required
@role_required('admin', 'manager', 'attendant')
def customers_page(request):
    customers = Customer.objects.all().order_by('-created_at')[:100]
    context = {
        'page_title': 'Customers',
        'customers': customers,
        'total_customers': Customer.objects.count(),
    }
    return render(request, 'dashboard_customers.html', context)


@login_required
@role_required('admin', 'manager', 'attendant')
def reports_page(request):
    today = timezone.localdate()
    refresh_daily_sales_report(today)
    reports = DailySalesReport.objects.all().order_by('-date')[:30]
    start_dt = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
    end_dt = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.max.time()))
    today_sales = Transaction.objects.filter(created_at__gte=start_dt, created_at__lte=end_dt, status='completed').exclude(fuel_type__name__iexact='lpg')

    fuel_breakdown = []
    for fuel in FuelType.objects.exclude(name__iexact='lpg').order_by('name'):
        fuel_sales = today_sales.filter(fuel_type=fuel)
        totals = fuel_sales.aggregate(
            transactions=Count('id'),
            liters=Sum('liters_dispensed'),
            revenue=Sum('total_amount'),
        )
        liters = totals['liters'] or Decimal('0')
        revenue = totals['revenue'] or Decimal('0')
        fuel_breakdown.append({
            'name': fuel.get_name_display(),
            'transactions': totals['transactions'] or 0,
            'liters': liters,
            'revenue': revenue,
            'average_price': (revenue / liters) if liters else Decimal('0'),
        })

    context = {
        'page_title': 'Reports',
        'reports': reports,
        'today_revenue': today_sales.aggregate(Sum('total_amount'))['total_amount__sum'] or 0,
        'today_liters': today_sales.aggregate(Sum('liters_dispensed'))['liters_dispensed__sum'] or 0,
        'today_transactions': today_sales.count(),
        'fuel_breakdown': fuel_breakdown,
    }
    return render(request, 'dashboard_reports.html', context)


@login_required
@role_required('admin', 'manager', 'attendant')
def price_history_page(request):
    price_history_rows = FuelPriceHistory.objects.all().order_by('id')
    current_prices = FuelType.objects.exclude(name__iexact='lpg').order_by('name')
    context = {
        'page_title': 'Price History',
        'price_history_rows': price_history_rows,
        'current_prices': current_prices,
    }
    return render(request, 'dashboard_price_history.html', context)


@login_required
@role_required('admin', 'manager', 'attendant')
def update_price_page(request):
    fuel_types = FuelType.objects.exclude(name__iexact='lpg').order_by('name')
    return render(request, 'dashboard_update_price.html', {
        'page_title': 'Update Fuel Price',
        'fuel_types': fuel_types,
    })


@login_required
@role_required('manager')
def attendance_dashboard_page(request):
    """Manager-only page: facial attendance kiosk, live camera feed, notifications,
    and attendance history."""
    for inv in FuelInventory.objects.select_related('fuel_type').all():
        if inv.is_low_stock():
            Notification.objects.get_or_create(
                notification_type='low_stock',
                related_fuel=inv.fuel_type,
                is_read=False,
                defaults={
                    'title': f'Low stock: {inv.fuel_type.get_name_display()}',
                    'message': f'{inv.fuel_type.get_name_display()} is at {inv.quantity_liters}L, below threshold {inv.min_threshold}L.',
                },
            )

    unread_notification_count = Notification.objects.filter(is_read=False).count()
    notifications = Notification.objects.order_by('-created_at')[:20]
    attendance_rows = Attendance.objects.select_related('staff__user').order_by('-created_at')[:50]
    staff_list = Staff.objects.filter(is_active=True).select_related('user', 'role').order_by('user__first_name', 'user__username')
    enrolled_count = sum(1 for s in staff_list if s.profile_picture)
    total_active = len(staff_list)

    context = {
        'page_title': 'Attendance & Fuel Pricing',
        'notifications': notifications,
        'unread_notification_count': unread_notification_count,
        'attendance_rows': attendance_rows,
        'staff_list': staff_list,
        'staff_without_photo_count': total_active - enrolled_count,
    }
    return render(request, 'dashboard_attendance.html', context)


@login_required
@role_required('manager')
def attendance_capture_api(request):
    """POST {'image': 'data:image/jpeg;base64,...'} -> recognize the face against enrolled
    staff, log an Attendance clock-in/out event, and return the result as JSON."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

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

    staff = Staff.objects.select_related('user').get(id=result['staff_id'])
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
    })


@login_required
@role_required('manager')
def attendance_staff_photo_upload(request, staff_id):
    """POST an image file to enroll/replace a staff member's face-recognition photo."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    staff = get_object_or_404(Staff, pk=staff_id, is_active=True)
    photo = request.FILES.get('photo')
    if not photo:
        return JsonResponse({'status': 'error', 'message': 'No photo provided.'}, status=400)

    staff.profile_picture = photo
    staff.save(update_fields=['profile_picture', 'updated_at'])

    return JsonResponse({
        'status': 'success',
        'staff_id': staff.id,
        'photo_url': staff.profile_picture.url,
    })


@login_required
def attendance_self_photo_upload(request):
    """Self-service face registration: a user enrolls their own attendance photo."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    staff = _get_or_create_own_staff(request.user)
    if not staff:
        return JsonResponse({'status': 'error', 'message': 'Attendance is only available for staff accounts.'}, status=403)

    photo = request.FILES.get('photo')
    if not photo:
        return JsonResponse({'status': 'error', 'message': 'No photo provided.'}, status=400)

    staff.profile_picture = photo
    staff.save(update_fields=['profile_picture', 'updated_at'])

    return JsonResponse({'status': 'success', 'photo_url': staff.profile_picture.url})


@login_required
def attendance_self_mark(request):
    """Auto attendance: recognize the logged-in user's own face and toggle their shift
    clock-in/clock-out state.

    Uses the last recorded event (not calendar date) to decide whether a clock-in or
    clock-out is valid -- this matters for night-shift staff whose clock-in and clock-out
    fall on different calendar dates either side of midnight.

    Frontend calls this repeatedly (no manual 'Verify' step) while the camera is open. Real
    recognition happens via recognize_frame() -- the same OpenCV/LBPH backend used by the
    manager kiosk (station/face_recognition_service.py). This is where a future, more advanced
    recognition/liveness API would be swapped in without the frontend changing shape.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    staff = _get_or_create_own_staff(request.user)
    if not staff:
        return JsonResponse({'status': 'error', 'message': 'Attendance is only available for staff accounts.'}, status=403)
    if not staff.profile_picture:
        return JsonResponse({'status': 'no_face_registered'})

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid request payload'}, status=400)

    event_type = payload.get('event_type', 'clock_in')
    if event_type not in ('clock_in', 'clock_out'):
        return JsonResponse({'status': 'error', 'message': 'Invalid event_type'}, status=400)

    last_event = Attendance.objects.filter(staff=staff).order_by('-created_at').first()
    shift_open = last_event is not None and last_event.event_type == 'clock_in'

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

    if event_type == 'clock_in' and shift_open:
        return JsonResponse({
            'status': 'already_marked',
            'event_type': 'clock_in',
            'time': timezone.localtime(last_event.created_at).strftime('%I:%M %p'),
        })
    if event_type == 'clock_out' and not shift_open:
        if last_event is None:
            return JsonResponse({'status': 'not_clocked_in'})
        return JsonResponse({
            'status': 'already_marked',
            'event_type': 'clock_out',
            'time': timezone.localtime(last_event.created_at).strftime('%I:%M %p'),
        })

    try:
        header, b64data = payload['image'].split(',', 1)
        img_bytes = base64.b64decode(b64data)
        frame = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError('could not decode image')
    except (KeyError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid image payload'}, status=400)

    result = recognize_frame(frame)
    if not result['matched'] or result['staff_id'] != staff.id:
        reason = result['reason'] or 'identity_mismatch'
        return JsonResponse({'status': 'no_match', 'reason': reason})

    attendance = Attendance.objects.create(
        staff=staff,
        event_type=event_type,
        confidence=result['confidence'],
        captured_image=ContentFile(img_bytes, name=f'{staff.id}_{timezone.now():%Y%m%d%H%M%S}.jpg'),
    )

    return JsonResponse({
        'status': 'success',
        'event_type': event_type,
        'staff_name': staff.user.get_full_name() or staff.user.username,
        'date': timezone.localdate().strftime('%d %B %Y'),
        'time': timezone.localtime(attendance.created_at).strftime('%I:%M %p'),
    })


@login_required
@role_required('admin', 'manager', 'attendant')
def my_attendance_page(request):
    """Dedicated page: mark today's attendance (facial recognition) and browse your own
    clock-in/out history. Kept separate from the main dashboard on purpose."""
    own_staff_record = _get_or_create_own_staff(request.user)
    own_last_event = None
    if own_staff_record:
        own_last_event = Attendance.objects.filter(staff=own_staff_record).order_by('-created_at').first()

    own_shift_open = own_last_event is not None and own_last_event.event_type == 'clock_in'

    attendance_widget = {
        'show': own_staff_record is not None,
        'has_face': bool(own_staff_record.profile_picture) if own_staff_record else False,
        'shift_open': own_shift_open,
        'marked_today': own_shift_open,
        'marked_time': timezone.localtime(own_last_event.created_at).strftime('%I:%M %p on %b %d') if own_shift_open else None,
        'closed_today': own_last_event is not None and own_last_event.event_type == 'clock_out',
        'closed_time': timezone.localtime(own_last_event.created_at).strftime('%I:%M %p on %b %d') if (own_last_event and own_last_event.event_type == 'clock_out') else None,
    }

    attendance_history_panel = []
    if own_staff_record:
        for record in Attendance.objects.filter(staff=own_staff_record).order_by('-created_at')[:50]:
            local_dt = timezone.localtime(record.created_at)
            attendance_history_panel.append({
                'event_type': record.event_type,
                'event_label': 'Check In' if record.event_type == 'clock_in' else 'Check Out',
                'date': local_dt.strftime('%b %d, %Y'),
                'time': local_dt.strftime('%I:%M %p'),
            })

    context = {
        'page_title': 'My Attendance',
        'attendance_widget': attendance_widget,
        'attendance_history_panel': attendance_history_panel,
    }
    return render(request, 'dashboard_my_attendance.html', context)


@login_required
@role_required('admin', 'manager', 'attendant')
def notifications_page(request):
    """Dedicated page for browsing and managing notifications, separate from the dashboard."""
    notification_panel = []
    for notif in Notification.objects.order_by('-created_at')[:50]:
        notification_panel.append({
            'id': notif.id,
            'title': notif.title,
            'message': notif.message,
            'type_badge': notif.notification_type.replace('_', ' ').title(),
            'is_read': notif.is_read,
            'created_at': timezone.localtime(notif.created_at).strftime('%b %d, %I:%M %p'),
        })
    notification_unread_count = Notification.objects.filter(is_read=False).count()

    context = {
        'page_title': 'Notifications',
        'notification_panel': notification_panel,
        'notification_unread_count': notification_unread_count,
    }
    return render(request, 'dashboard_notifications.html', context)


@login_required
@role_required('admin', 'manager', 'attendant')
def payments_page(request):
    payments_qs = Payment.objects.select_related('transaction', 'transaction__fuel_type').all().order_by('-created_at')
    context = {
        'page_title': 'Payments',
        'payments': payments_qs[:50],
        'pending_count': payments_qs.filter(status='pending').count(),
        'completed_count': payments_qs.filter(status='completed').count(),
        'failed_count': payments_qs.filter(status='failed').count(),
    }
    return render(request, 'dashboard_payments.html', context)


# ==================== ROLE-BASED DASHBOARDS ====================

@login_required
@role_required('admin')
def admin_dashboard(request):
    """Admin Dashboard - System-wide view and management"""
    today = timezone.localdate()
    start_dt = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
    end_dt = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.max.time()))
    
    # Revenue metrics
    today_transactions = Transaction.objects.filter(
        created_at__gte=start_dt,
        created_at__lte=end_dt,
        status='completed'
    ).filter(_exclude_lpg_filter())
    today_revenue = today_transactions.aggregate(Sum('total_amount'))['total_amount__sum'] or 0
    
    # All time metrics
    total_revenue = Transaction.objects.filter(status='completed').filter(_exclude_lpg_filter()).aggregate(Sum('total_amount'))['total_amount__sum'] or 0
    total_transactions = Transaction.objects.filter(status='completed').filter(_exclude_lpg_filter()).count()
    total_customers = Customer.objects.count()
    total_staff = Staff.objects.filter(is_active=True).count()
    
    # Fuel inventory
    fuel_inventories = FuelInventory.objects.select_related('fuel_type').exclude(fuel_type__name__iexact='lpg')
    low_stock_fuels = [inv for inv in fuel_inventories if inv.is_low_stock()]
    
    # Pump status
    pumps = Pump.objects.exclude(fuel_type__name__iexact='lpg')
    pump_status = {
        'operational': pumps.filter(status='operational').count(),
        'maintenance': pumps.filter(status='maintenance').count(),
        'offline': pumps.filter(status='offline').count(),
    }
    
    # Recent transactions for admin
    recent_transactions = today_transactions.select_related('pump', 'fuel_type', 'customer', 'staff').order_by('-created_at')[:10]
    
    # Fuel prices
    fuel_types = FuelType.objects.exclude(name__iexact='lpg')
    
    context = {
        'page_title': 'Admin Dashboard',
        'today_revenue': today_revenue,
        'total_revenue': total_revenue,
        'total_transactions': total_transactions,
        'total_customers': total_customers,
        'total_staff': total_staff,
        'low_stock_count': len(low_stock_fuels),
        'low_stock_fuels': low_stock_fuels,
        'fuel_inventories': fuel_inventories,
        'pump_status': pump_status,
        'recent_transactions': recent_transactions,
        'fuel_types': fuel_types,
        'active_pumps': pump_status['operational'],
    }
    
    return render(request, 'dashboard_admin.html', context)


@login_required
@role_required('manager')
def manager_dashboard(request):
    """Manager Dashboard - Daily operations and reporting"""
    today = timezone.localdate()
    start_dt = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
    end_dt = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.max.time()))
    
    # Today's metrics
    today_transactions = Transaction.objects.filter(
        created_at__date=today,
        status='completed'
    ).exclude(
        fuel_type__name__iexact='lpg'
    )
    today_revenue = today_transactions.aggregate(Sum('total_amount'))['total_amount__sum'] or 0
    today_liters = today_transactions.aggregate(Sum('liters_dispensed'))['liters_dispensed__sum'] or 0
    today_count = today_transactions.count()
    
    # Weekly metrics
    week_start = today - timedelta(days=today.weekday())
    week_transactions = Transaction.objects.filter(
        created_at__date__gte=week_start,
        status='completed'
    ).exclude(
        fuel_type__name__iexact='lpg'
    )
    week_revenue = week_transactions.aggregate(Sum('total_amount'))['total_amount__sum'] or 0
    
    # Monthly metrics
    month_start = today.replace(day=1)
    month_transactions = Transaction.objects.filter(
        created_at__date__gte=month_start,
        status='completed'
    ).exclude(
        fuel_type__name__iexact='lpg'
    )
    month_revenue = month_transactions.aggregate(Sum('total_amount'))['total_amount__sum'] or 0
    
    # Real-time fuel stock
    fuel_inventories = FuelInventory.objects.select_related('fuel_type').exclude(fuel_type__name__iexact='lpg')
    
    # Attendant performance
    attendants = Staff.objects.filter(role__name='attendant', is_active=True).select_related('user')
    attendant_performance = []
    for attendant in attendants:
        txn_count = Transaction.objects.filter(staff=attendant, status='completed').exclude(fuel_type__name__iexact='lpg').count()
        txn_revenue = Transaction.objects.filter(staff=attendant, status='completed').exclude(fuel_type__name__iexact='lpg').aggregate(Sum('total_amount'))['total_amount__sum'] or 0
        attendant_performance.append({
            'name': attendant.user.get_full_name(),
            'transactions': txn_count,
            'revenue': txn_revenue,
        })
    
    # Pump status
    pumps = Pump.objects.exclude(fuel_type__name__iexact='lpg')
    pump_data = []
    for pump in pumps:
        pump_data.append({
            'pump_number': pump.pump_number,
            'fuel_type': pump.fuel_type.get_name_display(),
            'status': pump.status,
            'total_dispensed': pump.total_liters_dispensed,
        })
    
    context = {
        'page_title': 'Manager Dashboard',
        'today_revenue': today_revenue,
        'today_liters': today_liters,
        'today_transactions': today_count,
        'week_revenue': week_revenue,
        'month_revenue': month_revenue,
        'fuel_inventories': fuel_inventories,
        'attendant_performance': attendant_performance,
        'pump_data': pump_data,
    }
    
    return render(request, 'dashboard_manager.html', context)


@login_required
@role_required('attendant')
def attendant_dashboard(request):
    """Attendant Dashboard - Fuel sales and transactions"""
    today = timezone.localdate()
    start_dt = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
    end_dt = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.max.time()))
    
    # Get current staff
    try:
        staff = request.user.staff_profile
    except Staff.DoesNotExist:
        staff = None
    
    # Today's sales for this attendant
    today_transactions = Transaction.objects.filter(
        staff=staff,
        created_at__date=today,
        status='completed'
    ).exclude(fuel_type__name__iexact='lpg') if staff else Transaction.objects.none()
    
    today_revenue = today_transactions.aggregate(Sum('total_amount'))['total_amount__sum'] or 0
    today_liters = today_transactions.aggregate(Sum('liters_dispensed'))['liters_dispensed__sum'] or 0
    today_count = today_transactions.count()
    
    # Shift performance
    total_revenue = Transaction.objects.filter(staff=staff, status='completed').exclude(fuel_type__name__iexact='lpg').aggregate(Sum('total_amount'))['total_amount__sum'] or 0
    total_transactions = Transaction.objects.filter(staff=staff, status='completed').exclude(fuel_type__name__iexact='lpg').count()
    
    # Available pumps
    pumps = Pump.objects.filter(status='operational').exclude(fuel_type__name__iexact='lpg')
    
    # Fuel types for selection
    fuel_types = FuelType.objects.exclude(name__iexact='lpg')
    
    # Recent transactions
    recent_txns = today_transactions.select_related('fuel_type', 'customer', 'pump').order_by('-created_at')[:5]
    
    context = {
        'page_title': 'Attendant Dashboard',
        'staff': staff,
        'today_revenue': today_revenue,
        'today_liters': today_liters,
        'today_transactions': today_count,
        'total_revenue': total_revenue,
        'total_transactions': total_transactions,
        'pumps': pumps,
        'fuel_types': fuel_types,
        'recent_transactions': recent_txns,
    }
    
    return render(request, 'dashboard_attendant.html', context)


@login_required
@role_required('customer')
def customer_dashboard(request):
    """Customer Dashboard - Personal purchases and rewards"""
    try:
        customer = request.user.customer_profile
    except Customer.DoesNotExist:
        customer = None
    
    # Customer's transactions
    transactions = Transaction.objects.filter(
        customer=customer,
        status='completed'
    ).exclude(fuel_type__name__iexact='lpg').select_related('fuel_type', 'pump') if customer else Transaction.objects.none()
    
    total_spent = transactions.aggregate(Sum('total_amount'))['total_amount__sum'] or 0
    total_liters = transactions.aggregate(Sum('liters_dispensed'))['liters_dispensed__sum'] or 0
    total_visits = transactions.count()
    average_spent_per_visit = (total_spent / total_visits) if total_visits else 0
    points_to_next_tier = max(500 - (customer.loyalty_points if customer else 0), 0)
    
    # Recent transactions
    recent_txns = transactions.order_by('-created_at')[:5]
    
    # Loyalty info
    loyalty_points = customer.loyalty_points if customer else 0
    
    context = {
        'page_title': 'Customer Dashboard',
        'customer': customer,
        'total_spent': total_spent,
        'total_liters': total_liters,
        'total_visits': total_visits,
        'loyalty_points': loyalty_points,
        'average_spent_per_visit': average_spent_per_visit,
        'points_to_next_tier': points_to_next_tier,
        'recent_transactions': recent_txns,
    }
    
    return render(request, 'dashboard_customer.html', context)


@login_required
@role_required('admin', 'manager', 'attendant')
def dashboard_live_stats_api(request):
    """
    API endpoint for live dashboard statistics.
    Returns today's KPIs as JSON for auto-refresh functionality.
    """
    today = timezone.localdate()
    
    try:
        # Always query the Transaction table directly so the live update reflects
        # payments immediately without depending on the DashboardDailySummary cache.
        completed_txns = Transaction.objects.filter(
            created_at__date=today,
            status='completed',
        ).filter(_exclude_lpg_filter())

        today_revenue = completed_txns.aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0')
        transaction_count = completed_txns.count()
        total_fuel_dispensed = completed_txns.aggregate(Sum('liters_dispensed'))['liters_dispensed__sum'] or Decimal('0')
        pending_payments = Transaction.objects.filter(status='pending').filter(_exclude_lpg_filter()).count()
        
        # Get recent transactions
        recent_transactions = completed_txns.select_related(
            'pump', 'fuel_type', 'customer', 'staff'
        ).order_by('-created_at')[:5]
        
        recent_txns_data = []
        for txn in recent_transactions:
            recent_txns_data.append({
                'transaction_id': txn.transaction_id,
                'customer_name': txn.customer.first_name + ' ' + txn.customer.last_name if txn.customer else 'N/A',
                'fuel_type': txn.fuel_type.get_name_display() if txn.fuel_type else 'N/A',
                'amount': float(txn.total_amount),
                'status': txn.status.capitalize(),
                'time': txn.created_at.strftime('%I:%M %p') if txn.created_at else 'N/A',
            })

        recent_payments = Payment.objects.select_related('transaction', 'transaction__customer').filter(
            transaction__created_at__date=today,
            status='completed',
        ).order_by('-created_at')[:5]

        recent_payments_data = []
        for payment in recent_payments:
            transaction = payment.transaction
            customer_name = payment.customer_name or (transaction.customer.first_name + ' ' + transaction.customer.last_name if transaction.customer else 'N/A')
            recent_payments_data.append({
                'reference': payment.reference,
                'customer_name': customer_name,
                'amount': float(payment.amount),
                'method': payment.payment_method.replace('_', ' ').title(),
                'status': payment.status.capitalize(),
                'time': payment.created_at.strftime('%I:%M %p') if payment.created_at else 'N/A',
            })
        
        # Get fuel inventory status
        fuel_inventories = FuelInventory.objects.select_related('fuel_type').exclude(fuel_type__name__iexact='lpg')
        low_stock_count = sum(1 for inv in fuel_inventories if inv.is_low_stock())
        
        # Get pump status
        pumps = Pump.objects.select_related('fuel_type').exclude(fuel_type__name__iexact='lpg')
        active_pumps = pumps.filter(status='operational').count()
        total_pumps = pumps.count()
        
        return JsonResponse({
            'status': 'success',
            'timestamp': timezone.now().isoformat(),
            'kpis': {
                'today_revenue': float(today_revenue),
                'transaction_count': transaction_count,
                'total_fuel_dispensed': float(total_fuel_dispensed),
                'pending_payments': pending_payments,
                'low_stock_count': low_stock_count,
                'active_pumps': active_pumps,
                'total_pumps': total_pumps,
            },
            'recent_transactions': recent_txns_data,
            'recent_payments': recent_payments_data,
            'signature': _build_dashboard_live_signature(),  # Used to detect changes
        })
    except Exception as e:
        logger.exception(f"Error in dashboard_live_stats_api: {e}")
        return JsonResponse({
            'status': 'error',
            'message': str(e),
        }, status=500)


def google_login(request):
    """
    Redirect to Google OAuth endpoint.
    This proxies to the allauth Google login route so the app uses one
    canonical callback path.
    """
    try:
        return redirect('/accounts/google/login/')
    except Exception:
        messages.error(request, "Google login is not configured. Please contact the administrator.")
        return redirect('login')

def _exclude_lpg_filter():
    """Returns Q filter that includes non-LPG fuel types and NULL fuel types (e.g., Paystack payments)"""
    return Q(fuel_type__isnull=True) | ~Q(fuel_type__name__iexact='lpg')
