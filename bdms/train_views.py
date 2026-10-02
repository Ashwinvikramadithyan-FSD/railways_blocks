"""Live train tracking: detection intake, live-status maths and the dashboard."""
import hmac
import json
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import Station, Train, TrainStop, TrainLiveStatus, DetectionEvent, UserProfile
from .railway_data import RAILWAY_DATA

# Indian Standard Time has no daylight saving, so a fixed offset is exact.
IST = dt_timezone(timedelta(hours=5, minutes=30))
CAN_RECORD = ('CTRL', 'STATION', 'DIVISION')
DASHBOARD_URL_NAME = {
    'WORKER': 'worker_dashboard', 'CTRL': 'control_dashboard',
    'STATION': 'station_dashboard', 'DIVISION': 'division_dashboard',
}


def _hhmm(t):
    return t.strftime('%H:%M') if t else None


def _iso(dt):
    return dt.astimezone(IST).isoformat() if dt else None


def _to_ist(dt):
    return dt.astimezone(IST)


def _scheduled_datetime(stop, event, detected_at):
    """The timetable time this detection should be compared with, as an aware
    datetime. Handles overnight trains by trying neighbouring start dates and
    keeping the one closest to when the train was actually detected."""
    t = stop.arrival if event == 'ARRIVED' else stop.departure
    t = t or stop.departure or stop.arrival
    if t is None:
        return None
    local = _to_ist(detected_at)
    best = None
    for shift in (-1, 0, 1):
        start_date = local.date() - timedelta(days=stop.day_offset) + timedelta(days=shift)
        cand = datetime.combine(start_date + timedelta(days=stop.day_offset), t, tzinfo=IST)
        if best is None or abs(cand - detected_at) < abs(best - detected_at):
            best = cand
    return best


def record_detection(train, station, event, detected_at=None, platform='', source=''):
    """Store one detection and update the train's live status.
    Returns (DetectionEvent, error_message)."""
    detected_at = detected_at or timezone.now()
    if timezone.is_naive(detected_at):
        detected_at = detected_at.replace(tzinfo=IST)
    if detected_at > timezone.now() + timedelta(minutes=5):
        return None, "The detection time is in the future."
    if event not in dict(DetectionEvent.EVENT_CHOICES):
        return None, "event must be ARRIVED, DEPARTED or PASSED."

    stop = TrainStop.objects.filter(train=train, station=station).order_by('seq').first()
    if stop is None:
        return None, f"{station.code} is not on the timetable of train {train.number}."

    sched = _scheduled_datetime(stop, event, detected_at)
    delay = int(round((detected_at - sched).total_seconds() / 60)) if sched else 0
    platform = platform or stop.platform

    ev = DetectionEvent.objects.create(
        train=train, station=station, stop_seq=stop.seq, event=event,
        detected_at=detected_at, scheduled_at=sched, delay_minutes=delay,
        platform=platform, source=source,
    )

    live, _ = TrainLiveStatus.objects.get_or_create(train=train)
    # Ignore out-of-order (older) detections for the live picture; they stay in the log.
    if live.last_detected_at is None or detected_at >= live.last_detected_at:
        last_seq = train.stops.order_by('-seq').values_list('seq', flat=True).first()
        if event == 'ARRIVED':
            live.state = 'TERMINATED' if stop.seq == last_seq else 'AT_STATION'
        else:
            live.state = 'TERMINATED' if (stop.seq == last_seq and event != 'PASSED') else 'RUNNING'
        live.last_seq = stop.seq
        live.delay_minutes = delay
        live.platform = platform
        live.last_detected_at = detected_at
        live.source = source
        live.save()
    return ev, None


# ---------------------------------------------------------------------------
# Serialisation for the dashboard
# ---------------------------------------------------------------------------

def build_payload():
    stations = list(Station.objects.all())
    trains = list(Train.objects.filter(is_active=True).prefetch_related('stops__station'))
    lives = {l.train_id: l for l in TrainLiveStatus.objects.all()}
    now = timezone.now()
    today_idx = _to_ist(now).weekday()  # Monday = 0

    events_by_train = {}
    for ev in DetectionEvent.objects.select_related('station').filter(
            detected_at__gte=now - timedelta(days=3)).order_by('detected_at'):
        events_by_train.setdefault(ev.train_id, []).append(ev)

    train_rows = []
    for tr in trains:
        stops = list(tr.stops.all())
        live = lives.get(tr.id)
        # Events belonging to the run currently in progress.
        run_events = []
        if live and live.last_detected_at and stops:
            evs = events_by_train.get(tr.id, [])
            # The current run starts at the most recent detection at the origin station;
            # if there is none, fall back to the last 30 hours.
            origin = [e for e in evs if e.stop_seq == stops[0].seq and e.detected_at <= live.last_detected_at]
            since = origin[-1].detected_at if origin else live.last_detected_at - timedelta(hours=30)
            run_events = [e for e in evs if e.detected_at >= since]
        actual = {}
        for e in run_events:
            slot = actual.setdefault(e.stop_seq, {})
            if e.event == 'DEPARTED':
                slot['dep'] = _hhmm(_to_ist(e.detected_at))
                slot['depDelay'] = e.delay_minutes
            else:  # ARRIVED / PASSED
                slot['arr'] = _hhmm(_to_ist(e.detected_at))
                slot['arrDelay'] = e.delay_minutes
                if e.event == 'PASSED':
                    slot['passed'] = True

        stop_rows = []
        for s in stops:
            a = actual.get(s.seq, {})
            stop_rows.append({
                'seq': s.seq, 'code': s.station.code, 'station': s.station.name,
                'arr': _hhmm(s.arrival), 'dep': _hhmm(s.departure), 'day': s.day_offset,
                'km': s.distance_km, 'plat': s.platform,
                'actArr': a.get('arr'), 'actDep': a.get('dep'),
                'arrDelay': a.get('arrDelay'), 'depDelay': a.get('depDelay'),
                'passed': a.get('passed', False),
            })

        state = live.state if live else 'NOT_STARTED'
        cur = next((s for s in stops if live and s.seq == live.last_seq), None)
        nxt = next((s for s in stops if live and live.last_seq is not None and s.seq == live.last_seq + 1), None)
        if live and live.last_seq is None and stops:
            nxt = stops[0]
        total_km = stops[-1].distance_km if stops else 0
        progress = 0
        if cur and total_km:
            progress = cur.distance_km
            if state == 'RUNNING' and nxt:
                progress = (cur.distance_km + nxt.distance_km) / 2
            progress = round(100 * progress / total_km)
        if state == 'TERMINATED':
            progress = 100

        train_rows.append({
            'id': tr.id, 'number': tr.number, 'name': tr.name, 'type': tr.get_train_type_display(),
            'days': tr.running_days, 'runsToday': tr.running_days[today_idx:today_idx + 1] == '1',
            'from': stops[0].station.name if stops else '', 'fromCode': stops[0].station.code if stops else '',
            'to': stops[-1].station.name if stops else '', 'toCode': stops[-1].station.code if stops else '',
            'km': total_km, 'stops': stop_rows,
            'live': {
                'state': state, 'seq': live.last_seq if live else None,
                'delay': live.delay_minutes if live else 0,
                'platform': live.platform if live else '',
                'detectedAt': _iso(live.last_detected_at) if live else None,
                'source': live.source if live else '',
                'cur': {'code': cur.station.code, 'name': cur.station.name} if cur else None,
                'next': {'code': nxt.station.code, 'name': nxt.station.name} if nxt else None,
                'progress': progress,
            },
        })

    recent = DetectionEvent.objects.select_related('train', 'station').order_by('-detected_at', '-id')[:150]
    return {
        'now': _iso(now),
        'stations': [{'id': s.id, 'code': s.code, 'name': s.name, 'division': s.division,
                      'platforms': s.platforms} for s in stations],
        'trains': train_rows,
        'events': [{
            'id': e.id, 'train': e.train.number, 'trainName': e.train.name,
            'code': e.station.code, 'station': e.station.name, 'event': e.event,
            'at': _iso(e.detected_at), 'sched': _iso(e.scheduled_at), 'delay': e.delay_minutes,
            'plat': e.platform, 'source': e.source,
        } for e in recent],
    }


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def _profile(user):
    return UserProfile.objects.filter(user=user).first()


@login_required(login_url='login')
def train_dashboard(request):
    profile = _profile(request.user)
    department = profile.department if profile else None
    home = DASHBOARD_URL_NAME.get(department, 'login')
    # First visit with an empty database: fill in the SAMPLE trains automatically so the
    # dashboard is never blank. Turn off with TRAIN_AUTO_SAMPLE = False in settings.py, or
    # by clicking "Remove sample data" (that leaves a cookie so it is not re-added).
    if (department in CAN_RECORD and getattr(settings, 'TRAIN_AUTO_SAMPLE', True)
            and not request.COOKIES.get('train_sample_off')
            and not Train.objects.exists() and not Station.objects.exists()):
        from .train_sample import load_sample_data
        load_sample_data()
    return render(request, 'train_dashboard.html', {
        'page_data': build_payload(),
        'railway_data': RAILWAY_DATA,
        'can_record': department in CAN_RECORD,
        'home_url_name': home,
        'department_label': profile.get_department_display() if profile else '',
    })


@login_required(login_url='login')
def train_data(request):
    """JSON feed the dashboard polls every few seconds."""
    return JsonResponse(build_payload())


def _apply(payload):
    """Shared by both detection endpoints. `payload` is a dict."""
    number = str(payload.get('train_number') or payload.get('train') or '').strip()
    code = str(payload.get('station_code') or payload.get('station') or '').strip().upper()
    event = str(payload.get('event') or '').strip().upper()
    if not number or not code or not event:
        return {'ok': False, 'error': 'train_number, station_code and event are required.'}, 400
    train = Train.objects.filter(number=number).first()
    if not train:
        return {'ok': False, 'error': f'Unknown train number {number}.'}, 404
    station = Station.objects.filter(code__iexact=code).first()
    if not station:
        return {'ok': False, 'error': f'Unknown station code {code}.'}, 404

    detected_at = None
    raw = payload.get('detected_at')
    if raw:
        detected_at = parse_datetime(str(raw))
        if detected_at is None:
            return {'ok': False, 'error': 'detected_at must look like 2026-09-28T14:35:00.'}, 400
    ev, err = record_detection(
        train, station, event, detected_at,
        platform=str(payload.get('platform') or '').strip()[:10],
        source=str(payload.get('source') or '').strip()[:40],
    )
    if err:
        return {'ok': False, 'error': err}, 400
    return {'ok': True, 'delay_minutes': ev.delay_minutes, 'event_id': ev.id}, 200


@login_required(login_url='login')
@require_POST
def train_detect(request):
    """Dashboard form (Control / Station / Division users). Normal CSRF applies."""
    profile = _profile(request.user)
    if not profile or profile.department not in CAN_RECORD:
        return JsonResponse({'ok': False, 'error': 'You are not allowed to record detections.'}, status=403)
    body, status = _apply(request.POST)
    body['source'] = 'Manual'
    return JsonResponse(body, status=status)


@csrf_exempt
@require_POST
def train_detection_api(request):
    """Machine endpoint for the detection system (camera / track sensor / GPS).
    Send header  X-API-Key: <TRAIN_DETECTION_API_KEY from settings.py>  and a JSON body:
      {"train_number": "90001", "station_code": "AJJ", "event": "ARRIVED",
       "detected_at": "2026-09-28T14:35:00", "platform": "3", "source": "Camera-AJJ-1"}"""
    key = getattr(settings, 'TRAIN_DETECTION_API_KEY', '')
    supplied = request.headers.get('X-API-Key', '')
    if not key:
        return JsonResponse({'ok': False, 'error': 'TRAIN_DETECTION_API_KEY is not set in settings.py.'}, status=403)
    if not hmac.compare_digest(key.encode(), supplied.encode()):
        return JsonResponse({'ok': False, 'error': 'Invalid API key.'}, status=403)
    try:
        payload = json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Body must be JSON.'}, status=400)
    body, status = _apply(payload)
    return JsonResponse(body, status=status)


@login_required(login_url='login')
@require_POST
def train_seed(request):
    """Load or remove the SAMPLE trains from the dashboard buttons."""
    profile = _profile(request.user)
    if not profile or profile.department not in CAN_RECORD:
        return JsonResponse({'ok': False, 'error': 'You are not allowed to change train data.'}, status=403)
    from .train_sample import load_sample_data, clear_sample_data
    if request.POST.get('action') == 'clear':
        resp = JsonResponse({'ok': True, 'message': clear_sample_data()})
        resp.set_cookie('train_sample_off', '1', max_age=60 * 60 * 24 * 365)
        return resp
    resp = JsonResponse({'ok': True, 'message': load_sample_data()})
    resp.delete_cookie('train_sample_off')
    return resp