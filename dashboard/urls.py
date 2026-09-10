from django.urls import path
from .views import (
    dashboard_home, user_profile, settings_view,
    admin_dashboard, manager_dashboard, attendant_dashboard, customer_dashboard,
    google_login, inventory_page, pumps_page, sales_page,
    customers_page, reports_page, price_history_page, update_price_page, payments_page,
    attendance_dashboard_page, attendance_capture_api, camera_frame_ingest_api,
    camera_latest_frame_api, attendance_staff_photo_upload,
    attendance_self_photo_upload, attendance_self_mark, esp32_camera_stream,
    my_attendance_page, notifications_page,
    dashboard_live_signature, dashboard_live_stats_api, inventory_live_api, pumps_live_api,
)

urlpatterns = [
    # Single unified dashboard
    path("", dashboard_home, name="dashboard"),
    
    # Backward-compatible aliases that now resolve to the same dashboard page
    path("admin/", dashboard_home, name="admin_dashboard"),
    path("manager/", dashboard_home, name="manager_dashboard"),
    path("attendant/", dashboard_home, name="attendant_dashboard"),
    path("customer/", dashboard_home, name="customer_dashboard"),
    
    # User settings
    path("profile/", user_profile, name="profile"),
    path("settings/", settings_view, name="settings"),

    # Dashboard sections
    path("inventory/", inventory_page, name="dashboard_inventory"),
    path("inventory/live/", inventory_live_api, name="dashboard_inventory_live"),
    path("pumps/", pumps_page, name="dashboard_pumps"),
    path("pumps/live/", pumps_live_api, name="dashboard_pumps_live"),
    path("sales/", sales_page, name="dashboard_sales"),
    path("customers/", customers_page, name="dashboard_customers"),
    path("reports/", reports_page, name="dashboard_reports"),
    path("price-history/", price_history_page, name="dashboard_price_history"),
    path("update-price/", update_price_page, name="dashboard_update_price"),
    path("payments/", payments_page, name="dashboard_payments"),
    path("attendance/", attendance_dashboard_page, name="dashboard_attendance"),
    path("attendance/capture/", attendance_capture_api, name="dashboard_attendance_capture"),
    path("attendance/camera/ingest/", camera_frame_ingest_api, name="camera_frame_ingest"),
    path("attendance/camera/latest/", camera_latest_frame_api, name="camera_latest_frame"),
    path("attendance/camera/stream/", esp32_camera_stream, name="esp32_camera_stream"),
    path("attendance/staff/<int:staff_id>/photo/", attendance_staff_photo_upload, name="dashboard_attendance_staff_photo"),
    path("attendance/self/photo/", attendance_self_photo_upload, name="dashboard_attendance_self_photo"),
    path("attendance/self/mark/", attendance_self_mark, name="dashboard_attendance_self_mark"),
    path("my-attendance/", my_attendance_page, name="dashboard_my_attendance"),
    path("notifications/", notifications_page, name="dashboard_notifications"),
    path("live-signature/", dashboard_live_signature, name="dashboard_live_signature"),
    path("api/live-stats/", dashboard_live_stats_api, name="dashboard_live_stats_api"),
    
    # Google OAuth
    path("google/login/", google_login, name="google_oauth_login"),
]
