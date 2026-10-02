from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.utils import timezone
from django.utils.dateparse import parse_datetime, parse_date, parse_time
from django.db.models import Q, Max, Count
from django.urls import reverse
from datetime import timedelta, datetime as _datetime, time as _time

from .forms import RegisterForm, password_fingerprint
from .models import UserProfile, AssetRequest, Message, Train, TrainLiveStatus
from .railway_data import RAILWAY_DATA
from .ai_engine import (run_ai_scan, moved_parent_ids, available_slots, available_groups, check_reschedule_window,
                        asset_duration_minutes, _dur_label, _control_sender, window_clashes, BUFFER_MIN, APPROVAL_LEAD_MIN, IST)


# ---------------------------------------------------------------------------
# Display-label translation: the dashboards use their own wording for the
# same underlying status codes, so every payload builder below goes through
# these maps rather than showing raw DB codes.
# ---------------------------------------------------------------------------

STATUS_LABEL = {'WAITING': 'WAITING', 'APPROVED': 'APPROVED', 'REJECTED': 'NOT APPROVED'}
STATUS_KEY = {'WAITING': 'pending', 'APPROVED': 'approved', 'REJECTED': 'rejected'}
ACCEPT_LABEL = {'WAITING': 'WAITING', 'ACCEPTED': 'ACCEPTED', 'REJECTED': 'NOT ACCEPTED'}


def _time_changed_by_other(item):
    """Someone other than the worker moved the asset's time (a master, Control or the AI)."""
    return item.rescheduled_by in ('STATION', 'DIVISION', 'CTRL', 'AI')


def _is_own_upload(item):
    """The worker raised this asset themselves (directly, or it is the AI copy of their asset)."""
    if not item.worker_id:
        return False
    if item.source == 'WORKER':
        return item.worker_id == item.created_by_id
    if item.source == 'AI' and item.parent_id:
        return item.worker_id == item.parent.created_by_id
    return False


def _assigned_automatically(item):
    """An asset the worker uploaded, whose time nobody else has changed: the worker is simply
    ASSIGNED to it. No Accept / Not Accept is needed - that only comes when someone moves the time."""
    return _is_own_upload(item) and not _time_changed_by_other(item)


def _acceptance_label(item):
    """Worker status: ASSIGNED / WAITING (for the worker's Accept or Not Accept) / ACCEPTED / NOT ACCEPTED."""
    a = item.worker_acceptance
    if a == 'REJECTED':
        return 'NOT ACCEPTED'
    auto = _assigned_automatically(item)
    if a == 'ACCEPTED':
        return 'ASSIGNED' if (auto and item.worker_response_at is None) else 'ACCEPTED'
    return 'ASSIGNED' if auto else 'WAITING'
WORK_LABEL = {'OPEN': 'OPEN', 'ACTIVE': 'ACTIVE', 'COMPLETED': 'COMPLETED', 'RESCHEDULED': 'SCHEDULED', 'REJECTED': 'NOT APPROVED'}


def _status_label(item):
    """The one status word every dashboard shows for a request:
    NOT APPROVED / ACTIVE / COMPLETED come from the time-based life cycle below,
    APPROVED = both masters approved it and its start time has not come yet."""
    if item.work_status in ('ACTIVE', 'COMPLETED', 'REJECTED'):
        return WORK_LABEL[item.work_status]
    if item.both_approved:
        return 'APPROVED'
    return WORK_LABEL.get(item.work_status, item.work_status)


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
    start, end = _base_window(item)
    if item.extended_end and item.extended_end > end:
        end = item.extended_end            # the Control Office added extra time to this running asset
    return start, end


def _base_window(item):
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


# ---------------------------------------------------------------------------
# Time-based life cycle of an asset
#
#   uploaded -> Station Master + Division Master approve it
#   start time comes:
#       both approved (and the worker accepted / it is the worker's own asset) -> ACTIVE
#       not approved by both                                                   -> NOT APPROVED
#   end time comes:
#       the worker is asked 'complete or not' (Complete -> COMPLETED; Not complete -> asks the
#       Control Office for more time, then is asked again at the new end time)
#
# sync_lifecycle() runs every time a dashboard loads (and every 30 seconds from the
# open dashboards), so the status is always right for the current clock time.
# ---------------------------------------------------------------------------

def _advance_item(item, now=None):
    """Move ONE request along its life cycle for the current time. True if it changed."""
    now = now or timezone.now()
    if item.work_status in ('COMPLETED', 'REJECTED'):
        return False
    start, end = _schedule_window(item)

    # The worker's own asset whose time nobody changed: they are simply ASSIGNED to it, so once both
    # masters approved there is nothing for the worker to accept. (If a master / the AI moved the
    # time, the worker must press Accept or Not Accept first.)
    changed = False
    if item.both_approved and item.worker_acceptance != 'ACCEPTED' and _assigned_automatically(item):
        item.worker_acceptance = 'ACCEPTED'
        item.save()
        changed = True

    if now < start:
        return changed                    # the start time has not come yet: nothing more to decide

    if not item.both_approved:
        # The start time is here and the Station Master + Division Master did not both approve.
        if item.station_status == 'WAITING':
            item.station_status, item.station_reason = 'REJECTED', 'No decision before the start time.'
        if item.division_status == 'WAITING':
            item.division_status, item.division_reason = 'REJECTED', 'No decision before the start time.'
        item.work_status = 'REJECTED'
        item.save()
        target = item.worker or item.created_by
        if target:
            Message.objects.create(
                sender=_control_sender(target), sender_department='CTRL', recipient_department='WORKER',
                recipient=target, related_request=item,
                body=(f"{item.request_code} ({item.work_description}) is NOT APPROVED: the Station Master and "
                      f"Division Master did not both approve it before its start time ({_fmt_dt_short(start)})."),
            )
        return True

    if item.worker_acceptance != 'ACCEPTED':
        return changed                    # approved, but the worker has not accepted it yet
    # Never completed by the clock: after the end time the worker is asked 'complete or not'.
    target_status = 'ACTIVE'
    if item.work_status != target_status:
        item.work_status = target_status
        changed = True
    if changed:
        item.save()
    return changed


def sync_lifecycle(now=None):
    """Apply the life cycle to every live request. Returns how many changed."""
    now = now or timezone.now()
    changed = _run_auto_approvals(now)      # 1:25 - nobody decided: the AI approves, on every page
    live = AssetRequest.objects.exclude(work_status__in=['COMPLETED', 'REJECTED'])
    for item in live.filter(source='AI'):
        changed += _advance_item(item, now)
    # The worker's original entry of an asset the AI took over stays hidden (and is left alone)
    # while its AI copy is alive; it only comes back - and is checked - if the copy was dropped.
    hidden = moved_parent_ids()
    for item in live.exclude(source='AI').exclude(id__in=hidden):
        changed += _advance_item(item, now)
    return changed


def _lifecycle_sig():
    """Changes whenever any request changes; the open dashboards poll it and refresh themselves."""
    agg = AssetRequest.objects.aggregate(n=Count('id'), last=Max('updated_at'))
    return f"{agg['n']}:{agg['last'].timestamp() if agg['last'] else 0}"


@login_required(login_url='login')
def lifecycle_status(request):
    """Polled by every dashboard: runs the life cycle and returns the change signature."""
    sync_lifecycle()
    return JsonResponse({'sig': _lifecycle_sig()})


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


def _worker_msg_fields(item):
    """The worker's 'not accepted' message (and the new time they asked for while it is
    waiting for re-approval). Station / Division dashboards write it under the worker."""
    asked_new_time = item.worker_acceptance == 'REJECTED' and item.work_status == 'RESCHEDULED'
    wnew = ''
    if asked_new_time and item.reschedule_start:
        s, e = _schedule_window(item)
        wnew = f"{_fmt_date(s)} {_fmt_time(s)}-{_fmt_time(e)}"
    return {'wr': item.worker_reason or '', 'wnew': wnew}


RESCH_BY_LABEL = {'STATION': 'Station Master', 'DIVISION': 'Division Master', 'CTRL': 'Control Office',
                  'WORKER': 'Worker', 'AI': 'AI'}


def _resched_fields(item):
    """Who proposed the current time, and the message they wrote with it."""
    by = item.rescheduled_by or ''
    return {'rby': by, 'rbyLabel': RESCH_BY_LABEL.get(by, ''),
            'rmsg': item.reschedule_reason if by in ('STATION', 'DIVISION', 'CTRL') else ''}


def _serialize_ai(item, sec_index, own_field=None):
    """One AI Suggested Work card. `own_field` is 'station_status' or
    'division_status' - whichever this dashboard approves - so the card can show
    Approve / Not Approved buttons for it; the other one is shown read-only."""
    start, end = _schedule_window(item)
    sm, dm = item.station_status, item.division_status
    own = getattr(item, own_field) if own_field else None
    peer = (dm if own_field == 'station_status' else sm) if own_field else None
    profile = getattr(item.worker, 'profile', None) if item.worker else None
    return {
        'reqId': item.id, 'id': item.request_code, 'src': item.source,
        't': item.work_description, 'p': (item.priority or 'Normal').upper(),
        's': sec_index.get(item.section or 'Unassigned', 0),
        'km': item.km, 'as': f"{item.asset_type} {item.asset_code}".strip(),
        'dt': _fmt_dt_short(item.detected_at),
        'dISO': timezone.localtime(item.detected_at).date().isoformat(),
        'date': _fmt_date(start), 'stm': _fmt_time(start), 'etm': _fmt_time(end),
        'dur': _fmt_duration(start, end),
        'd': item.ai_note or item.work_details or item.work_description,
        'worker': item.worker.username if item.worker else None,
        'wdept': profile.work_department if profile else '',
        'div': item.division or '', 'loc': item.location or '', 'ln': item.track_line or '',
        'fs': item.from_station, 'ts': item.to_station, 'fj': item.from_junction, 'tj': item.to_junction,
        'trains': item.trains_checked, 'workerId': item.worker_id,
        'sm': STATUS_LABEL[sm], 'dm': STATUS_LABEL[dm],
        'own': STATUS_LABEL[own] if own else None, 'peer': STATUS_LABEL[peer] if peer else None,
        'reason': (item.station_reason if own_field == 'station_status' else item.division_reason) if own_field else '',
        'ac': _acceptance_label(item),
        'st': _status_label(item),
        'cap': item.control_approved,
        **_worker_msg_fields(item),
        **_resched_fields(item),
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
        'st': _status_label(item),
        'dv': STATUS_LABEL[peer_status],
        'ct': STATUS_LABEL[own_status],
        'wk': item.worker.username if item.worker else None,
        'rl': item.worker_role or '—',
        'ac': _acceptance_label(item),
        'rp': _fmt_dt_short(item.worker_response_at) if item.worker_response_at else '—',
        'online': item.work_status == 'ACTIVE',
        'decision': decision, 'reason': own_reason,
        'resch': item.work_status == 'RESCHEDULED',
        'cap': item.control_approved,
        'aap': item.ai_approved,
        # Shown in the "Request Detail" panel and in the Not Approved pop-up.
        'div': item.division or '',
        'dur': _fmt_duration(start, end),
        **_worker_msg_fields(item),
        **_resched_fields(item),
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


def _user_for_password(raw_password):
    """The ONE account that was registered with this password (registration refuses a password
    another account already uses, so a password belongs to exactly one account)."""
    fp = password_fingerprint(raw_password)
    profile = UserProfile.objects.filter(password_fingerprint=fp).select_related('user').first()
    if profile and profile.user.is_active and profile.user.check_password(raw_password):
        return profile.user
    # Accounts created before fingerprints existed: find them by their hash, then remember the fingerprint.
    for u in User.objects.filter(profile__password_fingerprint='', is_active=True):
        if u.check_password(raw_password):
            UserProfile.objects.filter(user=u).update(password_fingerprint=fp)
            return u
    return None


def login(request):
    """Every sign-in opens the SAME stored account - nothing is created here.
    - password only: opens the account registered with that password;
    - username + password: opens that username's account if the password is right.
    A different password belongs to a different (separately registered) account."""
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '').strip()

        if not password:
            messages.error(request, "Please enter your password.")
            return render(request, 'login.html')

        if username:
            account = User.objects.filter(username__iexact=username).first()
            if account is None:
                messages.error(request, "This username is not registered.")
                return render(request, 'login.html')
            if not account.is_active or not account.check_password(password):
                messages.error(request, "Incorrect password.")
                return render(request, 'login.html')
            user = account
        else:
            user = _user_for_password(password)
            if user is None:
                messages.error(request, "No account uses this password. Register first, or check the password.")
                return render(request, 'login.html')

        auth_login(request, user, backend='django.contrib.auth.backends.ModelBackend')
        return _redirect_to_dashboard(user)

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

    sync_lifecycle()  # start / end time reached? -> Active / Completed / Not Approved
    run_ai_scan()  # look for train-free windows for idle workers' assets
    today = timezone.localdate()
    all_requests = AssetRequest.objects.exclude(id__in=moved_parent_ids())
    sections, sec_index = _sections_and_index(all_requests)
    page_data = {
        'sig': _lifecycle_sig(),
        'S': sections,
        'A': [_serialize_ai(r, sec_index, 'station_status') for r in all_requests.filter(source='AI').select_related('worker__profile').order_by('-detected_at')],
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

    sync_lifecycle()  # start / end time reached? -> Active / Completed / Not Approved
    run_ai_scan()  # look for train-free windows for idle workers' assets
    today = timezone.localdate()
    all_requests = AssetRequest.objects.exclude(id__in=moved_parent_ids())
    sections, sec_index = _sections_and_index(all_requests)
    page_data = {
        'sig': _lifecycle_sig(),
        'S': sections,
        'A': [_serialize_ai(r, sec_index, 'division_status') for r in all_requests.filter(source='AI').select_related('worker__profile').order_by('-detected_at')],
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

    sync_lifecycle()
    all_requests = AssetRequest.objects.exclude(id__in=moved_parent_ids())
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
            'reqStatus': _status_label(r),
            'cap': r.control_approved,
            'aap': r.ai_approved,
            'approval': {'sm': STATUS_KEY[r.station_status], 'div': STATUS_KEY[r.division_status]},
            'worker': {
                'a': True if r.worker_acceptance == 'ACCEPTED' else (False if r.worker_acceptance == 'REJECTED' else None),
                'label': _acceptance_label(r),
                't': _fmt_time(r.worker_response_at) if r.worker_response_at else '—',
                'name': r.worker.username if r.worker else None,
                'role': r.worker_role or '—',
                'online': r.work_status == 'ACTIVE',
            },
            'reschedule': {'done': r.work_status == 'RESCHEDULED', 'newDT': _fmt_iso(r.reschedule_start), 'reason': r.reschedule_reason or ''},
        }

    page_data = {
        'sig': _lifecycle_sig(),
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
    sync_lifecycle()  # start / end time reached? -> Active / Completed / Not Approved
    my_requests = (AssetRequest.objects.filter(Q(worker=user) | Q(created_by=user))
                   .exclude(id__in=moved_parent_ids()).distinct())

    approved = my_requests.filter(station_status='APPROVED', division_status='APPROVED')
    waiting = my_requests.filter(Q(station_status='WAITING') | Q(division_status='WAITING'))
    accepted_all_three = approved.filter(worker_acceptance='ACCEPTED')
    active = my_requests.filter(work_status='ACTIVE')
    completed = my_requests.filter(work_status='COMPLETED')
    rescheduled = my_requests.filter(work_status='RESCHEDULED')

    # Needs the worker's response: fully approved by both departments,
    # assigned to this worker, and this worker hasn't responded yet
    # (this also covers items a station/division master just rescheduled).
    needs_response = my_requests.filter(worker=user, worker_acceptance='WAITING').filter(
        Q(station_status='APPROVED', division_status='APPROVED') | Q(rescheduled_by__in=['STATION', 'DIVISION', 'CTRL'])
    )

    def _original_window(r):
        """The time the worker first asked for (before the AI or a master moved it)."""
        src = r.parent if (r.source == 'AI' and r.parent_id) else r
        if not src.work_date:
            return None, None
        tz = timezone.get_current_timezone()
        st = timezone.make_aware(_datetime.combine(src.work_date, src.start_time or _time(0, 0)), tz)
        en = timezone.make_aware(_datetime.combine(src.work_date, src.end_time), tz) if src.end_time else st + timedelta(hours=2)
        if en <= st:
            en += timedelta(days=1)
        return st, en

    def _worker_row(r):
        start, end = _schedule_window(r)
        by = r.rescheduled_by
        masters_moved = by in ('STATION', 'DIVISION', 'CTRL')
        if r.work_status == 'COMPLETED':
            stage = 'completed'
        elif r.work_status == 'REJECTED' or r.any_rejected:
            stage = 'not-approved'              # a master did not approve, or nobody approved before the start time
        elif r.worker_acceptance == 'ACCEPTED':
            if not r.both_approved:
                stage = 'accepted-waiting'
            elif r.work_status == 'ACTIVE':
                stage = 'active'                # start time has come
            else:
                stage = 'approved'              # approved, waiting for the start time
        elif r.worker_acceptance == 'REJECTED':
            # Worker pressed Not Accept and sent a new time: it waits for both masters again.
            stage = 'resubmitted' if (by == 'WORKER' and r.work_status == 'RESCHEDULED') else 'declined'
        elif r.both_approved or masters_moved:
            stage = 'pending-response'          # waiting for THIS worker: Accept / Not Accept
        else:
            stage = 'pending-approval'          # waiting for the Station / Division Master
        o_start, o_end = _original_window(r)
        rescheduled = bool(r.reschedule_start or by)
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
            'workerLabel': _acceptance_label(r),
            'reschedule': {'rescheduled': rescheduled, 'newDateTime': _fmt_iso(r.reschedule_start or start)},
            # Finished on the end time, or earlier if the worker pressed Mark Completed.
            'completedAt': _fmt_iso(min(r.updated_at, end) if end else r.updated_at) if r.work_status == 'COMPLETED' else None,
            'ownedByMe': r.created_by_id == user.id,
            'source': r.source, 'aiNote': r.ai_note, 'trains': r.trains_checked,
            'asset': r.work_details or r.asset_type or '—',
            'place': r.location or '—',
            # The CURRENT time (already includes any reschedule) - what every table shows.
            'dateLabel': _fmt_date(start), 'dateFull': timezone.localtime(start).strftime('%d %b %Y') if start else '',
            'startLabel': _fmt_time(start), 'endLabel': _fmt_time(end),
            'duration': _fmt_duration(start, end),
            'origLabel': (f"{timezone.localtime(o_start):%d %b %Y} {_fmt_time(o_start)}-{_fmt_time(o_end)}" if o_start else '—'),
            'workerReason': r.worker_reason,
            'ctrlApproved': r.control_approved,
            'aiApproved': r.ai_approved,
            'rby': by, 'rbyLabel': RESCH_BY_LABEL.get(by, ''),
            'rmsg': (r.reschedule_reason if masters_moved else ''),
            'durMin': asset_duration_minutes(r),
            'proposedEnd': _fmt_iso(r.reschedule_end),
        }

    page_data = {
        'sig': _lifecycle_sig(),
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
        )
        if own_department == 'WORKER':
            # Other workers' private messages (e.g. the AI reschedule reason) must not show here.
            thread = thread.filter(
                Q(sender=profile.user) | Q(recipient=profile.user) | Q(recipient__isnull=True, sender_department__in=[
                    d for d, _ in UserProfile.DEPARTMENT_CHOICES if d != 'WORKER'])
            )
        thread = thread.order_by('created_at')
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

    # Who becomes ACTIVE, and when:
    #  - a worker's own asset nobody moved: assigned - ready as soon as BOTH masters approved it;
    #  - a master (or the AI) moved the time: the worker must Accept / Not Accept first;
    #  - the worker pressed Not Accept and sent a new time: when BOTH masters approve that time;
    #  - a master moved the time and the worker already accepted it: when the OTHER master
    #    approves too.
    _finish_approval(request, item)
    run_ai_scan()
    return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))

# ---------------------------------------------------------------------------
# Approval alerts, step by step. Example: work 3:00-4:00, buffer 2:45-3:00 and 4:00-4:15
#   buffer starts 1:55 (work 2:10-3:10, buffer 3:10-3:25 after)
#   0:55 - 1:25  Station Master / Division Master dashboards: "approve or not approve" alert (60 -> 30 min before the buffer)
#   1:10 - 1:25  Control Office dashboard: Approve, or Not Approved + new time + message to the worker
#   1:25         Nobody decided: the AI checks the buffer, start and end time for trains. No train ->
#                approved on every page and every alert stops. A train -> stays open for the Control Office.
# Plus: if a train suddenly comes while a worker is working (buffer / start..end), the Control
# Office gets a "train is coming" alert with the asset's details and can block that train.
# ---------------------------------------------------------------------------

MASTER_ALERT_LEAD_MIN = 60   # minutes before the buffer starts: masters are alerted   (buffer 1:55 -> alert 0:55)
CTRL_ALERT_LEAD_MIN = 45     # ... Control Office is alerted                           (1:55 - 45 = 1:10)
AUTO_LEAD_MIN = 30           # ... the AI checks the trains, approves, alerts stop     (1:55 - 30 = 1:25)
TRAIN_WATCH_LEAD_MIN = 30    # watch for trains this long before the buffer starts


def _hm(d):
    return timezone.localtime(d).strftime('%H:%M')


def _asset_details(item):
    """Every detail of the asset, for the alert / message text."""
    worker = item.worker or item.created_by
    wprof = _get_profile(worker) if worker else None
    bits = [f"Asset {item.request_code}"]
    if item.asset_type or item.asset_code:
        bits.append(f"{item.asset_type} {item.asset_code}".strip())
    if item.work_description:
        bits.append(f"work: {item.work_description}")
    if item.section:
        bits.append(f"section {item.section}" + (f" km {item.km}" if item.km else ''))
    st = ' to '.join(x for x in (item.from_station, item.to_station) if x)
    if st:
        bits.append(f"stations {st}")
    jn = ' to '.join(x for x in (item.from_junction, item.to_junction) if x)
    if jn:
        bits.append(f"junctions {jn}")
    if item.location:
        bits.append(f"location {item.location}")
    if worker:
        bits.append(f"worker {worker.username}" + (f" ({wprof.work_department})" if wprof and wprof.work_department else ''))
    return ', '.join(bits)


def _alert_times(item):
    start, end = _schedule_window(item)
    if not start:
        return None
    buf = timedelta(minutes=BUFFER_MIN)
    bs = start - buf
    return {'start': start, 'end': end, 'bufferStart': bs, 'bufferEnd': end + buf,
            'masterAt': bs - timedelta(minutes=MASTER_ALERT_LEAD_MIN),
            'ctrlAt': bs - timedelta(minutes=CTRL_ALERT_LEAD_MIN),
            'autoAt': bs - timedelta(minutes=AUTO_LEAD_MIN)}


def _approval_alert(item, now=None):
    """The approval alert for one request, or None. 'ctrlActive' becomes True at the Control Office
    time (2:15) - only then may the Control Office act; 'autoDue' at 2:30 (AI check)."""
    now = now or timezone.now()
    if item.work_status in ('ACTIVE', 'COMPLETED', 'REJECTED'):
        return None
    if 'REJECTED' in (item.station_status, item.division_status):
        return None
    missing = []
    if item.station_status == 'WAITING':
        missing.append('STATION')
    if item.division_status == 'WAITING':
        missing.append('DIVISION')
    if not missing:
        return None
    t = _alert_times(item)
    if not t or now < t['masterAt'] or now >= t['start']:
        return None
    who = {'STATION': 'Station Master', 'DIVISION': 'Division Master'}
    names = ' and '.join(who[m] for m in missing)
    return {
        'kind': 'approval', 'reqId': item.id, 'code': item.request_code, 'work': item.work_description,
        'source': item.source, 'missing': missing,
        'date': _fmt_date(t['start']), 'start': _hm(t['start']), 'end': _hm(t['end']),
        'bufferStart': _hm(t['bufferStart']), 'bufferEnd': _hm(t['bufferEnd']),
        'alertAt': _hm(t['masterAt']), 'ctrlAt': _hm(t['ctrlAt']), 'autoAt': _hm(t['autoAt']),
        'masterActive': t['masterAt'] <= now < t['autoAt'],
        'ctrlActive': now >= t['ctrlAt'], 'autoDue': now >= t['autoAt'],
        'minutesLeft': max(int((t['start'] - now).total_seconds() // 60), 0),
        'details': _asset_details(item), 'trainClash': '',
        'text': (f"{item.request_code} ({item.work_description}) works {_fmt_date(t['start'])} "
                 f"{_hm(t['start'])}\u2013{_hm(t['end'])} (buffer {_hm(t['bufferStart'])}\u2013{_hm(t['bufferEnd'])}). "
                 f"{names} {'has' if len(missing) == 1 else 'have'} not decided yet (Approve or Not Approved)."),
    }


def _tell(item, depts, body, sender=None, worker_too=True):
    """Message from the Control Office to the given departments (and the asset's worker)."""
    target = item.worker or item.created_by
    snd = sender or (_control_sender(target) if target else None)
    if snd is None:
        return
    for dept in depts:
        Message.objects.create(sender=snd, sender_department='CTRL', recipient_department=dept,
                               related_request=item, body=body)
    if worker_too and target:
        Message.objects.create(sender=snd, sender_department='CTRL', recipient_department='WORKER',
                               recipient=target, related_request=item, body=body)


def _ai_auto_approve(item, alert, now):
    """2:30 and nobody decided: if no train is on the line during buffer + work, approve everywhere.
    Returns True when approved. Otherwise puts the train into the alert text."""
    t = _alert_times(item)
    _found, clashes = window_clashes(item, t['start'], t['end'], now)
    no_data = clashes is None            # the asset's stations are on no timetable: no train can be detected
    if clashes:
        alert['trainClash'] = ("AI check: NOT auto-approved - " + '; '.join(c['text'] for c in clashes[:3]) +
                               ". Control Office must decide.")
        return False
    waiting = [n for n, st in (('Station Master', item.station_status), ('Division Master', item.division_status)) if st == 'WAITING']
    if item.station_status == 'WAITING':
        item.station_status, item.station_reason = 'APPROVED', ''
    if item.division_status == 'WAITING':
        item.division_status, item.division_reason = 'APPROVED', ''
    item.ai_approved = True
    item.save()
    body = (f"{item.request_code} ({item.work_description}) was APPROVED automatically by the AI: the "
            f"{' and '.join(waiting)} and the Control Office did not decide, and no train is detected between "
            f"{alert['bufferStart']} and {alert['bufferEnd']} (buffer, start and end time) on this stretch"
            f"{' (the stations are on no train timetable, so no train data exists)' if no_data else ''}. "
            f"Work {alert['start']}\u2013{alert['end']}.")
    _tell(item, ('STATION', 'DIVISION'), body)
    _finish_approval(None, item)
    return True


def _run_auto_approvals(now=None):
    """At the AI check time (30 min before the buffer: 1:25 for a 1:55 buffer) every asset that the masters have not
    decided is approved here - for ALL assets, whichever dashboard is open. Returns how many were approved."""
    now = now or timezone.now()
    done = 0
    pending = (AssetRequest.objects.exclude(work_status__in=['ACTIVE', 'COMPLETED', 'REJECTED'])
               .exclude(Q(station_status='APPROVED') & Q(division_status='APPROVED'))
               .exclude(id__in=moved_parent_ids()))
    for item in pending:
        a = _approval_alert(item, now)
        if a and a['autoDue'] and _ai_auto_approve(item, a, now):
            done += 1
    return done


def _raise_approval_alerts(now=None):
    """Every due approval alert, plus: one automatic message (once per asset) to the master(s) who have
    not decided, and the 2:30 AI auto-approval."""
    now = now or timezone.now()
    hidden = moved_parent_ids()
    alerts = []
    for item in AssetRequest.objects.exclude(work_status__in=['ACTIVE', 'COMPLETED', 'REJECTED']).exclude(id__in=hidden):
        a = _approval_alert(item, now)
        if not a:
            continue
        if a['autoDue'] and _ai_auto_approve(item, a, now):
            continue
        alerts.append(a)
        target = item.worker or item.created_by
        if not target:          # Message.sender needs a real user
            continue
        for dept in a['missing']:
            if Message.objects.filter(related_request=item, recipient_department=dept, body__startswith='ALERT:').exists():
                continue
            Message.objects.create(
                sender=_control_sender(target), sender_department='CTRL', recipient_department=dept,
                related_request=item,
                body=(f"ALERT: {a['text']} Please approve or not approve it before {a['autoAt']}. If nobody decides, the "
                      f"Control Office is alerted at {a['ctrlAt']} and at {a['autoAt']} the AI checks the trains and "
                      f"approves it if the line is clear."),
            )
    return alerts


def _train_alerts(now=None):
    """Control Office: a train is (suddenly) coming onto a stretch where an approved / active asset's
    worker is working - during its buffer, start..end or end buffer."""
    now = now or timezone.now()
    out = []
    qs = AssetRequest.objects.exclude(work_status__in=['COMPLETED', 'REJECTED']).exclude(id__in=moved_parent_ids())
    for item in qs:
        if item.work_status != 'ACTIVE' and not item.both_approved:
            continue
        t = _alert_times(item)
        if not t or now < t['bufferStart'] - timedelta(minutes=TRAIN_WATCH_LEAD_MIN) or now > t['bufferEnd']:
            continue
        _found, clashes = window_clashes(item, t['start'], t['end'], now)
        for c in clashes or []:
            worker = item.worker or item.created_by
            out.append({
                'kind': 'train', 'reqId': item.id, 'code': item.request_code, 'work': item.work_description,
                'trainNumber': c['number'], 'trainName': c['name'], 'enter': c['enter'], 'leave': c['leave'],
                'places': c['places'], 'worker': worker.username if worker else '',
                'start': _hm(t['start']), 'end': _hm(t['end']),
                'bufferStart': _hm(t['bufferStart']), 'bufferEnd': _hm(t['bufferEnd']),
                'working': item.work_status == 'ACTIVE', 'details': _asset_details(item),
                'text': (f"TRAIN COMING: train {c['number']} {c['name']} is on {c['places']} between {c['enter']} and "
                         f"{c['leave']}{(' (' + c['note'] + ')') if c['note'] else ''}, but "
                         f"{'the worker is working there now' if item.work_status == 'ACTIVE' else 'a worker is booked to work there'} "
                         f"({_hm(t['start'])}\u2013{_hm(t['end'])}, buffer {_hm(t['bufferStart'])}\u2013{_hm(t['bufferEnd'])}). "
                         f"Block that train."),
            })
    return out


@login_required(login_url='login')
def approval_alerts(request):
    """Polled by the Station / Division / Control dashboards (every 30 s) to show the alert banners."""
    profile = _get_profile(request.user)
    if not profile or profile.department not in ('STATION', 'DIVISION', 'CTRL'):
        return JsonResponse({'alerts': []})
    sync_lifecycle()
    alerts = _raise_approval_alerts()
    if profile.department in ('STATION', 'DIVISION'):
        # masters see it from 60 to 30 minutes before the buffer; at the AI check time it stops
        alerts = [a for a in alerts if profile.department in a['missing'] and a['masterActive']]
        return JsonResponse({'alerts': alerts, 'role': profile.department})
    # Control Office: approval alerts from 45 to 30 minutes before the buffer (and after that only when the
    # AI could not approve because a train is detected), plus train alerts and "more time" requests.
    alerts = [a for a in alerts if a['ctrlActive'] and (not a['autoDue'] or a['trainClash'])]
    return JsonResponse({'alerts': alerts + _train_alerts() + _extension_alerts(), 'role': 'CTRL'})


@login_required(login_url='login')
def block_train(request, request_id, train_number):
    """Control Office blocks (holds) a train that is coming onto the stretch where the asset's worker works."""
    profile = _get_profile(request.user)
    if request.method != 'POST' or not profile or profile.department != 'CTRL':
        return JsonResponse({'ok': False, 'error': 'Only the Control Office can block a train.'}, status=403)
    item = get_object_or_404(AssetRequest, id=request_id)
    train = get_object_or_404(Train, number=train_number)
    t = _alert_times(item)
    if not t:
        return JsonResponse({'ok': False, 'error': 'This asset has no work time.'}, status=400)
    until = max(t['bufferEnd'], timezone.now() + timedelta(minutes=5))
    live, _c = TrainLiveStatus.objects.get_or_create(train=train)
    live.held_until = until
    live.held_reason = f"Held by Control Office: {item.request_code} work {_hm(t['start'])}-{_hm(t['end'])}"[:255]
    live.save()
    body = (f"Train {train.number} {train.name} is BLOCKED (held) until {_hm(until)} by the Control Office because "
            f"a worker is working on its line. {_asset_details(item)}. Work {_hm(t['start'])}\u2013{_hm(t['end'])}, "
            f"buffer {_hm(t['bufferStart'])}\u2013{_hm(t['bufferEnd'])}.")
    _tell(item, ('STATION', 'DIVISION'), body, sender=request.user)
    return JsonResponse({'ok': True, 'message': f"Train {train.number} blocked until {_hm(until)}."})


def _finish_approval(request, item):
    """What happens after an approval: the worker is assigned / waits for Accept / becomes active.
    request may be None (the AI approved it on its own) - then no flash message is shown."""
    def say(text):
        if request is not None:
            messages.success(request, text)

    if item.both_approved and item.worker_acceptance == 'WAITING' and _assigned_automatically(item):
        _accept_and_activate(item, auto=True)
        say(f"{item.request_code} approved - {item.worker.username}: {_start_text(item)}")
    elif (item.both_approved and item.worker_id and item.worker_acceptance == 'REJECTED'
            and item.work_status == 'RESCHEDULED'):
        _accept_and_activate(item)
        say(f"{item.request_code} new time approved - {item.worker.username}: {_start_text(item)}")
    elif item.both_approved and item.worker_id and item.worker_acceptance == 'ACCEPTED' and item.work_status != 'ACTIVE':
        _accept_and_activate(item)
        say(f"{item.request_code} approved - {item.worker.username}: {_start_text(item)}")
    else:
        say(f"{item.request_code} approved.")


@login_required(login_url='login')
def control_approve_request(request, request_id):
    """The Control Office approves an asset the masters did not decide in time (after the alert is raised)."""
    profile = _get_profile(request.user)
    ajax = request.POST.get('ajax') == '1'

    def reply(ok, text, status=None):
        if ajax:
            return JsonResponse({'ok': ok, 'error': '' if ok else text, 'message': text if ok else ''},
                                status=status or (200 if ok else 400))
        (messages.success if ok else messages.error)(request, text)
        return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))

    if request.method != 'POST' or not profile or profile.department != 'CTRL':
        return reply(False, "Only the Control Office can do this.", 403)
    item = get_object_or_404(AssetRequest, id=request_id)
    alert = _approval_alert(item)
    if not alert or not alert['ctrlActive']:
        return reply(False, f"{item.request_code}: there is no Control Office alert for this asset yet (it is raised "
                            f"at the Control Office time, 30 minutes before the buffer), or it is already decided, "
                            f"or its start time has passed.")

    waiting = []
    if item.station_status == 'WAITING':
        item.station_status, item.station_reason = 'APPROVED', ''
        waiting.append('Station Master')
    if item.division_status == 'WAITING':
        item.division_status, item.division_reason = 'APPROVED', ''
        waiting.append('Division Master')
    item.control_approved = True
    item.save()

    # Tell the masters and the worker, then carry on exactly like a normal approval.
    target = item.worker or item.created_by
    note = (f"{item.request_code} ({item.work_description}) was APPROVED by the Control Office because the "
            f"{' and '.join(waiting)} did not decide before the alert time.")
    for dept in ('STATION', 'DIVISION'):
        Message.objects.create(sender=request.user, sender_department='CTRL', recipient_department=dept,
                               related_request=item, body=note)
    if target:
        Message.objects.create(sender=request.user, sender_department='CTRL', recipient_department='WORKER',
                               recipient=target, related_request=item, body=note)
    _finish_approval(request, item)
    run_ai_scan()
    return reply(True, f"{item.request_code} approved by the Control Office.")


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
    run_ai_scan()

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


def _reschedule_reply(request, ok, text, ajax):
    """JSON for the in-page forms; normal redirect + message for plain form posts."""
    if ajax:
        return JsonResponse({'ok': ok, 'error': '' if ok else text, 'message': text if ok else ''}, status=200 if ok else 400)
    (messages.success if ok else messages.error)(request, text)
    return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))


@login_required(login_url='login')
def reschedule_request(request, request_id):
    profile = _get_profile(request.user)
    if request.method != 'POST' or not profile or profile.department not in ('STATION', 'DIVISION', 'CTRL'):
        return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))

    ajax = request.POST.get('ajax') == '1'
    item = get_object_or_404(AssetRequest, id=request_id)
    start = parse_datetime(request.POST.get('start', ''))
    end = parse_datetime(request.POST.get('end', ''))
    reason = request.POST.get('reason', '').strip()
    if start and timezone.is_naive(start):
        start = timezone.make_aware(start)
    if end and timezone.is_naive(end):
        end = timezone.make_aware(end)
    if not start:
        return _reschedule_reply(request, False, "Please choose the new date and time.", ajax)
    # No end given: use the duration the worker asked for on the asset.
    if not end:
        end = start + timedelta(minutes=asset_duration_minutes(item))

    # Check the date/time first: no past dates, no time when a train is on the asset's line.
    problem = check_reschedule_window(item, start, end)
    if problem:
        return _reschedule_reply(request, False, problem, ajax)

    # Control Office pressed 'Not Approved' on the approval alert: the new time it picked is its decision,
    # so the masters who had not decided are marked approved for the new time (shown as Control Office).
    from_alert = request.POST.get('alert') == '1' and profile.department == 'CTRL'
    if from_alert:
        alert = _approval_alert(item)
        if not alert or not alert['ctrlActive']:
            return _reschedule_reply(request, False, f"{item.request_code}: there is no Control Office alert for this asset.", ajax)
        if not reason:
            return _reschedule_reply(request, False, "Please write the message for the worker.", ajax)
        if item.station_status == 'WAITING':
            item.station_status, item.station_reason = 'APPROVED', ''
        if item.division_status == 'WAITING':
            item.division_status, item.division_reason = 'APPROVED', ''
        item.control_approved = True
    item.reschedule_start = start
    item.reschedule_end = end
    item.reschedule_reason = reason
    item.rescheduled_by = profile.department
    item.worker_requested_reschedule = False
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

    return _reschedule_reply(request, True, f"{item.request_code} rescheduled.", ajax)


@login_required(login_url='login')
def request_slots(request, request_id):
    """'Reschedule time' button: train-free windows exactly as long as the asset's work duration.
    Starts with the asset's own day (or the day picked). No free window that day -> the next
    days; a far-away day -> today's free time as well."""
    profile = _get_profile(request.user)
    if not profile or profile.department not in ('STATION', 'DIVISION', 'CTRL'):
        return JsonResponse({'ok': False, 'error': 'Not allowed.'}, status=403)
    item = get_object_or_404(AssetRequest, id=request_id)
    day = parse_date(request.GET.get('date', '').strip()) if request.GET.get('date') else None
    if day is None:
        start, _end = _schedule_window(item)
        day = timezone.localtime(start).date() if start else None
    return JsonResponse(available_groups(item, day=day, include_today=True))


@login_required(login_url='login')
def ai_not_approve(request, request_id):
    """Station / Division Master presses Not Approve on an AI suggestion: they write a message
    and pick a new train-free time. The asset's time changes everywhere and the worker gets it
    in 'Needs Your Response' (Accept / Not Accept), showing who rescheduled it."""
    profile = _get_profile(request.user)
    if request.method != 'POST' or not profile or profile.department not in ('STATION', 'DIVISION'):
        return JsonResponse({'ok': False, 'error': 'Not allowed.'}, status=403)

    item = get_object_or_404(AssetRequest, id=request_id)
    if item.work_status in ('ACTIVE', 'COMPLETED'):
        return JsonResponse({'ok': False, 'error': f"{item.request_code} is already {item.work_status.lower()}."}, status=400)

    reason = request.POST.get('reason', '').strip()
    if not reason:
        return JsonResponse({'ok': False, 'error': 'Please write your message to the worker.'}, status=400)
    start = _parse_local_dt(request.POST.get('start', ''))
    end = _parse_local_dt(request.POST.get('end', ''))
    if not start:
        return JsonResponse({'ok': False, 'error': 'Please press "Reschedule time" and choose a new time.'}, status=400)
    if not end:
        end = start + timedelta(minutes=asset_duration_minutes(item))
    problem = check_reschedule_window(item, start, end)
    if problem:
        return JsonResponse({'ok': False, 'error': problem}, status=400)

    is_station = profile.department == 'STATION'
    dept_label = 'Station Master' if is_station else 'Division Master'
    item.reschedule_start, item.reschedule_end = start, end
    item.reschedule_reason = reason[:255]
    item.rescheduled_by = profile.department
    item.worker_requested_reschedule = False
    item.work_status = 'RESCHEDULED'
    # This master approves the time they picked. The other master must look at the new time again.
    if is_station:
        item.station_status, item.station_reason = 'APPROVED', ''
        item.division_status, item.division_reason = 'WAITING', ''
    else:
        item.division_status, item.division_reason = 'APPROVED', ''
        item.station_status, item.station_reason = 'WAITING', ''
    item.control_approved = False
    # The worker has to answer the new time.
    item.worker_acceptance, item.worker_reason, item.worker_response_at = 'WAITING', '', None
    item.save()

    if item.worker_id:
        s_l, e_l = start.astimezone(IST), end.astimezone(IST)
        Message.objects.create(
            sender=request.user, sender_department=profile.department, recipient_department='WORKER',
            recipient=item.worker, related_request=item,
            body=(f"{item.request_code} ({item.work_description}) - time changed by {dept_label}. "
                  f"Message: {reason}. New time: {s_l:%d %b %Y} {s_l:%H:%M}-{e_l:%H:%M}. "
                  f"Please accept or not accept."),
        )
    run_ai_scan()
    return JsonResponse({'ok': True, 'message': f"{item.request_code} sent to {item.worker.username if item.worker_id else 'the worker'} with the new time."})


@login_required(login_url='login')
def ai_scan(request):
    """'Run AI detection now' button on the AI Suggested Work page."""
    profile = _get_profile(request.user)
    if request.method != 'POST' or not profile or profile.department not in ('STATION', 'DIVISION', 'CTRL'):
        return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))
    r = run_ai_scan()
    if r['created']:
        messages.success(request, f"AI found {r['created']} asset(s) with a train-free window.")
    elif not r['idle_workers']:
        messages.success(request, "AI check done - every worker is already active, so there is nothing to suggest.")
    else:
        messages.success(request, "AI check done - no new train-free windows to suggest right now.")
    return _redirect_to_dashboard(request.user, tab='ai')


# ---------------------------------------------------------------------------
# Action endpoints: Worker responses
# ---------------------------------------------------------------------------

def _accept_and_activate(item, auto=False):
    """Worker accepted this item (or their own new time was approved). It becomes ACTIVE
    only when its start time comes - _advance_item does that (right now, if it already has)."""
    item.worker_acceptance = 'ACCEPTED'
    if not auto:                       # 'auto' = the worker was simply assigned, they did not press Accept
        item.worker_response_at = timezone.now()
    item.save()
    _advance_item(item)
    # The worker's own request for this same asset is now covered by the AI's train-free
    # window, so show it as rescheduled to that window instead of leaving a duplicate open.
    parent = item.parent if item.source == 'AI' else None
    if parent and parent.work_status not in ('ACTIVE', 'COMPLETED'):
        win_start, win_end = _schedule_window(item)
        parent.work_status = 'RESCHEDULED'
        parent.reschedule_start, parent.reschedule_end = win_start, win_end
        parent.reschedule_reason = f"Moved to {item.request_code}: AI found a train-free window"
        parent.save()


def _start_text(item):
    """'is now active' / 'becomes active at 12 Oct, 10:00' - for the messages after an approval."""
    if item.work_status == 'ACTIVE':
        return "is now active on it."
    if item.work_status == 'COMPLETED':
        return "its time has already finished."
    start, _end = _schedule_window(item)
    return f"it becomes active at {_fmt_dt_short(start)}."


def _worker_reply(request, ok, text, ajax, status=None, **extra):
    """JSON for the in-page worker forms; normal redirect + message for plain posts."""
    if ajax:
        payload = {'ok': ok, 'error': '' if ok else text, 'message': text if ok else ''}
        payload.update(extra)
        return JsonResponse(payload, status=status or (200 if ok else 400))
    (messages.success if ok else messages.error)(request, text)
    return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))


def _parse_local_dt(value):
    dt = parse_datetime((value or '').strip())
    if dt and timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def _wrong_time_payload(probe, start, end):
    """None when the time is fine. Otherwise: the 'Wrong time' text plus the next available
    train-free times on that same date, each as long as the duration that was asked for."""
    if start is None:
        return {'wrong': False, 'error': 'Please enter the date and duration, press "Available time" and choose a time.', 'slots': [], 'groups': []}
    problem = check_reschedule_window(probe, start, end)
    now = timezone.now()
    if not problem and start < now + timedelta(minutes=APPROVAL_LEAD_MIN - 5):
        problem = (f"Too soon: the Station Master and Division Master need time to approve before the buffer starts. "
                   f"The earliest start is about {timezone.localtime(now + timedelta(minutes=APPROVAL_LEAD_MIN)):%H:%M}.")
    if not problem:
        return None
    day = start.astimezone(IST).date()
    found = available_groups(probe, day=day, duration_min=int((end - start).total_seconds() // 60) if end else None,
                             lead_min=APPROVAL_LEAD_MIN)
    return {'wrong': True, 'error': problem, 'date': found['day'],
            'duration': found['duration'], 'slots': found['slots'], 'groups': found['groups'], 'note': found['note']}


@login_required(login_url='login')
def worker_respond(request, request_id):
    profile = _get_profile(request.user)
    if request.method != 'POST' or not profile or profile.department != 'WORKER':
        return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))

    ajax = request.POST.get('ajax') == '1'
    item = get_object_or_404(AssetRequest, id=request_id, worker=request.user)
    decision = request.POST.get('decision')

    # Only work that BOTH the Station Master and the Division Master approved, and that this
    # worker has not answered yet, is ever in "Needs Your Response".
    masters_moved = item.rescheduled_by in ('STATION', 'DIVISION', 'CTRL')
    if decision in ('accept', 'reject') and not (
            item.worker_acceptance == 'WAITING' and (item.both_approved or masters_moved)):
        return _worker_reply(request, False, f"{item.request_code} is not waiting for your response.", ajax)

    if decision == 'accept':
        if item.both_approved:
            _accept_and_activate(item)
            done = f"{item.request_code} accepted - {_start_text(item)}"
        else:
            # A master moved the time and the OTHER master has not approved that time yet:
            # the worker's answer is recorded and the work starts once that master approves too.
            item.worker_acceptance = 'ACCEPTED'
            item.worker_response_at = timezone.now()
            item.save()
            done = f"{item.request_code} accepted. It becomes active when the other master approves the new time."
        for dept in ('STATION', 'DIVISION'):
            Message.objects.create(
                sender=request.user, sender_department='WORKER', recipient_department=dept, related_request=item,
                body=f"{item.request_code} ({item.work_description}) - accepted by {request.user.username}.",
            )
        run_ai_scan()
        return _worker_reply(request, True, done, ajax)

    if decision == 'reject':
        reason = request.POST.get('reason', '').strip()
        if not reason:
            return _worker_reply(request, False, "Please write your message - why you cannot accept this work.", ajax)
        start = _parse_local_dt(request.POST.get('start', ''))
        end = _parse_local_dt(request.POST.get('end', ''))
        if not start:
            return _worker_reply(request, False, "Please choose the new date and time.", ajax)
        if not end:
            end = start + timedelta(minutes=asset_duration_minutes(item))

        # Wrong time (past, or a train is on the line)? Say so and offer the next free times.
        problem = _wrong_time_payload(item, start, end)
        if problem:
            return _worker_reply(request, False, problem['error'], ajax, wrong=True,
                                 date=problem.get('date', ''), duration=problem.get('duration', ''),
                                 slots=problem.get('slots', []), groups=problem.get('groups', []), note=problem.get('note', ''))

        item.worker_acceptance = 'REJECTED'
        item.worker_reason = reason
        item.worker_response_at = timezone.now()
        item.reschedule_start, item.reschedule_end = start, end
        item.reschedule_reason = f"Worker asked for a new time: {reason}"[:255]
        item.rescheduled_by = 'WORKER'
        item.worker_requested_reschedule = True
        item.work_status = 'RESCHEDULED'
        # The Station Master and the Division Master must approve (or not approve) the new time again.
        item.station_status, item.station_reason = 'WAITING', ''
        item.division_status, item.division_reason = 'WAITING', ''
        item.control_approved = False
        item.save()

        s_local, e_local = start.astimezone(IST), end.astimezone(IST)
        for dept in ('STATION', 'DIVISION', 'CTRL'):
            Message.objects.create(
                sender=request.user,
                sender_department='WORKER',
                recipient_department=dept,
                related_request=item,
                body=(f"{item.request_code} ({item.work_description}) - Not accepted by {request.user.username}. "
                      f"Message: {reason}. New time asked: {s_local:%d %b %Y} {s_local:%H:%M}-{e_local:%H:%M}. "
                      f"Please approve or not approve the new time."),
            )
        run_ai_scan()
        return _worker_reply(request, True,
                             f"{item.request_code} sent back with your message and new time. Waiting for Station Master and Division Master approval.",
                             ajax)

    return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))


@login_required(login_url='login')
def worker_request_slots(request, request_id):
    """'Show next available times' for a worker who is not accepting one of their works."""
    profile = _get_profile(request.user)
    if not profile or profile.department != 'WORKER':
        return JsonResponse({'ok': False, 'error': 'Not allowed.'}, status=403)
    item = get_object_or_404(AssetRequest, id=request_id, worker=request.user)
    day = parse_date(request.GET.get('date', '').strip()) if request.GET.get('date') else None
    if day is None:
        start, _end = _schedule_window(item)
        day = timezone.localtime(start).date() if start else None
    return JsonResponse(available_groups(item, day=day, lead_min=APPROVAL_LEAD_MIN))


def _duration_from_post(post):
    """Work duration in minutes from the Add Asset form (hours + minutes fields, or one number)."""
    try:
        if post.get('duration_min'):
            mins = int(float(post.get('duration_min')))
        else:
            mins = int(float(post.get('dur_h') or 0)) * 60 + int(float(post.get('dur_m') or 0))
    except (TypeError, ValueError):
        return 0
    return mins if 5 <= mins <= 24 * 60 else 0


def _asset_probe(post):
    """An unsaved AssetRequest built from the Add Asset form, so the AI engine can check its
    time against live trains before anything is stored. The end time is start + duration."""
    work_date = parse_date(post.get('work_date', '').strip()) if post.get('work_date') else None
    start_time = parse_time(post.get('start_time', '').strip()) if post.get('start_time') else None
    dur = _duration_from_post(post)
    probe = AssetRequest(
        section=post.get('section', '').strip(),
        from_station=post.get('from_station', '').strip(), to_station=post.get('to_station', '').strip(),
        from_junction=post.get('from_junction', '').strip(), to_junction=post.get('to_junction', '').strip(),
        work_date=work_date, start_time=start_time,
    )
    start = end = None
    if work_date and start_time and dur:
        start = _datetime.combine(work_date, start_time, tzinfo=IST)
        end = start + timedelta(minutes=dur)
        probe.end_time = end.time().replace(second=0, microsecond=0)
    return probe, start, end, dur


@login_required(login_url='login')
def worker_available_times(request):
    """'Available time' button on the Add Asset form. The worker gives a date and how long the
    work takes; this lists the train-free times of that day that are long enough (from now on,
    if the date is today). If that day has none, the nearest earlier and later days are shown."""
    profile = _get_profile(request.user)
    if request.method != 'POST' or not profile or profile.department != 'WORKER':
        return JsonResponse({'ok': False, 'error': 'Not allowed.'}, status=403)
    day = parse_date(request.POST.get('work_date', '').strip()) if request.POST.get('work_date') else None
    dur = _duration_from_post(request.POST)
    if not day:
        return JsonResponse({'ok': False, 'error': 'Please choose the asset block date first.'}, status=400)
    if not dur:
        return JsonResponse({'ok': False, 'error': 'Please enter how long the work takes (at least 5 minutes, at most 24 hours).'}, status=400)
    probe, _s, _e, _d = _asset_probe(request.POST)
    return JsonResponse(available_groups(probe, day=day, duration_min=dur, lead_min=APPROVAL_LEAD_MIN))


@login_required(login_url='login')
def worker_complete(request, request_id):
    """The worker confirms the asset is complete (also from the 'time is over' prompt)."""
    profile = _get_profile(request.user)
    ajax = request.POST.get('ajax') == '1'
    if request.method != 'POST' or not profile or profile.department != 'WORKER':
        return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))

    item = get_object_or_404(AssetRequest, id=request_id, worker=request.user)
    if item.work_status != 'ACTIVE':
        return _worker_reply(request, False, f"{item.request_code} is not active, so it cannot be marked completed.", ajax)
    item.work_status = 'COMPLETED'
    item.extension_minutes = 0
    item.save()
    for dept in ('STATION', 'DIVISION', 'CTRL'):
        Message.objects.create(sender=request.user, sender_department='WORKER', recipient_department=dept,
                               related_request=item,
                               body=f"{item.request_code} ({item.work_description}) - COMPLETED by {request.user.username}.")
    run_ai_scan()  # this worker is idle again, so the AI can look for their next asset
    return _worker_reply(request, True, f"{item.request_code} marked completed.", ajax)


@login_required(login_url='login')
def completion_prompts(request):
    """Worker dashboard (polled): assets whose end time is over and still waiting for 'complete or not'."""
    profile = _get_profile(request.user)
    if not profile or profile.department != 'WORKER':
        return JsonResponse({'prompts': [], 'pending': []})
    sync_lifecycle()
    now = timezone.now()
    prompts, pending = [], []
    for item in AssetRequest.objects.filter(worker=request.user, work_status='ACTIVE').exclude(id__in=moved_parent_ids()):
        start, end = _schedule_window(item)
        if item.extension_minutes > 0:
            pending.append({'reqId': item.id, 'code': item.request_code, 'minutes': item.extension_minutes})
        elif now >= end:
            prompts.append({'reqId': item.id, 'code': item.request_code, 'work': item.work_description,
                            'end': _hm(end), 'start': _hm(start), 'details': _asset_details(item)})
    return JsonResponse({'prompts': prompts, 'pending': pending})


@login_required(login_url='login')
def worker_request_extension(request, request_id):
    """'Not complete': the worker says how much more time is needed; the Control Office is alerted."""
    profile = _get_profile(request.user)
    if request.method != 'POST' or not profile or profile.department != 'WORKER':
        return JsonResponse({'ok': False, 'error': 'Not allowed.'}, status=403)
    item = get_object_or_404(AssetRequest, id=request_id, worker=request.user)
    if item.work_status != 'ACTIVE':
        return JsonResponse({'ok': False, 'error': f"{item.request_code} is not active."}, status=400)
    try:
        mins = int(float(request.POST.get('minutes') or 0))
    except (TypeError, ValueError):
        mins = 0
    if not 5 <= mins <= 12 * 60:
        return JsonResponse({'ok': False, 'error': 'Enter how much more time you need (5 minutes to 12 hours).'}, status=400)
    item.extension_minutes = mins
    item.extension_requested_at = timezone.now()
    item.save()
    Message.objects.create(
        sender=request.user, sender_department='WORKER', recipient_department='CTRL', related_request=item,
        body=(f"{item.request_code} ({item.work_description}) is NOT complete. {request.user.username} needs "
              f"{_dur_label(mins)} more. Please approve and block that area. {_asset_details(item)}"))
    return JsonResponse({'ok': True, 'message': f"Sent to the Control Office: {_dur_label(mins)} more for {item.request_code}."})


def _extension_alerts(now=None):
    """Control Office: a worker said the asset is not complete and asked for more time."""
    now = now or timezone.now()
    out = []
    for item in AssetRequest.objects.filter(work_status='ACTIVE', extension_minutes__gt=0):
        t = _alert_times(item)
        if not t:
            continue
        new_end = now + timedelta(minutes=item.extension_minutes)
        _f, clashes = window_clashes(item, now, new_end, now)
        worker = item.worker or item.created_by
        trains = [c['text'] for c in (clashes or [])]
        out.append({
            'kind': 'extension', 'reqId': item.id, 'code': item.request_code, 'work': item.work_description,
            'minutes': item.extension_minutes, 'worker': worker.username if worker else '',
            'end': _hm(t['end']), 'newEnd': _hm(new_end), 'details': _asset_details(item), 'trains': trains,
            'text': (f"{worker.username if worker else 'The worker'} says {item.request_code} ({item.work_description}) is NOT "
                     f"complete and needs {_dur_label(item.extension_minutes)} more (until {_hm(new_end)}). "
                     f"Approve and block that area."
                     + (f" Trains on the line: {'; '.join(trains[:3])}." if trains else '')),
        })
    return out


@login_required(login_url='login')
def control_extend(request, request_id):
    """Control Office clicks OK: the asked time is added from now, the area is blocked, everyone is told."""
    profile = _get_profile(request.user)
    if request.method != 'POST' or not profile or profile.department != 'CTRL':
        return JsonResponse({'ok': False, 'error': 'Only the Control Office can do this.'}, status=403)
    item = get_object_or_404(AssetRequest, id=request_id)
    if item.work_status != 'ACTIVE' or item.extension_minutes <= 0:
        return JsonResponse({'ok': False, 'error': f"{item.request_code} has no pending request for more time."}, status=400)
    now = timezone.now()
    mins = item.extension_minutes
    new_end = now + timedelta(minutes=mins)
    _f, clashes = window_clashes(item, now, new_end, now)
    item.extended_end = new_end
    item.extension_minutes = 0
    item.save()

    # Block that area: hold every train that would come onto the stretch until the extra time (+ buffer) is over.
    until = new_end + timedelta(minutes=BUFFER_MIN)
    blocked = []
    for c in clashes or []:
        train = Train.objects.filter(number=c['number']).first()
        if not train:
            continue
        live, _c = TrainLiveStatus.objects.get_or_create(train=train)
        live.held_until = until
        live.held_reason = f"Held by Control Office: {item.request_code} needs more time until {_hm(new_end)}"[:255]
        live.save()
        blocked.append(f"{train.number} {train.name}")
    body = (f"Control Office approved {_dur_label(mins)} more for {item.request_code} ({item.work_description}). "
            f"New end time {_hm(new_end)} (it was {_hm(_base_window(item)[1])}). "
            + (f"Blocked trains: {', '.join(blocked)} until {_hm(until)}. " if blocked else "No train needed blocking. ")
            + _asset_details(item))
    _tell(item, ('STATION', 'DIVISION'), body, sender=request.user)
    return JsonResponse({'ok': True, 'message': f"{item.request_code} extended until {_hm(new_end)}."})


@login_required(login_url='login')
def worker_create_request(request):
    profile = _get_profile(request.user)
    if request.method != 'POST' or not profile or profile.department != 'WORKER':
        return _redirect_to_dashboard(request.user, tab=request.POST.get('tab'))

    ajax = request.POST.get('ajax') == '1'

    # Wrong time (already passed, or a train is on this stretch of line)? Do not save it:
    # say "wrong time" and give the next free times on that date, as long as the asked duration.
    probe, p_start, p_end, duration = _asset_probe(request.POST)
    problem = _wrong_time_payload(probe, p_start, p_end)
    if problem:
        return _worker_reply(request, False, problem['error'], ajax, wrong=problem.get('wrong', False),
                             date=problem.get('date', ''), duration=problem.get('duration', ''),
                             slots=problem.get('slots', []), groups=problem.get('groups', []), note=problem.get('note', ''))

    last = AssetRequest.objects.order_by('-id').first()
    next_num = (last.id if last else 2290) + 1
    while AssetRequest.objects.filter(request_code=f"WR-{next_num}").exists():
        next_num += 1   # an AI copy may already use this WR- number

    work_date = probe.work_date
    start_time = probe.start_time
    end_time = probe.end_time
    # Total time is the duration the worker typed in (not typed start / end times any more).
    total_time = _dur_label(duration)

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
    run_ai_scan()  # check the new asset against live trains straight away
    if ajax:
        return JsonResponse({'ok': True, 'message': 'Request sent for approval.'})
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