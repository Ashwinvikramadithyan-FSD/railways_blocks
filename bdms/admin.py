from django.contrib import admin
from .models import (
    UserProfile, AssetRequest, Message,
    Station, Train, TrainStop, TrainLiveStatus, DetectionEvent,
)

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'department')


@admin.register(AssetRequest)
class AssetRequestAdmin(admin.ModelAdmin):
    list_display = (
        'request_code', 'source', 'asset_code', 'worker',
        'station_status', 'division_status', 'worker_acceptance', 'work_status',
    )
    list_filter = ('source', 'station_status', 'division_status', 'worker_acceptance', 'work_status')


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ('sender', 'sender_department', 'recipient_department', 'related_request', 'created_at')


@admin.register(Station)
class StationAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'division', 'platforms')
    search_fields = ('code', 'name')


class TrainStopInline(admin.TabularInline):
    model = TrainStop
    extra = 1
    ordering = ('seq',)


@admin.register(Train)
class TrainAdmin(admin.ModelAdmin):
    list_display = ('number', 'name', 'train_type', 'running_days', 'is_active')
    list_filter = ('train_type', 'is_active')
    search_fields = ('number', 'name')
    inlines = [TrainStopInline]


@admin.register(TrainLiveStatus)
class TrainLiveStatusAdmin(admin.ModelAdmin):
    list_display = ('train', 'state', 'last_seq', 'delay_minutes', 'platform', 'last_detected_at')


@admin.register(DetectionEvent)
class DetectionEventAdmin(admin.ModelAdmin):
    list_display = ('train', 'station', 'event', 'detected_at', 'delay_minutes', 'platform', 'source')
    list_filter = ('event',)