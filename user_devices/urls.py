# urls.py
from django.urls import path
from django.contrib.auth.views import LoginView, LogoutView
from .views import home_view, toggle_button_status, download_data  # Import your home view or another view to redirect after login
from . import views
from .admin_mqtt import gateway_bundle_view

urlpatterns = [
    path('', views.base_redirect, name='base_redirect'),
    path('login/', LoginView.as_view(), name='login'),  # URL for the login view
    path('logout/', LogoutView.as_view(next_page='login'), name='logout'),  # URL for logout
    path('home/', home_view, name='home'),  # URL for the home view
    # path: accetta anche nomi con "/" (con str il reverse va in NoReverseMatch e la home dà 500)
    path('device/<path:device_name>/', views.device_detail_view, name='device_detail'),  # URL for the device detail view
    path('toggle-button/<int:button_id>/', toggle_button_status, name='toggle_button'),
    path('download-data/', download_data, name='download_data'),  # URL for data download
    path('mqtt/gateway/<int:gateway_pk>/bundle/', gateway_bundle_view, name='gateway_mqtt_bundle'),
]