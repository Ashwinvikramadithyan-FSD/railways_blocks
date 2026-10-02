from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone


class UserProfile(models.Model):
    DEPARTMENT_CHOICES = [
        ('WORKER', 'Worker User'),
        ('CTRL', 'Control Officer'),
        ('STATION', 'Station Master'),
        ('DIVISION', 'Division Master'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    password = models.CharField(max_length=128)  # Explicit password field
    department = models.CharField(max_length=10, choices=DEPARTMENT_CHOICES)

    # Worker sub-department (only filled in when department == 'WORKER').
    WORK_DEPARTMENT_CHOICES = [
        ('SMMS', 'SMMS'),
        ('TMS', 'TMS'),
        ('TDMS', 'TDMS'),
    ]
    work_department = models.CharField(max_length=10, choices=WORK_DEPARTMENT_CHOICES, blank=True)

    # Extra registration details.
    phone_number = models.CharField(max_length=10, blank=True)
    division = models.CharField(max_length=200, blank=True)
    section = models.CharField(max_length=200, blank=True)
    station = models.CharField(max_length=200, blank=True)

    # One-way fingerprint of the password so we can refuse a password that
    # another account already uses (the real password hash is salted, so it
    # can't be compared directly).
    password_fingerprint = models.CharField(max_length=64, blank=True, db_index=True)

    def __str__(self):
        return f"{self.user.username} - {self.get_department_display()}"


class AssetRequest(models.Model):
    """A single maintenance work item that flows: AI/worker raises it -> Station
    Master + Division Master approve/reject it -> the assigned worker
    accepts/rejects it -> work happens -> it's completed (or rescheduled)."""

    SOURCE_CHOICES = [
        ('AI', 'AI Suggested'),
        ('WORKER', 'Worker Request'),
    ]
    APPROVAL_CHOICES = [
        ('WAITING', 'Waiting'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Not Approved'),
    ]
    ACCEPT_CHOICES = [
        ('WAITING', 'Waiting'),
        ('ACCEPTED', 'Accepted'),
        ('REJECTED', 'Rejected'),
    ]
    WORK_STATUS_CHOICES = [
        ('OPEN', 'Open'),
        ('ACTIVE', 'Active'),
        ('COMPLETED', 'Completed'),
        ('RESCHEDULED', 'Rescheduled'),
        ('REJECTED', 'Rejected'),
    ]

    request_code = models.CharField(max_length=20, unique=True)
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default='AI')

    section = models.CharField(max_length=120, blank=True)
    km = models.CharField(max_length=20, blank=True)
    asset_type = models.CharField(max_length=120, blank=True)
    asset_code = models.CharField(max_length=30, blank=True)
    work_description = models.CharField(max_length=255, blank=True)

    # Scheduling / asset request details entered from the Worker Add Assets page.
    work_date = models.DateField(null=True, blank=True)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    division = models.CharField(max_length=120, blank=True)
    name = models.CharField(max_length=120, blank=True)
    phone_number = models.CharField(max_length=20, blank=True)
    location = models.CharField(max_length=200, blank=True)
    priority = models.CharField(max_length=20, blank=True, default='Normal')
    work_details = models.CharField(max_length=255, blank=True)
    total_time = models.CharField(max_length=20, blank=True)

    from_junction = models.CharField(max_length=100, blank=True)
    to_junction = models.CharField(max_length=100, blank=True)
    from_station = models.CharField(max_length=100, blank=True)
    to_station = models.CharField(max_length=100, blank=True)

    detected_at = models.DateTimeField(default=timezone.now)
    is_critical = models.BooleanField(default=False)

    created_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name='created_requests'
    )
    worker = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name='assigned_requests'
    )

    station_status = models.CharField(max_length=10, choices=APPROVAL_CHOICES, default='WAITING')
    station_reason = models.CharField(max_length=255, blank=True)
    division_status = models.CharField(max_length=10, choices=APPROVAL_CHOICES, default='WAITING')
    division_reason = models.CharField(max_length=255, blank=True)

    worker_acceptance = models.CharField(max_length=10, choices=ACCEPT_CHOICES, default='WAITING')
    worker_reason = models.CharField(max_length=255, blank=True)
    worker_response_at = models.DateTimeField(null=True, blank=True)
    worker_role = models.CharField(max_length=100, blank=True)

    work_status = models.CharField(max_length=15, choices=WORK_STATUS_CHOICES, default='OPEN')

    track_line = models.CharField(max_length=100, blank=True)
    section_code = models.CharField(max_length=30, blank=True)

    reschedule_start = models.DateTimeField(null=True, blank=True)
    reschedule_end = models.DateTimeField(null=True, blank=True)
    reschedule_reason = models.CharField(max_length=255, blank=True)

    # True when the CURRENT reschedule_start/end/reason above was proposed by the
    # WORKER (via "Not Accept" + pick a new time), not by Station/Division Master.
    # Station Master and Division Master see this flagged on the AI Suggested Work /
    # Current Requests pages and approve or not-approve it like any other item.
    worker_requested_reschedule = models.BooleanField(default=False)

    # Who proposed the CURRENT reschedule time: 'STATION' / 'DIVISION' / 'CTRL' (a master
    # pressed Not Approve and picked a new time), 'WORKER' (worker pressed Not Accept and
    # sent a new time) or 'AI' (the AI moved the worker's own time to a train-free one).
    rescheduled_by = models.CharField(max_length=10, blank=True, default='')

    # True when the Control Office approved this asset because the Station Master and/or the
    # Division Master had not decided by the approval alert time (30 min before the buffer starts).
    control_approved = models.BooleanField(default=False)

    # True when nobody decided in time and the AI approved the asset itself because no train is
    # detected during its buffer, start and end time.
    ai_approved = models.BooleanField(default=False)

    # End-of-work check: when the end time passes the worker is asked "complete or not". If not
    # complete they ask for more time (extension_minutes); the Control Office approves it, the extra
    # time is added from that moment (extended_end) and the area is blocked for trains.
    extended_end = models.DateTimeField(null=True, blank=True)
    extension_minutes = models.PositiveIntegerField(default=0)
    extension_requested_at = models.DateTimeField(null=True, blank=True)

    # --- AI train-free-window detection (source == 'AI') -------------------
    # The worker-registered asset this AI suggestion was built from, and the
    # plain-English result of the live-train check that produced it.
    parent = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL, related_name='ai_children'
    )
    ai_note = models.CharField(max_length=500, blank=True)
    trains_checked = models.PositiveSmallIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.request_code

    @property
    def both_approved(self):
        return self.station_status == 'APPROVED' and self.division_status == 'APPROVED'

    @property
    def any_rejected(self):
        return self.station_status == 'REJECTED' or self.division_status == 'REJECTED'

    @property
    def any_waiting(self):
        return self.station_status == 'WAITING' or self.division_status == 'WAITING'

    @property
    def acceptance_status(self):
        """Combined station+division verdict used across every dashboard."""
        if self.any_rejected:
            return 'REJECTED'
        if self.both_approved:
            return 'APPROVED'
        return 'WAITING'


class Message(models.Model):
    DEPARTMENT_CHOICES = UserProfile.DEPARTMENT_CHOICES

    sender = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_messages')
    sender_department = models.CharField(max_length=10, choices=DEPARTMENT_CHOICES)
    recipient_department = models.CharField(max_length=10, choices=DEPARTMENT_CHOICES)
    recipient = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name='received_messages'
    )
    related_request = models.ForeignKey(
        AssetRequest, null=True, blank=True, on_delete=models.SET_NULL, related_name='messages'
    )
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.sender_department} -> {self.recipient_department}: {self.body[:30]}"

# ---------------------------------------------------------------------------
# Live train tracking ("Where is my train")
# ---------------------------------------------------------------------------

class Station(models.Model):
    code = models.CharField(max_length=10, unique=True)
    name = models.CharField(max_length=120)
    division = models.CharField(max_length=120, blank=True)
    platforms = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.code})"


class Train(models.Model):
    TYPE_CHOICES = [
        ('SUPERFAST', 'Superfast'),
        ('EXPRESS', 'Express'),
        ('MAIL', 'Mail'),
        ('PASSENGER', 'Passenger'),
        ('SUBURBAN', 'Suburban / Local'),
        ('FREIGHT', 'Freight'),
        ('SPECIAL', 'Special'),
    ]

    number = models.CharField(max_length=10, unique=True)
    name = models.CharField(max_length=120)
    train_type = models.CharField(max_length=12, choices=TYPE_CHOICES, default='EXPRESS')
    # Seven 0/1 flags, Monday..Sunday, e.g. "1111111" = daily, "1010100" = Mon/Wed/Fri.
    running_days = models.CharField(max_length=7, default='1111111')
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['number']

    def __str__(self):
        return f"{self.number} {self.name}"


class TrainStop(models.Model):
    """One row of a train's timetable. Times are Indian Standard Time (24-hour)."""
    train = models.ForeignKey(Train, on_delete=models.CASCADE, related_name='stops')
    station = models.ForeignKey(Station, on_delete=models.PROTECT, related_name='stops')
    seq = models.PositiveSmallIntegerField(help_text="Order of the stop, starting at 1.")
    arrival = models.TimeField(null=True, blank=True, help_text="Leave empty at the origin station.")
    departure = models.TimeField(null=True, blank=True, help_text="Leave empty at the last station.")
    day_offset = models.PositiveSmallIntegerField(default=0, help_text="0 = same day the train starts, 1 = next day, ...")
    distance_km = models.PositiveIntegerField(default=0, help_text="Distance from the origin station.")
    platform = models.CharField(max_length=10, blank=True)

    class Meta:
        ordering = ['train', 'seq']
        unique_together = [('train', 'seq')]

    def __str__(self):
        return f"{self.train.number} #{self.seq} {self.station.code}"


class TrainLiveStatus(models.Model):
    STATE_CHOICES = [
        ('NOT_STARTED', 'Not started'),
        ('AT_STATION', 'At station'),
        ('RUNNING', 'Running'),
        ('TERMINATED', 'Terminated'),
        ('CANCELLED', 'Cancelled'),
    ]

    train = models.OneToOneField(Train, on_delete=models.CASCADE, related_name='live')
    state = models.CharField(max_length=12, choices=STATE_CHOICES, default='NOT_STARTED')
    last_seq = models.PositiveSmallIntegerField(null=True, blank=True)
    delay_minutes = models.IntegerField(default=0, help_text="Negative = running early.")
    platform = models.CharField(max_length=10, blank=True)
    last_detected_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=40, blank=True)
    # The Control Office can block (hold) a train that is about to enter a stretch where a worker
    # is working. While held_until is in the future the train is treated as NOT entering that stretch.
    held_until = models.DateTimeField(null=True, blank=True)
    held_reason = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.train.number} - {self.state}"


class DetectionEvent(models.Model):
    EVENT_CHOICES = [
        ('ARRIVED', 'Arrived'),
        ('DEPARTED', 'Departed'),
        ('PASSED', 'Passed (non-stop)'),
    ]

    train = models.ForeignKey(Train, on_delete=models.CASCADE, related_name='events')
    station = models.ForeignKey(Station, on_delete=models.PROTECT, related_name='events')
    stop_seq = models.PositiveSmallIntegerField(null=True, blank=True)
    event = models.CharField(max_length=10, choices=EVENT_CHOICES)
    detected_at = models.DateTimeField()
    scheduled_at = models.DateTimeField(null=True, blank=True)
    delay_minutes = models.IntegerField(default=0)
    platform = models.CharField(max_length=10, blank=True)
    source = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-detected_at', '-id']

    def __str__(self):
        return f"{self.train.number} {self.event} {self.station.code} @ {self.detected_at:%H:%M}"