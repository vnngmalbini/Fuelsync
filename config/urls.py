from django.contrib import admin
from django.urls import path, include
from django.views.generic import RedirectView
from django.contrib.auth import views as auth_views
from dashboard.views import signup, PhoneLoginView, logout_view
from django.conf import settings
from django.conf.urls.static import static
from rest_framework import permissions
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView

from station.views import home, server_output_page, server_output_stream

urlpatterns = [
    # Serve favicon at site root so browsers find it at /favicon.ico
    path('favicon.ico', RedirectView.as_view(url=settings.STATIC_URL + 'images/favicon.ico', permanent=True)),
    path('admin/', admin.site.urls),
    # API Documentation
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
    
    # API Routes
    path('api/v1/station/', include('station.urls_api')),
    path('api/v1/payment/', include('payment.urls_api')),
    
    # Authentication
    path('api/auth/', include('rest_framework.urls')),
    
    # Show the home page first when users open the site root
    path('', home, name='root'),
    
    # Dashboard and main app URLs
    path('dashboard/', include('dashboard.urls')),
    path('accounts/login/', PhoneLoginView.as_view(), name='login'),
    path('accounts/logout/', logout_view, name='logout'),
    path('accounts/signup/', signup, name='signup'),
    # Include django-allauth URLs for social authentication (Google)
    path('accounts/', include('allauth.urls')),
    
    # Station URLs
    path('', include('station.urls')),
    
    # Payment URLs
    path('payment/', include('payment.urls')),
    # Server output viewer (development only)
    path('server-output/', server_output_page, name='server_output'),
    path('server-output/stream/', server_output_stream, name='server_output_stream'),
]

# Serve media files in development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
