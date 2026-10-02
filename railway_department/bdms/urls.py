from django.urls import path
from .views import (
    login, register, logout_view,
    worker_dashboard, station_dashboard, division_dashboard, control_dashboard,
    approve_request, reject_request, reschedule_request,
    worker_respond, worker_complete, worker_create_request, worker_delete_request,
    send_message, send_request_message,
)
from .train_views import train_dashboard, train_data, train_detect, train_detection_api, train_seed

urlpatterns = [
    path('train-dashboard/', train_dashboard, name='train_dashboard'),
    path('train/data/', train_data, name='train_data'),
    path('train/detect/', train_detect, name='train_detect'),
    path('train/sample/', train_seed, name='train_seed'),
    path('api/train-detection/', train_detection_api, name='train_detection_api'),
    path('login/', login, name='login'),
    path('register/', register, name='register'),
    path('logout/', logout_view, name='logout'),

    path('worker-dashboard/', worker_dashboard, name='worker_dashboard'),
    path('station-dashboard/', station_dashboard, name='station_dashboard'),
    path('division-dashboard/', division_dashboard, name='division_dashboard'),
    path('control-dashboard/', control_dashboard, name='control_dashboard'),

    path('requests/<int:request_id>/approve/', approve_request, name='approve_request'),
    path('requests/<int:request_id>/reject/', reject_request, name='reject_request'),
    path('requests/<int:request_id>/reschedule/', reschedule_request, name='reschedule_request'),

    path('requests/<int:request_id>/respond/', worker_respond, name='worker_respond'),
    path('requests/<int:request_id>/complete/', worker_complete, name='worker_complete'),
    path('requests/new/', worker_create_request, name='worker_create_request'),
    path('requests/<int:request_id>/withdraw/', worker_delete_request, name='worker_delete_request'),

    path('messages/send/', send_message, name='send_message'),
    path('requests/<int:request_id>/message/', send_request_message, name='send_request_message'),
]