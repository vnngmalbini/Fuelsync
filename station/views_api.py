from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from django.db.models import Sum, Count, Q, F
from .models import (
    FuelType, FuelInventory, Pump, Staff, Customer, Transaction,
    Receipt, Notification, FuelPriceHistory, DailySalesReport, AuditLog
)
from .serializers import (
    FuelTypeSerializer, FuelInventorySerializer, PumpSerializer,
    StaffSerializer, CustomerSerializer, TransactionSerializer,
    ReceiptSerializer, NotificationSerializer, FuelPriceHistorySerializer,
    DailySalesReportSerializer, AuditLogSerializer
)


class FuelTypeViewSet(viewsets.ModelViewSet):
    """API endpoints for fuel types"""
    queryset = FuelType.objects.all()
    serializer_class = FuelTypeSerializer
    permission_classes = [IsAuthenticated]
    
    @action(detail=True, methods=['post'])
    def update_price(self, request, pk=None):
        """Update fuel price with history tracking"""
        fuel = self.get_object()
        new_price = request.data.get('new_price')
        
        if not new_price:
            return Response({'error': 'new_price is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            new_price = float(new_price)
            if new_price <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return Response({'error': 'new_price must be a positive number'}, status=status.HTTP_400_BAD_REQUEST)

        fuel.current_price = new_price
        fuel.save()
        
        # Snapshot all current fuel prices; current_rate = the price just changed
        prices = {ft.name: float(ft.current_price) for ft in FuelType.objects.all()}
        FuelPriceHistory.objects.create(
            diesel=prices.get('diesel', 0.0),
            petrol=prices.get('petrol', 0.0),
            super=prices.get('super'),
            current_rate=float(new_price),
        )
        
        return Response(FuelTypeSerializer(fuel).data)


class FuelInventoryViewSet(viewsets.ModelViewSet):
    """API endpoints for fuel inventory management"""
    queryset = FuelInventory.objects.all()
    serializer_class = FuelInventorySerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['fuel_type']
    search_fields = ['fuel_type__name']
    
    @action(detail=True, methods=['post'])
    def restock(self, request, pk=None):
        """Add fuel to inventory"""
        inventory = self.get_object()
        liters = request.data.get('liters')
        
        if not liters:
            return Response({'error': 'liters is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        inventory.quantity_liters += float(liters)
        inventory.last_restocked = timezone.now()
        inventory.save()
        
        return Response(FuelInventorySerializer(inventory).data)
    
    @action(detail=False, methods=['get'])
    def low_stock_alerts(self, request):
        """Get all fuels with low stock"""
        low_stock = FuelInventory.objects.filter(
            quantity_liters__lte=F('min_threshold')
        )
        serializer = self.get_serializer(low_stock, many=True)
        return Response(serializer.data)


class PumpViewSet(viewsets.ModelViewSet):
    """API endpoints for pump management"""
    queryset = Pump.objects.all()
    serializer_class = PumpSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['fuel_type', 'status']
    search_fields = ['pump_number']
    ordering_fields = ['pump_number', 'created_at']
    
    @action(detail=True, methods=['post'])
    def update_status(self, request, pk=None):
        """Update pump status"""
        pump = self.get_object()
        new_status = request.data.get('status')
        
        if new_status not in ['operational', 'maintenance', 'offline']:
            return Response({'error': 'Invalid status'}, status=status.HTTP_400_BAD_REQUEST)
        
        pump.status = new_status
        pump.save()
        
        return Response(PumpSerializer(pump).data)
    
    @action(detail=False, methods=['get'])
    def statistics(self, request):
        """Get pump statistics"""
        total_pumps = Pump.objects.count()
        operational = Pump.objects.filter(status='operational').count()
        maintenance = Pump.objects.filter(status='maintenance').count()
        offline = Pump.objects.filter(status='offline').count()
        
        return Response({
            'total': total_pumps,
            'operational': operational,
            'maintenance': maintenance,
            'offline': offline
        })


class StaffViewSet(viewsets.ModelViewSet):
    """API endpoints for staff management"""
    queryset = Staff.objects.select_related('user', 'role')
    serializer_class = StaffSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['role', 'is_active']
    search_fields = ['user__first_name', 'user__last_name', 'phone']
    
    @action(detail=True, methods=['post'])
    def toggle_active(self, request, pk=None):
        """Activate/deactivate staff member"""
        staff = self.get_object()
        staff.is_active = not staff.is_active
        staff.save()
        return Response(StaffSerializer(staff).data)


class CustomerViewSet(viewsets.ModelViewSet):
    """API endpoints for customer management"""
    queryset = Customer.objects.all()
    serializer_class = CustomerSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['is_registered']
    search_fields = ['phone', 'first_name', 'last_name', 'email']
    
    @action(detail=False, methods=['get'])
    def top_customers(self, request):
        """Get top customers by purchase amount"""
        limit = request.query_params.get('limit', 10)
        top = Customer.objects.order_by('-total_purchases')[:int(limit)]
        serializer = self.get_serializer(top, many=True)
        return Response(serializer.data)


class TransactionViewSet(viewsets.ModelViewSet):
    """API endpoints for transactions"""
    queryset = Transaction.objects.select_related('pump', 'customer', 'fuel_type', 'staff')
    serializer_class = TransactionSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'payment_method', 'pump', 'fuel_type']
    search_fields = ['transaction_id', 'reference_number', 'customer__phone']
    ordering_fields = ['created_at', 'total_amount']
    
    @action(detail=False, methods=['get'])
    def daily_summary(self, request):
        """Get today's transaction summary"""
        today = timezone.now().date()
        today_transactions = Transaction.objects.filter(
            created_at__date=today,
            status='completed'
        )
        
        summary = {
            'total_transactions': today_transactions.count(),
            'total_revenue': today_transactions.aggregate(Sum('total_amount'))['total_amount__sum'] or 0,
            'total_liters': today_transactions.aggregate(Sum('liters_dispensed'))['liters_dispensed__sum'] or 0,
            'by_fuel_type': {}
        }
        
        # Group by fuel type
        fuel_summary = today_transactions.values('fuel_type__name').annotate(
            count=Count('id'),
            liters=Sum('liters_dispensed'),
            revenue=Sum('total_amount')
        )
        
        for item in fuel_summary:
            summary['by_fuel_type'][item['fuel_type__name']] = {
                'count': item['count'],
                'liters': float(item['liters'] or 0),
                'revenue': float(item['revenue'] or 0)
            }
        
        return Response(summary)
    
    @action(detail=False, methods=['get'])
    def date_range_summary(self, request):
        """Get summary for a date range"""
        date_from = request.query_params.get('from')
        date_to = request.query_params.get('to')
        
        if not date_from or not date_to:
            return Response({'error': 'from and to dates are required'}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
        transactions = Transaction.objects.filter(
            created_at__date__gte=date_from,
            created_at__date__lte=date_to,
            status='completed'
        )
        
        summary = {
            'total_transactions': transactions.count(),
            'total_revenue': transactions.aggregate(Sum('total_amount'))['total_amount__sum'] or 0,
            'total_liters': transactions.aggregate(Sum('liters_dispensed'))['liters_dispensed__sum'] or 0,
        }
        
        return Response(summary)


class ReceiptViewSet(viewsets.ReadOnlyModelViewSet):
    """API endpoints for receipts"""
    queryset = Receipt.objects.select_related('transaction')
    serializer_class = ReceiptSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['receipt_number', 'transaction__reference_number']
    ordering_fields = ['created_at']


class NotificationViewSet(viewsets.ModelViewSet):
    """API endpoints for notifications"""
    queryset = Notification.objects.all()
    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['notification_type', 'is_read']
    ordering_fields = ['-created_at']
    
    @action(detail=True, methods=['post'])
    def mark_as_read(self, request, pk=None):
        """Mark notification as read"""
        notification = self.get_object()
        notification.is_read = True
        notification.read_at = timezone.now()
        notification.save()
        return Response(NotificationSerializer(notification).data)
    
    @action(detail=False, methods=['post'])
    def mark_all_as_read(self, request):
        """Mark all notifications as read"""
        Notification.objects.filter(is_read=False).update(
            is_read=True,
            read_at=timezone.now()
        )
        return Response({'status': 'All notifications marked as read'})
    
    @action(detail=False, methods=['get'])
    def unread_count(self, request):
        """Get unread notification count"""
        count = Notification.objects.filter(is_read=False).count()
        return Response({'unread_count': count})


class DailySalesReportViewSet(viewsets.ReadOnlyModelViewSet):
    """API endpoints for daily sales reports"""
    queryset = DailySalesReport.objects.all()
    serializer_class = DailySalesReportSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.OrderingFilter]
    ordering_fields = ['date']
    
    @action(detail=False, methods=['get'])
    def current_month(self, request):
        """Get current month's reports"""
        today = timezone.now()
        first_day = today.replace(day=1)
        reports = DailySalesReport.objects.filter(
            date__gte=first_day,
            date__lte=today.date()
        )
        serializer = self.get_serializer(reports, many=True)
        return Response(serializer.data)


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """API endpoints for audit logs"""
    queryset = AuditLog.objects.select_related('user')
    serializer_class = AuditLogSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['action', 'user']
    search_fields = ['description', 'user__username']
    ordering_fields = ['-created_at']
