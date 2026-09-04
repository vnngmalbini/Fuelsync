import datetime
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from decimal import Decimal

# ==================== FUEL INVENTORY MODELS ====================

class FuelType(models.Model):
    """Fuel types: Petrol, Diesel, Super, LPG"""
    FUEL_CHOICES = [
        ('petrol', 'Petrol'),
        ('diesel', 'Diesel'),
        ('super', 'Super'),
        ('lpg', 'LPG (Liquefied Petroleum Gas)'),
    ]
    
    name = models.CharField(max_length=50, choices=FUEL_CHOICES, unique=True)
    description = models.TextField(blank=True)
    current_price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    price_updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name_plural = "Fuel Types"
    
    def __str__(self):
        return self.get_name_display()


class FuelInventory(models.Model):
    """Track fuel stock levels"""
    fuel_type = models.OneToOneField(FuelType, on_delete=models.CASCADE, related_name='inventory')
    quantity_liters = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    min_threshold = models.DecimalField(max_digits=12, decimal_places=2, default=1000.00)
    max_capacity = models.DecimalField(max_digits=12, decimal_places=2, default=50000.00)
    last_restocked = models.DateTimeField(null=True, blank=True)
    cost_per_liter = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    
    class Meta:
        verbose_name_plural = "Fuel Inventories"
    
    def __str__(self):
        return f"{self.fuel_type} - {self.quantity_liters}L"
    
    def is_low_stock(self):
        return self.quantity_liters <= self.min_threshold


# ==================== PUMP MODELS ====================

class Pump(models.Model):
    """Individual fuel pumps at the station"""
    PUMP_STATUS_CHOICES = [
        ('operational', 'Operational'),
        ('maintenance', 'Under Maintenance'),
        ('offline', 'Offline'),
    ]
    
    pump_number = models.CharField(max_length=10, unique=True)
    fuel_type = models.ForeignKey(FuelType, on_delete=models.PROTECT)
    status = models.CharField(max_length=20, choices=PUMP_STATUS_CHOICES, default='operational')
    meter_reading = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_liters_dispensed = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_revenue = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    installation_date = models.DateField()
    last_maintenance = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"Pump {self.pump_number} - {self.fuel_type} ({self.status})"


# ==================== STAFF MODELS ====================

class StaffRole(models.Model):
    """Staff roles: Admin, Manager, Attendant"""
    ROLE_CHOICES = [
        ('admin', 'Administrator'),
        ('manager', 'Station Manager'),
        ('attendant', 'Fuel Attendant'),
        ('accountant', 'Accountant'),
    ]
    
    name = models.CharField(max_length=50, choices=ROLE_CHOICES, unique=True)
    permissions = models.TextField(help_text="Comma-separated list of permissions")
    
    def __str__(self):
        return self.get_name_display()


class Staff(models.Model):
    """Staff members at the station"""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='staff_profile')
    role = models.ForeignKey(StaffRole, on_delete=models.PROTECT)
    phone = models.CharField(max_length=20)
    national_id = models.CharField(max_length=50, unique=True, null=True, blank=True)
    date_hired = models.DateField()
    salary = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    is_active = models.BooleanField(default=True)
    profile_picture = models.ImageField(upload_to='staff_profiles/', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name_plural = "Staff"
    
    def __str__(self):
        return f"{self.user.get_full_name()} - {self.role}"


# ==================== CUSTOMER MODELS ====================

class Customer(models.Model):
    """Customer profiles"""
    phone = models.CharField(max_length=20, unique=True)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(null=True, blank=True)
    total_purchases = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    total_liters = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    loyalty_points = models.IntegerField(default=0)
    is_registered = models.BooleanField(default=False)
    user = models.OneToOneField(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='customer_profile')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.first_name} {self.last_name} - {self.phone}"


class LoyaltyTier(models.Model):
    """Loyalty/Rewards tiers"""
    TIER_CHOICES = [
        ('bronze', 'Bronze'),
        ('silver', 'Silver'),
        ('gold', 'Gold'),
        ('platinum', 'Platinum'),
    ]
    
    name = models.CharField(max_length=50, choices=TIER_CHOICES, unique=True)
    min_points = models.IntegerField()
    points_multiplier = models.DecimalField(max_digits=3, decimal_places=2, default=1.00)
    discount_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    benefits = models.TextField()
    
    def __str__(self):
        return f"{self.get_name_display()} (≥{self.min_points} pts)"


class CustomerReward(models.Model):
    """Customer rewards and loyalty tracking"""
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='rewards')
    current_tier = models.ForeignKey(LoyaltyTier, on_delete=models.SET_NULL, null=True)
    points_earned = models.IntegerField(default=0)
    points_redeemed = models.IntegerField(default=0)
    last_tier_update = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.customer} - {self.current_tier}"


# ==================== SALES & TRANSACTION MODELS ====================

class Transaction(models.Model):
    """Sales transactions"""
    PAYMENT_METHOD_CHOICES = [
        ('cash', 'Cash'),
        ('card', 'Credit/Debit Card'),
        ('mobile_money', 'Mobile Money'),
        ('paystack', 'Paystack'),
        ('bank_transfer', 'Bank Transfer'),
    ]
    
    TRANSACTION_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('refunded', 'Refunded'),
    ]
    
    transaction_id = models.CharField(max_length=50, unique=True)
    pump = models.ForeignKey(Pump, on_delete=models.PROTECT, related_name='transactions', null=True, blank=True)
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name='transactions')
    fuel_type = models.ForeignKey(FuelType, on_delete=models.PROTECT, null=True, blank=True)
    liters_dispensed = models.DecimalField(max_digits=10, decimal_places=2)
    price_per_liter = models.DecimalField(max_digits=10, decimal_places=2)
    total_amount = models.DecimalField(max_digits=15, decimal_places=2)
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES)
    status = models.CharField(max_length=20, choices=TRANSACTION_STATUS_CHOICES, default='pending')
    staff = models.ForeignKey(Staff, on_delete=models.SET_NULL, null=True, blank=True)
    reference_number = models.CharField(max_length=100, unique=True, null=True, blank=True)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.transaction_id} - {self.liters_dispensed}L - {self.total_amount}"


class Receipt(models.Model):
    """Digital receipts for transactions"""
    transaction = models.OneToOneField(Transaction, on_delete=models.CASCADE, related_name='receipt')
    receipt_number = models.CharField(max_length=50, unique=True)
    printed = models.BooleanField(default=False)
    email_sent = models.BooleanField(default=False)
    sms_sent = models.BooleanField(default=False)
    loyalty_points_earned = models.IntegerField(default=0)
    discount_applied = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    final_amount = models.DecimalField(max_digits=15, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"Receipt {self.receipt_number}"


# ==================== PAYMENT MODELS ====================

class Payment(models.Model):
    """Payment records"""
    PAYMENT_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    
    transaction = models.OneToOneField(Transaction, on_delete=models.CASCADE, related_name='payment')
    customer_name = models.CharField(max_length=200, blank=True, default='')
    customer_email = models.EmailField(blank=True, default='')
    customer_phone = models.CharField(max_length=20, blank=True, default='')
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    payment_method = models.CharField(max_length=50)
    reference = models.CharField(max_length=100, unique=True)
    status = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default='pending')
    paid_at = models.DateTimeField(null=True, blank=True)
    provider_response = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"Payment {self.reference} - {self.amount}"


# ==================== NOTIFICATION MODELS ====================

class Notification(models.Model):
    """System notifications"""
    NOTIFICATION_TYPE_CHOICES = [
        ('low_stock', 'Low Stock Alert'),
        ('pump_issue', 'Pump Issue'),
        ('high_transaction', 'High Transaction'),
        ('payment_failed', 'Payment Failed'),
        ('system', 'System Alert'),
    ]
    
    notification_type = models.CharField(max_length=50, choices=NOTIFICATION_TYPE_CHOICES)
    title = models.CharField(max_length=200)
    message = models.TextField()
    related_fuel = models.ForeignKey(FuelType, on_delete=models.SET_NULL, null=True, blank=True)
    related_pump = models.ForeignKey(Pump, on_delete=models.SET_NULL, null=True, blank=True)
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.notification_type}: {self.title}"


# ==================== ATTENDANCE ====================

class Attendance(models.Model):
    """Facial-recognition based staff clock in/out events"""
    EVENT_TYPE_CHOICES = [
        ('clock_in', 'Clock In'),
        ('clock_out', 'Clock Out'),
    ]

    staff = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name='attendance_records')
    event_type = models.CharField(max_length=20, choices=EVENT_TYPE_CHOICES)
    confidence = models.FloatField(help_text="LBPH distance at match time (lower = better match)")
    captured_image = models.ImageField(upload_to='attendance_captures/%Y/%m/%d/', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.staff} - {self.get_event_type_display()} @ {self.created_at:%Y-%m-%d %H:%M}"


# ==================== FUEL PRICE HISTORY ====================

class FuelPriceHistory(models.Model):
    """Snapshot of fuel prices (diesel, petrol, LPG) at a point in time"""
    diesel = models.FloatField(default=0.0)
    petrol = models.FloatField(default=0.0)
    super = models.FloatField(null=True, blank=True)
    current_rate = models.FloatField(default=0.0)
    created_at = models.DateField(default=datetime.date.today)
    
    class Meta:
        verbose_name_plural = "Fuel Price Histories"
        ordering = ['-created_at']
    
    def __str__(self):
        return (
            f"Prices on {self.created_at}: diesel={self.diesel}, "
            f"petrol={self.petrol}, super={self.super}, rate={self.current_rate}"
        )


# ==================== ANALYTICS & REPORTS ====================

class DailySalesReport(models.Model):
    """Daily sales summary"""
    date = models.DateField(unique=True)
    total_transactions = models.IntegerField(default=0)
    total_liters_sold = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_revenue = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    total_expenses = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    profit = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    petrol_sold = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    diesel_sold = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    lpg_sold = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    
    class Meta:
        ordering = ['-date']
    
    def __str__(self):
        return f"Sales Report - {self.date}"


class AuditLog(models.Model):
    """Track all important actions for security"""
    ACTION_CHOICES = [
        ('login', 'Login'),
        ('logout', 'Logout'),
        ('price_change', 'Price Change'),
        ('inventory_update', 'Inventory Update'),
        ('transaction', 'Transaction'),
        ('user_created', 'User Created'),
        ('user_deleted', 'User Deleted'),
    ]
    
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=50, choices=ACTION_CHOICES)
    description = models.TextField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.user} - {self.action}"
