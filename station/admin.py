from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from .models import (
    FuelType, FuelInventory, Pump, StaffRole, Staff, Customer,
    LoyaltyTier, CustomerReward, Transaction, Receipt, Notification,
    FuelPriceHistory, DailySalesReport, AuditLog, Payment, Attendance
)



@admin.register(FuelType)
class FuelTypeAdmin(admin.ModelAdmin):
    list_display = ['name', 'current_price', 'price_updated_at']
    list_editable = ['current_price']
    search_fields = ['name']
    readonly_fields = ['price_updated_at']
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'description')
        }),
        ('Pricing', {
            'fields': ('current_price', 'price_updated_at')
        }),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.exclude(name__iexact='lpg')


@admin.register(FuelInventory)
class FuelInventoryAdmin(admin.ModelAdmin):
    list_display = ['fuel_type', 'quantity_liters', 'min_threshold', 'stock_status', 'last_restocked']
    list_filter = ['fuel_type', 'last_restocked']
    readonly_fields = ['last_restocked']
    
    fieldsets = (
        ('Fuel Type', {
            'fields': ('fuel_type',)
        }),
        ('Stock Levels', {
            'fields': ('quantity_liters', 'min_threshold', 'max_capacity')
        }),
        ('Cost', {
            'fields': ('cost_per_liter',)
        }),
        ('History', {
            'fields': ('last_restocked',),
            'classes': ('collapse',)
        }),
    )
    
    def stock_status(self, obj):
        if obj.is_low_stock():
            return format_html(
                '<span style="color: red; font-weight: bold;">{}</span>',
                '⚠ LOW STOCK'
            )
        return format_html(
            '<span style="color: green;">{}</span>',
            '✓ OK'
        )
    stock_status.short_description = 'Status'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.exclude(fuel_type__name__iexact='lpg')


@admin.register(Pump)
class PumpAdmin(admin.ModelAdmin):
    list_display = ['pump_number', 'fuel_type', 'status_badge', 'meter_reading', 'total_revenue']
    list_filter = ['status', 'fuel_type', 'installation_date']
    search_fields = ['pump_number']
    readonly_fields = ['total_liters_dispensed', 'total_revenue', 'created_at', 'updated_at']
    
    fieldsets = (
        ('Pump Details', {
            'fields': ('pump_number', 'fuel_type', 'status')
        }),
        ('Meter Information', {
            'fields': ('meter_reading', 'total_liters_dispensed', 'total_revenue')
        }),
        ('Maintenance', {
            'fields': ('installation_date', 'last_maintenance')
        }),
        ('System', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def status_badge(self, obj):
        colors = {
            'operational': 'green',
            'maintenance': 'orange',
            'offline': 'red',
        }
        color = colors.get(obj.status, 'gray')
        return format_html(
            '<span style="color: {}; font-weight: bold;">● {}</span>',
            color,
            obj.get_status_display(),
        )
    status_badge.short_description = 'Status'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.exclude(fuel_type__name__iexact='lpg')


@admin.register(StaffRole)
class StaffRoleAdmin(admin.ModelAdmin):
    list_display = ['name']


@admin.register(Staff)
class StaffAdmin(admin.ModelAdmin):
    list_display = ['user_full_name', 'role', 'phone', 'is_active', 'salary']
    list_filter = ['role', 'is_active', 'date_hired']
    search_fields = ['user__first_name', 'user__last_name', 'phone']
    readonly_fields = ['created_at', 'updated_at']
    
    fieldsets = (
        ('User Account', {
            'fields': ('user',)
        }),
        ('Personal Information', {
            'fields': ('phone', 'national_id', 'profile_picture')
        }),
        ('Employment', {
            'fields': ('role', 'date_hired', 'salary', 'is_active')
        }),
        ('System', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def user_full_name(self, obj):
        return obj.user.get_full_name() or obj.user.username
    user_full_name.short_description = 'Name'


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ['phone', 'full_name', 'email', 'total_purchases', 'loyalty_points']
    list_filter = ['is_registered', 'created_at']
    search_fields = ['phone', 'first_name', 'last_name', 'email']
    readonly_fields = ['total_purchases', 'total_liters', 'created_at', 'updated_at']
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('phone', 'first_name', 'last_name', 'email')
        }),
        ('Purchase History', {
            'fields': ('total_purchases', 'total_liters', 'loyalty_points')
        }),
        ('Account', {
            'fields': ('user', 'is_registered')
        }),
        ('System', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def full_name(self, obj):
        return f"{obj.first_name} {obj.last_name}"
    full_name.short_description = 'Full Name'


@admin.register(LoyaltyTier)
class LoyaltyTierAdmin(admin.ModelAdmin):
    list_display = ['name', 'min_points', 'points_multiplier', 'discount_percentage']
    list_editable = ['points_multiplier', 'discount_percentage']


@admin.register(CustomerReward)
class CustomerRewardAdmin(admin.ModelAdmin):
    list_display = ['customer', 'current_tier', 'points_earned', 'points_redeemed']
    list_filter = ['current_tier']
    search_fields = ['customer__phone', 'customer__first_name']
    readonly_fields = ['last_tier_update']


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ['transaction_id', 'pump', 'customer_display', 'liters_dispensed', 'total_amount', 'status']
    list_filter = ['status', 'payment_method', 'fuel_type', 'created_at']
    search_fields = ['transaction_id', 'customer__phone', 'reference_number']
    readonly_fields = ['transaction_id', 'created_at', 'completed_at', 'total_amount']
    
    fieldsets = (
        ('Transaction Details', {
            'fields': ('transaction_id', 'pump', 'customer', 'fuel_type', 'status')
        }),
        ('Fuel Dispensed', {
            'fields': ('liters_dispensed', 'price_per_liter', 'total_amount')
        }),
        ('Payment', {
            'fields': ('payment_method', 'reference_number', 'staff')
        }),
        ('Notes', {
            'fields': ('notes',),
            'classes': ('collapse',)
        }),
        ('System', {
            'fields': ('created_at', 'completed_at'),
            'classes': ('collapse',)
        }),
    )
    
    def customer_display(self, obj):
        if obj.customer:
            return f"{obj.customer.first_name} {obj.customer.last_name}"
        return "Walk-in"
    customer_display.short_description = 'Customer'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.exclude(fuel_type__name__iexact='lpg')


@admin.register(Receipt)
class ReceiptAdmin(admin.ModelAdmin):
    list_display = ['receipt_number', 'transaction', 'printed', 'email_sent', 'sms_sent']
    list_filter = ['printed', 'email_sent', 'sms_sent', 'created_at']
    search_fields = ['receipt_number', 'transaction__reference_number']
    readonly_fields = ['created_at']

    def has_add_permission(self, request):
        # Receipts are created by the system; only superusers may add via admin
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        # Prevent deletion except by superusers
        return request.user.is_superuser


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ['reference', 'customer_name', 'customer_phone', 'amount', 'status', 'paid_at', 'created_at']
    list_filter = ['status', 'payment_method', 'created_at', 'paid_at']
    search_fields = ['reference', 'customer_name', 'customer_email', 'customer_phone', 'transaction__transaction_id']
    readonly_fields = ['created_at', 'updated_at', 'paid_at']

    fieldsets = (
        ('Payment Details', {
            'fields': ('transaction', 'reference', 'status', 'payment_method', 'amount', 'paid_at')
        }),
        ('Customer Details', {
            'fields': ('customer_name', 'customer_email', 'customer_phone')
        }),
        ('Provider', {
            'fields': ('provider_response',),
            'classes': ('collapse',),
        }),
        ('System', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ['notification_type', 'title', 'is_read', 'created_at']
    list_filter = ['notification_type', 'is_read', 'created_at']
    search_fields = ['title', 'message']
    readonly_fields = ['created_at', 'read_at']

    def has_add_permission(self, request):
        # Only superusers create notifications via admin
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(FuelPriceHistory)
class FuelPriceHistoryAdmin(admin.ModelAdmin):
    list_display = ['id', 'diesel', 'petrol', 'super', 'current_rate', 'created_at']
    list_filter = ['created_at']
    readonly_fields = ['created_at']


@admin.register(Attendance)
class AttendanceAdmin(admin.ModelAdmin):
    list_display = ['staff', 'event_type', 'confidence', 'created_at']
    list_filter = ['event_type', 'created_at']
    search_fields = ['staff__user__first_name', 'staff__user__last_name']
    readonly_fields = ['created_at']


@admin.register(DailySalesReport)
class DailySalesReportAdmin(admin.ModelAdmin):
    list_display = ['date', 'total_transactions', 'total_liters_sold', 'total_revenue', 'profit']
    list_filter = ['date']
    readonly_fields = ['date', 'total_transactions', 'total_liters_sold', 'total_revenue', 'total_expenses', 'profit']
    
    def has_add_permission(self, request):
        return False
    
    def has_delete_permission(self, request, obj=None):
        return False
    
    def has_change_permission(self, request, obj=None):
        return False


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ['user_name', 'action', 'created_at', 'ip_address']
    list_filter = ['action', 'created_at']
    search_fields = ['user__username', 'description']
    readonly_fields = ['created_at']
    
    def user_name(self, obj):
        return obj.user.username if obj.user else "System"
    user_name.short_description = 'User'
    
    def has_add_permission(self, request):
        return False
    
    def has_delete_permission(self, request, obj=None):
        return False
    
    def has_change_permission(self, request, obj=None):
        return False
