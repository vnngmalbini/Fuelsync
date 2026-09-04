from rest_framework import serializers
from .models import (
    FuelType, FuelInventory, Pump, StaffRole, Staff, Customer,
    LoyaltyTier, CustomerReward, Transaction, Receipt, Notification,
    FuelPriceHistory, DailySalesReport, AuditLog
)


class FuelTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = FuelType
        fields = ['id', 'name', 'description', 'current_price', 'price_updated_at']
        read_only_fields = ['price_updated_at']


class FuelInventorySerializer(serializers.ModelSerializer):
    fuel_type_name = serializers.CharField(source='fuel_type.get_name_display', read_only=True)
    is_low_stock = serializers.SerializerMethodField()
    
    class Meta:
        model = FuelInventory
        fields = ['id', 'fuel_type', 'fuel_type_name', 'quantity_liters', 'min_threshold',
                  'max_capacity', 'cost_per_liter', 'is_low_stock', 'last_restocked']
        read_only_fields = ['last_restocked']
    
    def get_is_low_stock(self, obj):
        return obj.is_low_stock()


class PumpSerializer(serializers.ModelSerializer):
    fuel_type_name = serializers.CharField(source='fuel_type.get_name_display', read_only=True)
    
    class Meta:
        model = Pump
        fields = ['id', 'pump_number', 'fuel_type', 'fuel_type_name', 'status', 'meter_reading',
                  'total_liters_dispensed', 'total_revenue', 'installation_date', 'last_maintenance',
                  'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at', 'total_liters_dispensed', 'total_revenue']


class StaffRoleSerializer(serializers.ModelSerializer):
    class Meta:
        model = StaffRole
        fields = ['id', 'name', 'permissions']


class StaffSerializer(serializers.ModelSerializer):
    user_email = serializers.CharField(source='user.email', read_only=True)
    user_first_name = serializers.CharField(source='user.first_name', read_only=True)
    user_last_name = serializers.CharField(source='user.last_name', read_only=True)
    role_name = serializers.CharField(source='role.get_name_display', read_only=True)
    
    class Meta:
        model = Staff
        fields = ['id', 'user', 'user_email', 'user_first_name', 'user_last_name', 'role',
                  'role_name', 'phone', 'national_id', 'date_hired', 'salary', 'is_active',
                  'profile_picture', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']


class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = ['id', 'phone', 'first_name', 'last_name', 'email', 'total_purchases',
                  'total_liters', 'loyalty_points', 'is_registered', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at', 'total_purchases', 'total_liters']


class LoyaltyTierSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoyaltyTier
        fields = ['id', 'name', 'min_points', 'points_multiplier', 'discount_percentage', 'benefits']


class CustomerRewardSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.first_name', read_only=True)
    tier_name = serializers.CharField(source='current_tier.get_name_display', read_only=True)
    
    class Meta:
        model = CustomerReward
        fields = ['id', 'customer', 'customer_name', 'current_tier', 'tier_name',
                  'points_earned', 'points_redeemed', 'last_tier_update']


class TransactionSerializer(serializers.ModelSerializer):
    pump_number = serializers.CharField(source='pump.pump_number', read_only=True)
    fuel_type_name = serializers.CharField(source='fuel_type.get_name_display', read_only=True)
    customer_name = serializers.SerializerMethodField()
    staff_name = serializers.CharField(source='staff.user.get_full_name', read_only=True)
    
    class Meta:
        model = Transaction
        fields = ['id', 'transaction_id', 'pump', 'pump_number', 'customer', 'customer_name',
                  'fuel_type', 'fuel_type_name', 'liters_dispensed', 'price_per_liter',
                  'total_amount', 'payment_method', 'status', 'staff', 'staff_name',
                  'reference_number', 'notes', 'created_at', 'completed_at']
        read_only_fields = ['created_at', 'completed_at']
    
    def get_customer_name(self, obj):
        if obj.customer:
            return f"{obj.customer.first_name} {obj.customer.last_name}"
        return "Walk-in Customer"


class ReceiptSerializer(serializers.ModelSerializer):
    transaction_id = serializers.CharField(source='transaction.transaction_id', read_only=True)
    customer_phone = serializers.CharField(source='transaction.customer.phone', read_only=True)
    
    class Meta:
        model = Receipt
        fields = ['id', 'transaction', 'transaction_id', 'receipt_number', 'customer_phone',
                  'printed', 'email_sent', 'sms_sent', 'loyalty_points_earned',
                  'discount_applied', 'final_amount', 'created_at']
        read_only_fields = ['created_at']


class NotificationSerializer(serializers.ModelSerializer):
    fuel_name = serializers.CharField(source='related_fuel.get_name_display', read_only=True)
    pump_number = serializers.CharField(source='related_pump.pump_number', read_only=True)
    
    class Meta:
        model = Notification
        fields = ['id', 'notification_type', 'title', 'message', 'related_fuel', 'fuel_name',
                  'related_pump', 'pump_number', 'is_read', 'read_at', 'created_at']
        read_only_fields = ['read_at', 'created_at']


class FuelPriceHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = FuelPriceHistory
        fields = ['id', 'diesel', 'petrol', 'super', 'current_rate', 'created_at']
        read_only_fields = ['created_at']


class DailySalesReportSerializer(serializers.ModelSerializer):
    class Meta:
        model = DailySalesReport
        fields = ['id', 'date', 'total_transactions', 'total_liters_sold', 'total_revenue',
                  'total_expenses', 'profit', 'petrol_sold', 'diesel_sold', 'lpg_sold']
        read_only_fields = ['profit']


class AuditLogSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True)
    
    class Meta:
        model = AuditLog
        fields = ['id', 'user', 'username', 'action', 'description', 'ip_address', 'created_at']
        read_only_fields = ['created_at']
