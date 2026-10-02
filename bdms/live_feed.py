"""Live data feed for the Train dashboard.

Two sources, both save through record_detection(), so delays, live status, the
progress bar and the event log all update exactly like a manual detection:

  1. REAL   - a live-running-status API (RapidAPI "Indian Railway IRCTC" by default).
  2. SIMULATE - no key needed. Moves the sample trains along their timetables.
"""
import json
import random
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import Train, Station, DetectionEvent, TrainLiveStatus
from .train_views import IST, record_detection

DEFAULT_URL = 'https://indian-railway-irctc.p.rapidapi.com/api/trains/v1/train/status'


def _stop_dt(start_date, stop, t):
    return datetime.combine(start_date + timedelta(days=stop.day_offset), t, tzinfo=IST)


def active_run_start(train, stops, now):
    """Start date (IST) of the run that is running now, or None."""
    if not stops:
        return None
    first, last = stops[0], stops[-1]
    t_first = first.departure or first.arrival
    t_last = last.arrival or last.departure
    if t_first is None or t_last is None:
        return None
    today = now.astimezone(IST).date()
    for start in (today - timedelta(days=3), today - timedelta(days=2), today - timedelta(days=1), today):
        if train.running_days[start.weekday():start.weekday() + 1] != '1':
            continue
        begin = _stop_dt(start, first, t_first) - timedelta(minutes=10)
        end = _stop_dt(start, last, t_last) + timedelta(hours=3)
        if begin <= now <= end:
            return start
    return None


# ---------------------- 1. REAL provider ----------------------

def fetch_raw(train_number, start_date):
    url = getattr(settings, 'LIVE_TRAIN_API_URL', DEFAULT_URL)
    headers = dict(getattr(settings, 'LIVE_TRAIN_API_HEADERS', {}))
    params = dict(getattr(settings, 'LIVE_TRAIN_API_PARAMS', {'isH5': 'true', 'client': 'w'}))
    params.setdefault('train_number', train_number)
    params.setdefault('departure_date', start_date.strftime('%Y%m%d'))
    req = urllib.request.Request(url + '?' + urllib.parse.urlencode(params), headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _norm(key):
    return str(key).lower().replace('_', '').replace('-', '')


CODE_KEYS = ('stationcode', 'stncode', 'stationcd', 'code')
ARR_KEYS = ('actualarrival', 'actarr', 'actualarr', 'actualarrivaltime', 'arrivedat')
DEP_KEYS = ('actualdeparture', 'actdep', 'actualdep', 'actualdeparturetime', 'departedat')


def _pick(d, keys):
    flat = {_norm(k): v for k, v in d.items()}
    for k in keys:
        v = flat.get(k)
        if v not in (None, '', '--', '-', 'NA', 'N/A'):
            return v
    return None


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _parse_time(value, now):
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit() and len(value) > 6):
            v = float(value)
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=IST)
        text = str(value).strip()
        if len(text) >= 8 and ('-' in text or 'T' in text):
            dt = parse_datetime(text.replace(' ', 'T', 1))
            if dt is not None:
                return dt if timezone.is_aware(dt) else dt.replace(tzinfo=IST)
        parts = text.split(':')
        if len(parts) >= 2:
            hh, mm = int(parts[0]), int(parts[1][:2])
            today = now.astimezone(IST).date()
            cands = [datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)
                     for d in (today - timedelta(days=1), today)]
            cands = [c for c in cands if c <= now + timedelta(minutes=5)]
            return max(cands) if cands else None
    except (ValueError, TypeError, OverflowError):
        return None
    return None


def extract_events(data, now):
    out = []
    for d in _walk(data):
        code = _pick(d, CODE_KEYS)
        if not isinstance(code, str) or not (1 <= len(code) <= 6):
            continue
        for keys, event in ((ARR_KEYS, 'ARRIVED'), (DEP_KEYS, 'DEPARTED')):
            raw = _pick(d, keys)
            dt = _parse_time(raw, now) if raw is not None else None
            if dt is not None:
                out.append((code.upper(), event, dt))
    return out


def apply_events(train, events, source):
    saved, skipped = 0, set()
    for code, event, dt in sorted(events, key=lambda e: e[2]):
        station = Station.objects.filter(code__iexact=code).first()
        if station is None:
            skipped.add(code)
            continue
        if DetectionEvent.objects.filter(train=train, station=station, event=event,
                                         detected_at__gte=dt - timedelta(minutes=1),
                                         detected_at__lte=dt + timedelta(minutes=1)).exists():
            continue
        ev, err = record_detection(train, station, event, dt, source=source)
        if ev:
            saved += 1
        else:
            skipped.add(code)
    return saved, skipped


def update_from_api(train, now, raw=False):
    stops = list(train.stops.select_related('station'))
    start = active_run_start(train, stops, now) or now.astimezone(IST).date()
    data = fetch_raw(train.number, start)
    if raw:
        return data, 0, set()
    events = extract_events(data, now)
    saved, skipped = apply_events(train, events, 'Live API')
    return data, saved, skipped


# ---------------------- 2. SIMULATOR ----------------------

def simulate_train(train, now):
    stops = list(train.stops.select_related('station').order_by('seq'))
    start = active_run_start(train, stops, now)
    if start is None:
        return 0
    live = TrainLiveStatus.objects.filter(train=train).first()
    delay = live.delay_minutes if live else random.choice([0, 2, 5, 9])
    since = _stop_dt(start, stops[0], stops[0].departure or stops[0].arrival) - timedelta(hours=1)
    count = 0
    for s in stops:
        for event, t in (('ARRIVED', s.arrival), ('DEPARTED', s.departure)):
            if t is None:
                continue
            sched = _stop_dt(start, s, t)
            delay = max(-5, min(90, delay + random.choice([-1, 0, 0, 0, 1, 1, 2])))
            when = sched + timedelta(minutes=delay)
            if when > now:
                return count
            if DetectionEvent.objects.filter(train=train, station=s.station, event=event,
                                             detected_at__gte=since).exists():
                continue
            ev, _ = record_detection(train, s.station, event, when, source='Simulator')
            count += 1 if ev else 0
    return count


def run_once(simulate=False, numbers=None, raw=False, out=print):
    now = timezone.now()
    trains = Train.objects.filter(is_active=True).order_by('number')
    if numbers:
        trains = trains.filter(number__in=numbers)
    total = 0
    for tr in trains:
        try:
            if simulate:
                n = simulate_train(tr, now)
                if n:
                    out(f'  {tr.number} {tr.name}: {n} new detection(s)')
                total += n
            else:
                stops = list(tr.stops.all())
                if not numbers and active_run_start(tr, stops, now) is None:
                    continue
                data, n, skipped = update_from_api(tr, now, raw=raw)
                if raw:
                    out(json.dumps(data, indent=2)[:6000])
                    continue
                out(f'  {tr.number} {tr.name}: {n} new detection(s)'
                    + (f' (stations not in your database: {", ".join(sorted(skipped))})' if skipped else ''))
                total += n
        except Exception as exc:
            out(f'  {tr.number}: FAILED - {exc}')
    return total