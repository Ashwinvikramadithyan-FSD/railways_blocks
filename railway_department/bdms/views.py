from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.utils import timezone
from django.utils.dateparse import parse_datetime, parse_date, parse_time
from django.db.models import Q
from django.urls import reverse
from datetime import timedelta, datetime as _datetime, time as _time

from .forms import RegisterForm, password_fingerprint
from .models import UserProfile, AssetRequest, Message
from .railway_data import RAILWAY_DATA


# ---------------------------------------------------------------------------
# Display-label translation: the dashboards use their own wording for the
# same underlying status codes, so every payload builder below goes through
# these maps rather than showing raw DB codes.
# ---------------------------------------------------------------------------

STATUS_LABEL = {'WAITING': 'WAITING', 'APPROVED': 'APPROVED', 'REJECTED': 'NOT APPROVED'}
STATUS_KEY = {'WAITING': 'pending', 'APPROVED': 'approved', 'REJECTED': 'rejected'}
ACCEPT_LABEL = {'WAITING': 'WAITING', 'ACCEPTED': 'ACCEPTED', 'REJECTED': 'NOT ACCEPTED'}
WORK_LABEL = {'OPEN': 'OPEN', 'ACTIVE': 'IN PROGRESS', 'COMPLETED': 'CLOSED', 'RESCHEDULED': 'SCHEDULED', 'REJECTED': 'REJECTED'}


def _fmt_date(dt):
    return timezone.localtime(dt).strftime('%d %b') if dt else ''


def _fmt_time(dt):
    return timezone.localtime(dt).strftime('%H:%M') if dt else ''


def _fmt_dt_short(dt):
    return timezone.localtime(dt).strftime('%d %b, %H:%M') if dt else ''


def _fmt_iso(dt):
    return timezone.localtime(dt).isoformat() if dt else None


def _schedule_window(item):
    """The working window every dashboard shows for a request.

    Priority order:
      1. a reschedule (reschedule_start / reschedule_end) if one was made;
      2. the block date + start/end time the worker entered on the Add Assets
         form (work_date, start_time, end_time);
      3. otherwise the detection time (AI items with no block date) plus a
         default 2-hour window.
    The date is therefore the *block date*, not the day the asset was added."""
    if item.reschedule_start:
        start = item.reschedule_start
        end = item.reschedule_end or (start + timedelta(hours=2))
        return start, end

    if item.work_date:
        tz = timezone.get_current_timezone()
        start = timezone.make_aware(_datetime.combine(item.work_date, item.start_time or _time(0, 0)), tz)
        if item.end_time:
            end = timezone.make_aware(_datetime.combine(item.work_date, item.end_time), tz)
            if end <= start:            # block runs past midnight
                end += timedelta(days=1)
        else:
            end = start + timedelta(hours=2)
        return start, end

    start = item.detected_at
    return start, start + timedelta(hours=2)


def _fmt_duration(start, end):
    if not start or not end:
        return '—'
    mins = int((end - start).total_seconds() // 60)
    if mins <= 0:
        return '—'
    return ' '.join(p for p in (f"{mins // 60}h" if mins // 60 else '', f"{mins % 60}m" if mins % 60 else '') if p)


def _block_fields(r, start, end):
    """Block time / start / end shown on the control Reschedule page: what the
    worker entered on the Add Asset form, otherwise the request's schedule window."""
    if r.start_time and r.end_time and r.work_status != 'RESCHEDULED':
        s, e = r.start_time.strftime('%H:%M'), r.end_time.strftime('%H:%M')
        mins = (r.end_time.hour * 60 + r.end_time.minute) - (r.start_time.hour * 60 + r.start_time.minute)
        if mins < 0:
            mins += 24 * 60
        block = r.total_time or (' '.join(p for p in (f"{mins // 60}h" if mins // 60 else '', f"{mins % 60}m" if mins % 60 else '') if p) or '—')
        return {'blockTime': block, 'startTime': s, 'endTime': e}
    return {'blockTime': _fmt_duration(start, end), 'startTime': _fmt_time(start), 'endTime': _fmt_time(end)}


def _sections_and_index(all_requests):
    """Builds the fixed 'S' section-reference list Station/Division Master
    pages use for their route overview, from whichever real requests exist
    (grouped by the free-text `section` field)."""
    sections, index = [], {}
    for r in all_requests.order_by('id'):
        name = r.section or 'Unassigned'
        if name not in index:
            index[name] = len(sections)
            sections.append({
                'n': name, 'fj': r.from_junction, 'tj': r.to_junction,
                'fs': r.from_station, 'ts': r.to_station,
                'km': r.km, 'ln': r.track_line, 'id': r.section_code or f"SEC {len(sections) + 1}",
                'loc': r.location,
            })
        elif r.location and not sections[index[name]]['loc']:
            # Fill in the work-location text from whichever request in this
            # section/division actually has one set.
            sections[index[name]]['loc'] = r.location
    if not sections:
        sections.append({'n': 'Unassigned', 'fj': '', 'tj': '', 'fs': '', 'ts': '', 'km': '', 'ln': '', 'id': 'SEC 1', 'loc': ''})
    return sections, index


def _serialize_ai(item, sec_index):
    start, end = _schedule_window(item)
    return {
        'reqId': item.id, 't': item.work_description, 'p': item.priority,
        's': sec_index.get(item.section or 'Unassigned', 0),
        'km': item.km, 'as': f"{item.asset_type} {item.asset_code}".strip(),
        'dt': _fmt_dt_short(item.detected_at),
        'dISO': timezone.localtime(item.detected_at).date().isoformat(),
        'date': _fmt_date(start), 'stm': _fmt_time(start), 'etm': _fmt_time(end),
        'd': item.work_description,
        'worker': item.worker.username if item.worker else None,
    }


def _serialize_request_for(item, sec_index, own_field):
    """own_field is 'station_status' or 'division_status' — whichever this
    dashboard is the approver for. The other one is shown read-only as the
    'peer' column. 'ct' is this dashboard's OWN decision (the column used to
    be a non-decision-making "Control Office" column; that layer doesn't
    exist as an approver in the data model, so it now shows the own
    department's own approval instead — 'Station Control' / 'Division
    Control' depending on which dashboard is rendering it)."""
    peer_field = 'division_status' if own_field == 'station_status' else 'station_status'
    own_status = getattr(item, own_field)
    peer_status = getattr(item, peer_field)
    own_reason = item.station_reason if own_field == 'station_status' else item.division_reason
    decision = {'WAITING': None, 'APPROVED': 'APPROVED', 'REJECTED': 'NOT APPROVED'}[own_status]
    start, end = _schedule_window(item)
    return {
        'reqId': item.id, 'id': item.request_code, 'w': item.work_description,
        'pr': (item.priority or 'Normal').title(),
        's': sec_index.get(item.section or 'Unassigned', 0), 'km': item.km,
        'date': _fmt_date(start), 'stm': _fmt_time(start), 'etm': _fmt_time(end),
        'st': WORK_LABEL.get(item.work_status, item.work_status),
        'dv': STATUS_LABEL[peer_status],
        'ct': STATUS_LABEL[own_status],
        'wk': item.worker.username if item.worker else None,
        'rl': item.worker_role or '—',
        'ac': ACCEPT_LABEL[item.worker_acceptance],
        'rp': _fmt_dt_short(item.worker_response_at) if item.worker_response_at else '—',
        'online': item.work_status == 'ACTIVE',
        'decision': decision, 'reason': own_reason,
        'resch': item.work_status == 'RESCHEDULED',
    }


def _messages_by_request(all_requests, own_department):
    """One message thread per work request, between this dashboard's
    department and the worker assigned to that request."""
    result = {}
    for item in all_requests.filter(worker__isnull=False):
        thread = item.messages.filter(
            Q(sender_department=own_department, recipient_department='WORKER') |
            Q(sender_department='WORKER', recipient_department=own_department)
        ).order_by('created_at')
        result[item.request_code] = [
            {'dir': 'out' if m.sender_department == own_department else 'in',
             't': m.body, 'time': _fmt_time(m.created_at)}
            for m in thread
        ]
    return result


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def register(request):
    if request.method == 'POST':
        form = RegisterForm(request.POST)

        if form.is_valid():
            cd = form.cleaned_data
            user = User.objects.create_user(
                username=cd['username'], email=cd['email'], password=cd['password'],
            )
            UserProfile.objects.create(
                user=user,
                password=user.password,  # store the hashed password, never plain text
                department=cd['department'],
                work_department=cd['work_department'],
                phone_number=cd['phone_number'],
                division=cd['division'],
                section=cd['section'],
                station=cd['station'],
                password_fingerprint=password_fingerprint(cd['password']),
            )

            # Sign the new user straight in and send them to the page that
            # matches the character they picked.
            new_user = authenticate(request, username=cd['username'], password=cd['password'])
            if new_user is not None:
                auth_login(request, new_user)
                messages.success(request, "Account created successfully!")
                return _redirect_to_dashboard(new_user)
            messages.success(request, "Account created successfully! Please sign in.")
            return redirect('login')

        return render(request, 'register.html', {'form': form})

    return render(request, 'register.html', {'form': RegisterForm()})


def login(request):
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '').strip()

        if not username or not password:
            messages.error(request, "Please enter both username and password.")
            return render(request, 'login.html')

        if not User.objects.filter(username__iexact=username).exists():
            messages.error(request, "This username is not registered.")
            return render(request, 'login.html')

        user = authenticate(request, username=username, password=password)

        if user is not None:
            auth_login(request, user)
            return _redirect_to_dashboard(user)
        else:
            messages.error(request, "Incorrect password.")
            return render(request, 'login.html')

    return render(request, 'login.html')


def logout_view(request):
    auth_logout(request)
    messages.success(request, "You have been logged out.")
    return redirect('login')


# ---------------------------------------------------------------------------
# Dashboard routing
# ---------------------------------------------------------------------------

DEPARTMENT_DASHBOARD_URL = {
    'WORKER': 'worker_dashboard',
    'CTRL': 'control_dashboard',
    'STATION': 'station_dashboard',
    'DIVISION': 'division_dashboard',
}


def _get_profile(user):
    return UserProfile.objects.filter(user=user).first()


def _redirect_to_dashboard(user, tab=None):
    """Send the user to the dashboard that matches their registered department.
    If `tab` is given (or a 'tab' field was posted), the URL keeps a #hash so
    the page's JS can reopen the same tab the action was triggered from,
    instead of always landing back on the Dashboard tab."""
    profile = _get_profile(user)
    department = profile.department if profile else None
    url_name = DEPARTMENT_DASHBOARD_URL.get(department, 'login')
    url = reverse(url_name) if url_name != 'login' else reverse('login')
    if tab:
        url = f"{url}#{tab}"
    return redirect(url)


def _require_department(request, department):
    """Return the profile if it matches, else None (caller redirects away)."""
    profile = _get_profile(request.user)
    if not profile or profile.department != department:
        return None
    return profile


# ---------------------------------------------------------------------------
# Station Master dashboard
# ---------------------------------------------------------------------------

@login_required(login_url='login')
def station_dashboard(request):
    profile = _require_department(request, 'STATION')
    if not profile:
        return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))

    today = timezone.localdate()
    all_requests = AssetRequest.objects.all()
    sections, sec_index = _sections_and_index(all_requests)
    page_data = {
        'S': sections,
        'A': [_serialize_ai(r, sec_index) for r in all_requests.filter(source='AI').order_by('-detected_at')],
        'R': [_serialize_request_for(r, sec_index, 'station_status') for r in all_requests],
        'M': _messages_by_request(all_requests, 'STATION'),
        'today': today.isoformat(),
        'workers': list(User.objects.filter(profile__department='WORKER').values('id', 'username')),
        'contacts': _serialize_conversations(profile, 'STATION'),
        'todayAssets': [
            _serialize_ai(r, sec_index)
            for r in all_requests.filter(detected_at__date=today).order_by('-is_critical', '-detected_at')
        ],
    }

    context = {
        'profile': profile,
        'page_data': page_data,
        'today_priority': all_requests.filter(detected_at__date=today).order_by('-is_critical', '-detected_at'),
        'open_requests': all_requests.filter(work_status='OPEN'),
        'open_requests_count': all_requests.filter(work_status='OPEN').count(),
        'station_waiting_count': all_requests.filter(station_status='WAITING').count(),
        'ai_suggestions': all_requests.filter(source='AI'),
        'ai_suggestions_count': all_requests.filter(source='AI').count(),
        'ai_critical_count': all_requests.filter(source='AI', is_critical=True).count(),
        'workers_assigned_count': all_requests.filter(worker__isnull=False).count(),
        'workers_accepted_count': all_requests.filter(worker_acceptance='ACCEPTED').count(),
        'workers_active_count': all_requests.filter(work_status='ACTIVE').count(),
        'workers_waiting_count': all_requests.filter(worker_acceptance='WAITING').count(),
        'division_pending_count': all_requests.filter(station_status='APPROVED', division_status='WAITING').count(),
        'division_approved_count': all_requests.filter(division_status='APPROVED').count(),
        'division_not_approved_count': all_requests.filter(division_status='REJECTED').count(),
        'all_requests': all_requests,
        'workers': User.objects.filter(profile__department='WORKER'),
        'message_departments': [d for d in UserProfile.DEPARTMENT_CHOICES if d[0] != 'STATION'],
        'conversations': _build_conversations(profile, 'STATION'),
    }
    return render(request, 'station_master.html', context)


# ---------------------------------------------------------------------------
# Division Master dashboard (mirrors Station Master)
# ---------------------------------------------------------------------------

@login_required(login_url='login')
def division_dashboard(request):
    profile = _require_department(request, 'DIVISION')
    if not profile:
        return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))

    today = timezone.localdate()
    all_requests = AssetRequest.objects.all()
    sections, sec_index = _sections_and_index(all_requests)
    page_data = {
        'S': sections,
        'A': [_serialize_ai(r, sec_index) for r in all_requests.filter(source='AI').order_by('-detected_at')],
        'R': [_serialize_request_for(r, sec_index, 'division_status') for r in all_requests],
        'M': _messages_by_request(all_requests, 'DIVISION'),
        'today': today.isoformat(),
        'workers': list(User.objects.filter(profile__department='WORKER').values('id', 'username')),
        'contacts': _serialize_conversations(profile, 'DIVISION'),
        'todayAssets': [
            _serialize_ai(r, sec_index)
            for r in all_requests.filter(detected_at__date=today).order_by('-is_critical', '-detected_at')
        ],
    }

    context = {
        'profile': profile,
        'page_data': page_data,
        'today_priority': all_requests.filter(detected_at__date=today).order_by('-is_critical', '-detected_at'),
        'open_requests': all_requests.filter(work_status='OPEN'),
        'open_requests_count': all_requests.filter(work_status='OPEN').count(),
        'division_waiting_count': all_requests.filter(division_status='WAITING').count(),
        'ai_suggestions': all_requests.filter(source='AI'),
        'ai_suggestions_count': all_requests.filter(source='AI').count(),
        'ai_critical_count': all_requests.filter(source='AI', is_critical=True).count(),
        'workers_assigned_count': all_requests.filter(worker__isnull=False).count(),
        'workers_accepted_count': all_requests.filter(worker_acceptance='ACCEPTED').count(),
        'workers_active_count': all_requests.filter(work_status='ACTIVE').count(),
        'workers_waiting_count': all_requests.filter(worker_acceptance='WAITING').count(),
        'station_pending_count': all_requests.filter(division_status='APPROVED', station_status='WAITING').count(),
        'station_approved_count': all_requests.filter(station_status='APPROVED').count(),
        'station_not_approved_count': all_requests.filter(station_status='REJECTED').count(),
        'all_requests': all_requests,
        'workers': User.objects.filter(profile__department='WORKER'),
        'message_departments': [d for d in UserProfile.DEPARTMENT_CHOICES if d[0] != 'DIVISION'],
        'conversations': _build_conversations(profile, 'DIVISION'),
    }
    return render(request, 'divition_master.html', context)


# ---------------------------------------------------------------------------
# Control Office dashboard
# ---------------------------------------------------------------------------

@login_required(login_url='login')
def control_dashboard(request):
    profile = _require_department(request, 'CTRL')
    if not profile:
        return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))

    all_requests = AssetRequest.objects.all()
    # Division and Section are separate values entered on the worker's request form.
    total_divisions = all_requests.exclude(work_status='COMPLETED').values('division').distinct().count()
    divisions = sorted({r.division for r in all_requests if r.division}) or ['Unassigned']
    sections, sec_index = _sections_and_index(all_requests)

    def _ctrl_row(r):
        start, end = _schedule_window(r)
        return {
            'internalId': r.id,
            'reqId': r.request_code, 'division': r.division or 'Unassigned', 'work': r.work_description,
            'priority': r.priority.title(),
            **_block_fields(r, start, end),
            # The actual free-text work location entered for the request,
            # falling back to the KM marker only when no location was given.
            'location': r.location or (f"KM {r.km}" if r.km else '—'),
            'fromJunction': r.from_junction, 'toJunction': r.to_junction, 'section': r.section,
            'fromStation': r.from_station, 'toStation': r.to_station, 'km': r.km,
            'dateTime': _fmt_iso(start), 'endDateTime': _fmt_iso(end),
            'stm': _fmt_time(start), 'etm': _fmt_time(end),
            # When this asset was uploaded/raised (shown as Reporting Time on Worker Status).
            'uploadedAt': _fmt_iso(r.created_at),
            'source': 'AI' if r.source == 'AI' else 'Manual',
            'aiStatus': STATUS_KEY[r.acceptance_status],
            'reqStatus': WORK_LABEL.get(r.work_status, r.work_status),
            'approval': {'sm': STATUS_KEY[r.station_status], 'div': STATUS_KEY[r.division_status]},
            'worker': {
                'a': True if r.worker_acceptance == 'ACCEPTED' else (False if r.worker_acceptance == 'REJECTED' else None),
                't': _fmt_time(r.worker_response_at) if r.worker_response_at else '—',
                'name': r.worker.username if r.worker else None,
                'role': r.worker_role or '—',
                'online': r.work_status == 'ACTIVE',
            },
            'reschedule': {'done': r.work_status == 'RESCHEDULED', 'newDT': _fmt_iso(r.reschedule_start), 'reason': r.reschedule_reason or ''},
        }

    page_data = {
        'divisions': divisions,
        'S': sections,
        'data': [_ctrl_row(r) for r in all_requests],
        'M': _messages_by_request(all_requests, 'CTRL'),
        'contacts': _serialize_conversations(profile, 'CTRL'),
    }

    context = {
        'profile': profile,
        'page_data': page_data,
        'total_divisions': total_divisions,
        'current_requests_count': all_requests.count(),
        'ai_suggestions_count': all_requests.filter(source='AI').count(),
        'approved_count': all_requests.filter(station_status='APPROVED', division_status='APPROVED').count(),
        'not_approved_count': all_requests.filter(
            Q(station_status='REJECTED') | Q(division_status='REJECTED')
        ).count(),
        'active_count': all_requests.filter(work_status='ACTIVE').count(),
        'rescheduled_count': all_requests.filter(work_status='RESCHEDULED').count(),
        'all_requests': all_requests,
        'message_departments': [d for d in UserProfile.DEPARTMENT_CHOICES if d[0] != 'CTRL'],
        'conversations': _build_conversations(profile, 'CTRL'),
    }
    return render(request, 'control_dashboard.html', context)


# ---------------------------------------------------------------------------
# Worker dashboard
# ---------------------------------------------------------------------------

@login_required(login_url='login')
def worker_dashboard(request):
    profile = _require_department(request, 'WORKER')
    if not profile:
        return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))

    user = request.user
    my_requests = AssetRequest.objects.filter(Q(worker=user) | Q(created_by=user)).distinct()

    approved = my_requests.filter(station_status='APPROVED', division_status='APPROVED')
    waiting = my_requests.filter(Q(station_status='WAITING') | Q(division_status='WAITING'))
    accepted_all_three = approved.filter(worker_acceptance='ACCEPTED')
    active = my_requests.filter(work_status='ACTIVE')
    completed = my_requests.filter(work_status='COMPLETED')
    rescheduled = my_requests.filter(work_status='RESCHEDULED')

    # Needs the worker's response: fully approved by both departments,
    # assigned to this worker, and this worker hasn't responded yet
    # (this also covers items a station/division master just rescheduled).
    needs_response = my_requests.filter(
        worker=user, station_status='APPROVED', division_status='APPROVED', worker_acceptance='WAITING'
    )

    def _worker_row(r):
        start, end = _schedule_window(r)
        if r.station_status == 'REJECTED' or r.division_status == 'REJECTED':
            stage = 'declined' if r.worker_acceptance == 'REJECTED' else 'pending-approval'
        elif not r.both_approved:
            stage = 'pending-approval'
        elif r.worker_acceptance == 'REJECTED':
            stage = 'declined'
        elif r.worker_acceptance == 'WAITING':
            stage = 'pending-response'
        elif r.work_status == 'COMPLETED':
            stage = 'completed'
        else:
            stage = 'active'
        return {
            'reqId': r.id, 'id': r.request_code, 'work': r.work_description,
            'priority': (r.priority or 'Normal').title(),
            'location': f"Section KM {r.km}" if r.km else (r.section or '—'),
            'fromJunction': r.from_junction, 'toJunction': r.to_junction,
            'fromStation': r.from_station, 'toStation': r.to_station,
            'km': f"{r.km} km" if r.km else '—',
            'dateTime': _fmt_iso(start), 'endDateTime': _fmt_iso(end),
            # Details for the My Requests table.
            'division': r.division or '—', 'section': r.section or '—', 'line': r.track_line or '—',
            'kmValue': r.km or '—',
            'uploadedAt': _fmt_iso(r.created_at),
            'blockDate': r.work_date.isoformat() if r.work_date else '',
            'startTime': r.start_time.strftime('%H:%M') if r.start_time else '',
            'endTime': r.end_time.strftime('%H:%M') if r.end_time else '',
            'totalTime': r.total_time,
            'approval': {
                'sm': STATUS_KEY[r.station_status], 'dc': STATUS_KEY[r.division_status],
            },
            'responseGivenBy': _fmt_time(start),
            'response': True if r.worker_acceptance == 'ACCEPTED' else (False if r.worker_acceptance == 'REJECTED' else None),
            'stage': stage,
            'reschedule': {'rescheduled': r.work_status == 'RESCHEDULED', 'newDateTime': _fmt_iso(r.reschedule_start)},
            'completedAt': _fmt_iso(r.updated_at) if r.work_status == 'COMPLETED' else None,
            'ownedByMe': r.created_by_id == user.id,
        }

    page_data = {
        'requests': [_worker_row(r) for r in my_requests],
        'contacts': _serialize_conversations(profile, 'WORKER'),
        'railway': RAILWAY_DATA,
    }

    context = {
        'profile': profile,
        'page_data': page_data,
        'my_requests': my_requests,
        'my_requests_count': my_requests.count(),
        'approved_count': approved.count(),
        'waiting_count': waiting.count(),
        'accepted_count': accepted_all_three.count(),
        'active_count': active.count(),
        'completed_count': completed.count(),
        'rescheduled_count': rescheduled.count(),
        'needs_response': needs_response,
        'active_list': active,
        'completed_list': completed,
        'rescheduled_list': rescheduled,
        'message_departments': [d for d in UserProfile.DEPARTMENT_CHOICES if d[0] != 'WORKER'],
        'conversations': _build_conversations(profile, 'WORKER'),
    }
    return render(request, 'worker_dashboard.html', context)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _build_conversations(profile, own_department):
    """Group messages into one thread per other department, for the
    Communication / Messages page of each dashboard."""
    conversations = []
    for code, label in UserProfile.DEPARTMENT_CHOICES:
        if code == own_department:
            continue
        thread = Message.objects.filter(
            Q(sender_department=own_department, recipient_department=code) |
            Q(sender_department=code, recipient_department=own_department)
        ).order_by('created_at')
        conversations.append({'code': code, 'label': label, 'messages': thread})
    return conversations


def _serialize_conversations(profile, own_department):
    """JSON-serializable version of _build_conversations, for dashboards whose
    Communication tab is rendered from page_data on the client side."""
    result = []
    for conv in _build_conversations(profile, own_department):
        result.append({
            'code': conv['code'], 'label': conv['label'],
            'messages': [
                {'dir': 'out' if m.sender_department == own_department else 'in',
                 't': m.body, 'time': _fmt_time(m.created_at)}
                for m in conv['messages']
            ],
        })
    return result


# ---------------------------------------------------------------------------
# Action endpoints: approvals (Station Master / Division Master)
# ---------------------------------------------------------------------------

@login_required(login_url='login')
def approve_request(request, request_id):
    profile = _get_profile(request.user)
    if request.method != 'POST' or not profile or profile.department not in ('STATION', 'DIVISION'):
        return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))

    item = get_object_or_404(AssetRequest, id=request_id)
    if profile.department == 'STATION':
        item.station_status = 'APPROVED'
        item.station_reason = ''
    else:
        item.division_status = 'APPROVED'
        item.division_reason = ''

    # An AI-suggested item has no worker yet until someone assigns one at
    # approval time; a worker-raised request already carries its own worker.
    worker_id = request.POST.get('worker_id')
    if not item.worker_id and worker_id:
        worker = User.objects.filter(id=worker_id, profile__department='WORKER').first()
        if worker:
            item.worker = worker

    item.save()
    messages.success(request, f"{item.request_code} approved.")
    return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))


@login_required(login_url='login')
def reject_request(request, request_id):
    profile = _get_profile(request.user)
    if request.method != 'POST' or not profile or profile.department not in ('STATION', 'DIVISION'):
        return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))

    item = get_object_or_404(AssetRequest, id=request_id)
    reason = request.POST.get('reason', '').strip() or 'No reason given.'

    if profile.department == 'STATION':
        item.station_status = 'REJECTED'
        item.station_reason = reason
        dept_label = 'Station Master'
    else:
        item.division_status = 'REJECTED'
        item.division_reason = reason
        dept_label = 'Division Master'
    item.work_status = 'REJECTED'
    item.save()

    # Notify the assigned worker with the rejection reason.
    Message.objects.create(
        sender=request.user,
        sender_department=profile.department,
        recipient_department='WORKER',
        recipient=item.worker,
        related_request=item,
        body=f"{item.request_code} ({item.work_description}) was not approved by {dept_label}. Reason: {reason}",
    )
    messages.success(request, f"{item.request_code} marked not approved.")
    return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))


@login_required(login_url='login')
def reschedule_request(request, request_id):
    profile = _get_profile(request.user)
    if request.method != 'POST' or not profile or profile.department not in ('STATION', 'DIVISION', 'CTRL'):
        return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))

    item = get_object_or_404(AssetRequest, id=request_id)
    start = parse_datetime(request.POST.get('start', ''))
    end = parse_datetime(request.POST.get('end', ''))
    reason = request.POST.get('reason', '').strip()
    if start and timezone.is_naive(start):
        start = timezone.make_aware(start)
    if end and timezone.is_naive(end):
        end = timezone.make_aware(end)
    if start:
        item.reschedule_start = start
        # A new start with no explicit end resets the old end, so the
        # request falls back to the default +2h window around the new
        # start instead of pairing a new start with a stale old end.
        item.reschedule_end = end
    elif end:
        item.reschedule_end = end
    item.reschedule_reason = reason
    item.work_status = 'RESCHEDULED'
    item.worker_acceptance = 'WAITING'  # worker needs to see & respond to the new slot
    item.save()

    # Let the assigned worker know on their message thread for this request,
    # with the new time and the reason for the change.
    if item.worker_id:
        new_start, _ = _schedule_window(item)
        dept_label = dict(UserProfile.DEPARTMENT_CHOICES).get(profile.department, profile.department)
        Message.objects.create(
            sender=request.user,
            sender_department=profile.department,
            recipient_department='WORKER',
            recipient=item.worker,
            related_request=item,
            body=(
                f"{item.request_code} ({item.work_description}) was rescheduled by {dept_label} "
                f"to {_fmt_dt_short(new_start)}. Reason: {reason or 'No reason given.'}"
            ),
        )

    messages.success(request, f"{item.request_code} rescheduled.")
    return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))


# ---------------------------------------------------------------------------
# Action endpoints: Worker responses
# ---------------------------------------------------------------------------

@login_required(login_url='login')
def worker_respond(request, request_id):
    profile = _get_profile(request.user)
    if request.method != 'POST' or not profile or profile.department != 'WORKER':
        return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))

    item = get_object_or_404(AssetRequest, id=request_id, worker=request.user)
    decision = request.POST.get('decision')

    if decision == 'accept':
        item.worker_acceptance = 'ACCEPTED'
        item.work_status = 'ACTIVE'
        item.worker_response_at = timezone.now()
        item.save()
        messages.success(request, f"{item.request_code} accepted.")
    elif decision == 'reject':
        reason = request.POST.get('reason', '').strip() or 'No reason given.'
        item.worker_acceptance = 'REJECTED'
        item.worker_reason = reason
        item.work_status = 'REJECTED'
        item.worker_response_at = timezone.now()
        item.save()
        for dept in ('STATION', 'DIVISION', 'CTRL'):
            Message.objects.create(
                sender=request.user,
                sender_department='WORKER',
                recipient_department=dept,
                related_request=item,
                body=f"{item.request_code} ({item.work_description}) — Not accepted. Reason: {reason}",
            )
        messages.success(request, f"{item.request_code} marked not accepted.")
    return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))


@login_required(login_url='login')
def worker_complete(request, request_id):
    profile = _get_profile(request.user)
    if request.method != 'POST' or not profile or profile.department != 'WORKER':
        return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))

    item = get_object_or_404(AssetRequest, id=request_id, worker=request.user)
    item.work_status = 'COMPLETED'
    item.save()
    messages.success(request, f"{item.request_code} marked completed.")
    return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))


@login_required(login_url='login')
def worker_create_request(request):
    profile = _get_profile(request.user)
    if request.method != 'POST' or not profile or profile.department != 'WORKER':
        return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))

    last = AssetRequest.objects.order_by('-id').first()
    next_num = (last.id if last else 2290) + 1

    work_date = parse_date(request.POST.get('work_date', '').strip()) if request.POST.get('work_date') else None
    start_time = parse_time(request.POST.get('start_time', '').strip()) if request.POST.get('start_time') else None
    end_time = parse_time(request.POST.get('end_time', '').strip()) if request.POST.get('end_time') else None

    # Total time is calculated automatically from the start and end time.
    total_time = ''
    if start_time and end_time:
        mins = (end_time.hour * 60 + end_time.minute) - (start_time.hour * 60 + start_time.minute)
        if mins > 0:
            total_time = ' '.join(p for p in (f"{mins // 60}h" if mins // 60 else '', f"{mins % 60}m" if mins % 60 else '') if p)

    AssetRequest.objects.create(
        request_code=f"WR-{next_num}",
        source='WORKER',
        section=request.POST.get('section', '').strip(),
        km=request.POST.get('km', '').strip(),
        track_line=request.POST.get('track_line', '').strip(),
        work_details=request.POST.get('work_details', '').strip(),
        work_description=request.POST.get('work_description', '').strip(),
        name=request.POST.get('name', '').strip(),
        phone_number=request.POST.get('phone_number', '').strip(),
        location=request.POST.get('location', '').strip(),
        division=request.POST.get('division', '').strip(),
        priority=request.POST.get('priority', '').strip() or 'Normal',
        from_station=request.POST.get('from_station', '').strip(),
        to_station=request.POST.get('to_station', '').strip(),
        from_junction=request.POST.get('from_junction', '').strip(),
        to_junction=request.POST.get('to_junction', '').strip(),
        work_date=work_date,
        start_time=start_time,
        end_time=end_time,
        total_time=total_time,
        created_by=request.user,
        worker=request.user,
    )
    messages.success(request, "Request sent for approval.")
    return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))


@login_required(login_url='login')
def worker_delete_request(request, request_id):
    """A worker can only withdraw a request they raised themselves, and only
    before either approver has acted on it — this is a self-service
    'cancel my own request', not a general delete."""
    profile = _get_profile(request.user)
    if request.method != 'POST' or not profile or profile.department != 'WORKER':
        return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))

    item = get_object_or_404(AssetRequest, id=request_id, created_by=request.user, source='WORKER')
    if item.station_status == 'WAITING' and item.division_status == 'WAITING':
        code = item.request_code
        item.delete()
        messages.success(request, f"{code} withdrawn.")
    else:
        messages.error(request, "This request already has a decision on it and can't be withdrawn.")
    return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))


# ---------------------------------------------------------------------------
# Messaging (all dashboards)
# ---------------------------------------------------------------------------

@login_required(login_url='login')
def send_message(request):
    profile = _get_profile(request.user)
    if request.method != 'POST' or not profile:
        return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))

    recipient_department = request.POST.get('department')
    body = request.POST.get('body', '').strip()
    valid_departments = dict(UserProfile.DEPARTMENT_CHOICES)
    if body and recipient_department in valid_departments:
        Message.objects.create(
            sender=request.user,
            sender_department=profile.department,
            recipient_department=recipient_department,
            body=body,
        )
    return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))


@login_required(login_url='login')
def send_request_message(request, request_id):
    """Station/Division messaging the specific worker on one work request
    (the per-request chat panel on their Communication tab)."""
    profile = _get_profile(request.user)
    if request.method != 'POST' or not profile or profile.department not in ('STATION', 'DIVISION', 'CTRL'):
        return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))

    item = get_object_or_404(AssetRequest, id=request_id)
    body = request.POST.get('body', '').strip()
    if body and item.worker_id:
        Message.objects.create(
            sender=request.user,
            sender_department=profile.department,
            recipient_department='WORKER',
            recipient=item.worker,
            related_request=item,
            body=body,
        )
    return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))