from django.urls import path
from . import views

urlpatterns = [
    # Public pages
    path("", views.home, name="home"),
    path("home/", views.home, name="home_page"),
    path("about/", views.about, name="about"),
    path("how-it-works/", views.how_it_works, name="how_it_works"),
    path("contact/", views.contact, name="contact"),
    path("attendance/capture/", views.attendance_public_capture, name="attendance_public_capture"),
    
    # Fuel sale workflow
    path("buy/", views.buy_fuel, name="buy"),
    path("fuel-sale/start/", views.start_fuel_sale, name="start_fuel_sale"),
    path("fuel-sale/pump/<int:pump_id>/", views.fuel_sale_form, name="fuel_sale_form"),
    path("fuel-sale/review/", views.fuel_sale_review, name="fuel_sale_review"),
    path("fuel-sale/complete/", views.fuel_sale_complete, name="fuel_sale_complete"),
    path("fuel-sale/receipt/<int:receipt_id>/", views.fuel_sale_receipt, name="fuel_sale_receipt"),
    path("fuel-sale/receipt/<int:receipt_id>/print/", views.fuel_sale_print_receipt, name="fuel_sale_print_receipt"),
    
    # Shift operations
    path("shift-summary/", views.shift_summary, name="shift_summary"),
    
    # Receipt lookup
    path("receipt/", views.receipt, name="receipt"),
]