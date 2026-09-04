from django.urls import path
from . import views

urlpatterns = [
    path("pay/", views.initialize_payment, name="initialize_payment"),
    path("api/start/", views.initialize_payment_api, name="initialize_payment_api"),
    path("qr/", views.qr_payment, name="qr_payment"),
    path("receipt/<str:reference>/", views.payment_receipt, name="payment_receipt"),
    path("success/", views.payment_success, name="payment_success"),
    path("verify/", views.verify_payment, name="verify_payment"),
    path("verify-api/", views.verify_payment_api, name="verify_payment_api"),
    path("charge/", views.charge_mobile_money, name="charge_mobile_money"),
    path("debug-init/", views.debug_init, name="debug_init"),
    path("debug-verify/", views.debug_verify, name="debug_verify"),
    path("webhook/", views.paystack_webhook, name="paystack_webhook"),
]