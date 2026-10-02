from django.urls import path
from .views import (
    login, register, logout_view,
    worker_dashboard, station_dashboard, division_dashboard, control_dashboard,
    approve_request, reject_request, reschedule_request, request_slots, ai_not_approve,
    worker_respond, worker_complete, worker_create_request, worker_delete_request,
    worker_request_slots, worker_available_times, approval_alerts, control_approve_request, block_train,
    completion_prompts, worker_request_extension, control_extend,
    send_message, send_request_message, ai_scan, lifecycle_status,
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
    path('requests/<int:request_id>/slots/', request_slots, name='request_slots'),
    path('requests/<int:request_id>/ai-not-approve/', ai_not_approve, name='ai_not_approve'),

    path('requests/<int:request_id>/respond/', worker_respond, name='worker_respond'),
    path('requests/<int:request_id>/complete/', worker_complete, name='worker_complete'),
    path('requests/new/', worker_create_request, name='worker_create_request'),
    path('requests/available-times/', worker_available_times, name='worker_available_times'),
    path('requests/<int:request_id>/my-slots/', worker_request_slots, name='worker_request_slots'),
    path('requests/<int:request_id>/withdraw/', worker_delete_request, name='worker_delete_request'),

    path('ai/scan/', ai_scan, name='ai_scan'),
    path('lifecycle/status/', lifecycle_status, name='lifecycle_status'),
    path('approval-alerts/', approval_alerts, name='approval_alerts'),
    path('completion-prompts/', completion_prompts, name='completion_prompts'),
    path('requests/<int:request_id>/request-extension/', worker_request_extension, name='worker_request_extension'),
    path('requests/<int:request_id>/control-extend/', control_extend, name='control_extend'),
    path('requests/<int:request_id>/block-train/<str:train_number>/', block_train, name='block_train'),
    path('requests/<int:request_id>/control-approve/', control_approve_request, name='control_approve_request'),
    path('messages/send/', send_message, name='send_message'),
    path('requests/<int:request_id>/message/', send_request_message, name='send_request_message'),
]