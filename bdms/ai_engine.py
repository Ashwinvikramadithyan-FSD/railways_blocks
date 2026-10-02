"""AI work-window detection.

What it does
------------
1. Finds every worker who is NOT active on any asset right now (an "idle" worker).
   A worker becomes active when an asset of theirs is approved by both the Station
   Master and the Division Master (or, for an AI suggestion, when they accept it).
2. Looks at that idle worker's registered assets (the ones added on the worker's
   "Add Asset" page) that are not being worked yet.
3. For each asset it checks the LIVE TRAIN DATA (timetables + detections + delays)
   for the asset's location: from/to station, from/to junction and the section end points.
   - If no train is scheduled or detected there in the asset's time window, the
     window is free.
   - If a train is due, the AI moves the window to the nearest train-free slot.
4. It then creates an "AI Suggested Work" item (AssetRequest with source='AI') that
   the Station Master and Division Master can approve or reject. Once both approve,
   it appears on the worker's Dashboard under "Needs Your Response".

`run_ai_scan()` is safe to call as often as you like: it never creates a second
suggestion for the same asset.
"""
import re
from datetime import datetime, timedelta, time as dtime, timezone as dt_timezone

from django.contrib.auth.models import User
from django.utils import timezone

from .models import AssetRequest, Message, Station, Train, TrainLiveStatus

IST = dt_timezone(timedelta(hours=5, minutes=30))

BUFFER_MIN = 15            # a work window must stay 15 minutes clear BEFORE a train enters and 15 minutes clear AFTER it leaves
DEFAULT_WINDOW_MIN = 120   # used when the asset has no start/end time
APPROVAL_LEAD_MIN = 80      # a worker-entered asset must start at least this long from now: 5 min to see the alert + 60 min of alerts before the buffer + 15 min buffer
STEP_MIN = 15              # slots start on :00 / :15 / :30 / :45 (the buffer is 15 min, so a window can sit right next to it)
HORIZON_HOURS = 48         # how far ahead the AI will look

# True : the AI makes a suggestion for EVERY worker asset, also when that worker is already active
#        on another asset (so an asset never disappears from AI Suggested Work after an Accept).
# False: only workers who are idle (not active on any asset) get suggestions.
AI_FOR_ACTIVE_WORKERS = True


# ---------------------------------------------------------------------------
# Name matching (asset forms use names such as "Arakkonam Jn"; sections use "Arakkonam")
# ---------------------------------------------------------------------------

def norm(name):
    n = re.sub(r'\s+', ' ', str(name or '').strip().lower())
    n = re.sub(r'\b(junction|jn\.?)$', '', n).strip()
    return n


def _split_section(section):
    parts = re.split(r'\s+[-\u2013\u2014]\s+', str(section or ''))
    return [p for p in parts if p.strip()] if len(parts) > 1 else []


def asset_location_names(asset):
    names = [asset.from_station, asset.to_station, asset.from_junction, asset.to_junction]
    names += _split_section(asset.section)
    seen, out = set(), []
    for n in names:
        k = norm(n)
        if k and k not in seen:
            seen.add(k)
            out.append(n.strip())
    return out


def _station_lookup():
    lookup = {}
    for st in Station.objects.all():
        lookup[norm(st.name)] = st
        lookup[norm(st.code)] = st
    return lookup


def resolve_stations(asset, lookup):
    found, missing = {}, []
    for n in asset_location_names(asset):
        st = lookup.get(norm(n))
        if st:
            found[st.id] = st
        else:
            missing.append(n)
    return found, missing


# ---------------------------------------------------------------------------
# Live train intervals
# ---------------------------------------------------------------------------

def _dt(run_date, stop, t):
    return datetime.combine(run_date + timedelta(days=stop.day_offset), t, tzinfo=IST)


def _stop_time(stop, prefer):
    a, d = stop.arrival, stop.departure
    return (a or d) if prefer == 'arr' else (d or a)


def train_intervals(train, station_ids, live, window_start, window_end, now):
    """Every time range in which `train` is on the asset's stretch of line, for the
    runs that could overlap [window_start, window_end]. Includes the live delay."""
    stops = list(train.stops.all())
    hit = [s for s in stops if s.station_id in station_ids]
    if not hit:
        return []
    first, last = hit[0], hit[-1]
    t_in, t_out = _stop_time(first, 'arr'), _stop_time(last, 'dep')
    if t_in is None or t_out is None:
        return []
    origin, end_stop = stops[0], stops[-1]
    t_origin = _stop_time(origin, 'dep')
    t_end = _stop_time(end_stop, 'arr')

    out = []
    base = window_start.astimezone(IST).date()
    for shift in range(-3, 3):
        run_date = base + timedelta(days=shift)
        if train.running_days[run_date.weekday():run_date.weekday() + 1] != '1':
            continue
        enter, leave = _dt(run_date, first, t_in), _dt(run_date, last, t_out)
        note = ''
        # Apply the live delay to the run that is on the road right now.
        if live and t_origin and t_end:
            o_dt, e_dt = _dt(run_date, origin, t_origin), _dt(run_date, end_stop, t_end)
            on_road = o_dt <= now <= e_dt + timedelta(minutes=max(live.delay_minutes, 0) + 60)
            if on_road:
                if live.state == 'CANCELLED':
                    continue
                if live.state in ('AT_STATION', 'RUNNING'):
                    d = timedelta(minutes=live.delay_minutes)
                    enter, leave = enter + d, leave + d
                    if live.delay_minutes:
                        note = f"running {live.delay_minutes:+d} min"
        if leave + timedelta(minutes=BUFFER_MIN) < window_start - timedelta(hours=1):
            continue
        if enter - timedelta(minutes=BUFFER_MIN) > window_end + timedelta(hours=1):
            continue
        out.append({'train': train, 'enter': enter, 'leave': leave, 'note': note})
    return out


def _occupied_now(trains, lives, station_ids, now):
    """Trains detected standing at one of the asset's stations right now."""
    out = []
    for tr in trains:
        live = lives.get(tr.id)
        if not live or live.state != 'AT_STATION' or live.last_seq is None:
            continue
        stop = next((s for s in tr.stops.all() if s.seq == live.last_seq), None)
        if stop and stop.station_id in station_ids:
            out.append({'train': tr, 'enter': now - timedelta(minutes=BUFFER_MIN),
                        'leave': now + timedelta(minutes=20), 'note': 'detected at station now'})
    return out


def _overlaps(iv, start, end):
    b = timedelta(minutes=BUFFER_MIN)
    return iv['enter'] - b < end and iv['leave'] + b > start


def _fmt(dt):
    return dt.astimezone(IST).strftime('%d %b %H:%M')


def _slot_dict(start, end):
    """One free window, plus the range that was checked for trains: the work time with
    BUFFER_MIN extra before it and after it (3:00-4:00 is checked as 2:45-4:15)."""
    b = timedelta(minutes=BUFFER_MIN)
    ls, le = start.astimezone(IST), end.astimezone(IST)
    cs, ce = (start - b).astimezone(IST), (end + b).astimezone(IST)
    return {
        'start_iso': ls.strftime('%Y-%m-%dT%H:%M'), 'end_iso': le.strftime('%Y-%m-%dT%H:%M'),
        'date': ls.strftime('%Y-%m-%d'), 'start': ls.strftime('%H:%M'), 'end': le.strftime('%H:%M'),
        'label': f"{ls:%d %b} {ls:%H:%M}\u2013{le:%H:%M}",
        'check_from': cs.strftime('%H:%M'), 'check_to': ce.strftime('%H:%M'),
        'checked': f"Trains checked {cs:%H:%M}\u2013{ce:%H:%M} ({BUFFER_MIN} min before and after the work)",
    }


def _jump_past(cur, clash):
    """Next start to try after `cur` clashed: the first quarter-hour that is BUFFER_MIN after the
    last clashing train has left (never less than one step ahead)."""
    nxt = max(iv['leave'] for iv in clash) + timedelta(minutes=BUFFER_MIN)
    nxt = nxt.replace(second=0, microsecond=0)
    nxt += timedelta(minutes=(-nxt.minute) % STEP_MIN)
    return nxt if nxt > cur else cur + timedelta(minutes=STEP_MIN)


# ---------------------------------------------------------------------------
# Finding a train-free window
# ---------------------------------------------------------------------------

def _asset_window(asset):
    """(requested_start | None, duration_minutes) from the asset's block date/time."""
    dur = DEFAULT_WINDOW_MIN
    if asset.start_time and asset.end_time:
        m = (asset.end_time.hour * 60 + asset.end_time.minute) - (asset.start_time.hour * 60 + asset.start_time.minute)
        if m <= 0:
            m += 24 * 60
        dur = m
    req = None
    if asset.work_date and asset.work_date.year >= 2000:
        req = datetime.combine(asset.work_date, asset.start_time or dtime(0, 0), tzinfo=IST)
    return req, dur


def find_train_free_window(asset, station_ids, trains, lives, now):
    req_start, dur = _asset_window(asset)
    step, span = timedelta(minutes=STEP_MIN), timedelta(minutes=dur)

    scan_from = now + timedelta(minutes=15)
    scan_from = scan_from.replace(second=0, microsecond=0)
    scan_from += timedelta(minutes=(-scan_from.minute) % 5)
    if req_start and req_start > scan_from:
        scan_from = req_start
    horizon_end = scan_from + timedelta(hours=HORIZON_HOURS)

    intervals = []
    for tr in trains:
        intervals += train_intervals(tr, station_ids, lives.get(tr.id), scan_from, horizon_end + span, now)
    intervals += _occupied_now(trains, lives, station_ids, now)

    first_conflict = None
    cur = scan_from
    while cur - scan_from <= timedelta(hours=HORIZON_HOURS):
        clash = [iv for iv in intervals if _overlaps(iv, cur, cur + span)]
        if not clash:
            return {'start': cur, 'end': cur + span, 'first_conflict': first_conflict,
                    'requested': req_start, 'scan_from': scan_from}
        if first_conflict is None:
            first_conflict = (cur, clash[0])
        cur = _jump_past(cur, clash)
    return None


def _note(asset, found, missing, trains_touching, slot, span_min):
    places = ', '.join(s.name for s in found.values()) or ', '.join(missing) or 'the asset location'
    s, e = slot['start'], slot['end']
    day = s.astimezone(IST).strftime('%d %b')
    parts = []
    parts.append(f"AI checked live train data for {places}: {trains_touching} train(s) use this stretch of line.")
    parts.append(f"No train is scheduled or detected there between {s.astimezone(IST):%H:%M} and {e.astimezone(IST):%H:%M} on {day}.")
    fc = slot.get('first_conflict')
    if fc:
        when, iv = fc
        rng = _fmt(iv['enter']) if iv['enter'] == iv['leave'] else f"{_fmt(iv['enter'])}\u2013{iv['leave'].astimezone(IST):%H:%M}"
        label = 'requested slot' if slot.get('requested') and when == slot['requested'] else 'earlier slot'
        parts.append(f"The {label} at {_fmt(when)} was skipped because train {iv['train'].number} "
                     f"is on that stretch ({rng}{', ' + iv['note'] if iv['note'] else ''}).")
    elif slot.get('requested') and slot['requested'] >= slot['scan_from']:
        parts.append("The time requested by the worker is clear of trains.")
    elif slot.get('requested'):
        parts.append("The time the worker requested has already passed, so the AI used the next free slot.")
    return ' '.join(parts)[:500]


# ---------------------------------------------------------------------------
# Wrong-time detection, reschedule checks and "next available times"
# ---------------------------------------------------------------------------

def _context():
    """(station lookup, active trains, live status by train id) - the live train picture."""
    lookup = _station_lookup()
    trains = list(Train.objects.filter(is_active=True).prefetch_related('stops'))
    lives = {l.train_id: l for l in TrainLiveStatus.objects.all()}
    return lookup, trains, lives


def _dur_label(mins):
    return ' '.join(p for p in (f"{mins // 60}h" if mins // 60 else '', f"{mins % 60}m" if mins % 60 else '') if p) or '0m'


def asset_duration_minutes(asset):
    """How long the worker asked for on the asset (end - start), else the default."""
    return _asset_window(asset)[1]


def _is_held(live, now):
    """A train the Control Office blocked is held outside the stretch until held_until."""
    return bool(live and getattr(live, 'held_until', None) and live.held_until > now)


def _asset_intervals(asset, ids, trains, lives, w_start, w_end, now):
    ivs = []
    for tr in trains:
        if _is_held(lives.get(tr.id), now):
            continue
        ivs += train_intervals(tr, ids, lives.get(tr.id), w_start, w_end, now)
    ivs += _occupied_now(trains, lives, ids, now)
    return ivs


def _clash_text(iv, places):
    span = _fmt(iv['enter']) if iv['enter'] == iv['leave'] else f"{iv['enter'].astimezone(IST):%H:%M}\u2013{iv['leave'].astimezone(IST):%H:%M}"
    extra = f", {iv['note']}" if iv['note'] else ''
    return (f"train {iv['train'].number} {iv['train'].name} is on {places} ({span}{extra}); "
            f"the work needs {BUFFER_MIN} minutes clear before and after every train")


def moved_parent_ids():
    """The worker's own entry for an asset that the AI has taken over.

    The AI makes ONE working copy of every worker asset (numbered WR-1001, WR-1002, ...). The masters approve, move
    and reschedule that copy, so the worker's original entry (WR-xxxx) is hidden everywhere
    and each asset shows exactly one row with exactly one time - whether the AI kept the
    worker's time, moved it, or a master or the worker rescheduled it later.

    The original comes back only if the AI copy is dropped: a master rejected it the old way
    ("Not Approved" without a new time), or the worker did not accept it and sent no new time.
    """
    ids = set()
    kids = (AssetRequest.objects.filter(source='AI', parent__isnull=False)
            .select_related('parent'))
    for k in kids:
        if k.station_status == 'REJECTED' or k.division_status == 'REJECTED':
            continue
        # The worker said "not accepted". If they also sent a new time (work_status RESCHEDULED)
        # the AI asset is still alive and waiting for re-approval, so the original stays hidden.
        if k.worker_acceptance == 'REJECTED' and k.work_status != 'RESCHEDULED':
            continue
        ids.add(k.parent_id)
    return ids


def available_slots(asset, day=None, limit=12, now=None):
    """Train-free windows, each exactly as long as the worker asked for on the asset."""
    now = now or timezone.now()
    dur = asset_duration_minutes(asset)
    span, step = timedelta(minutes=dur), timedelta(minutes=STEP_MIN)
    lookup, trains, lives = _context()
    found, _missing = resolve_stations(asset, lookup)
    ids = set(found)

    cur = (now + timedelta(minutes=15)).replace(second=0, microsecond=0)
    cur += timedelta(minutes=(-cur.minute) % STEP_MIN)
    if day:
        day_start = datetime.combine(day, dtime(0, 0), tzinfo=IST)
        day_end = day_start + timedelta(days=1)
        cur = max(cur, day_start)
        horizon_end = day_end
    else:
        horizon_end = cur + timedelta(hours=HORIZON_HOURS)

    out = {'ok': True, 'duration_min': dur, 'duration': _dur_label(dur),
           'places': [s.name for s in found.values()], 'verified': bool(ids), 'slots': [], 'note': ''}
    if cur >= horizon_end:
        out['note'] = 'That day has already passed. Choose today or a later date.'
        return out

    intervals = _asset_intervals(asset, ids, trains, lives, cur, horizon_end + span, now) if ids else []
    while cur < horizon_end and len(out['slots']) < limit:
        end = cur + span
        clash = [iv for iv in intervals if _overlaps(iv, cur, end)]
        if not clash:
            out['slots'].append(_slot_dict(cur, end))
            cur = end
        else:
            cur = _jump_past(cur, clash)
    out['buffer_min'] = BUFFER_MIN
    if not ids:
        out['note'] = "No train timetable was found for this asset's stations, so trains could not be checked."
    elif not out['slots']:
        out['note'] = f"No train-free {out['duration']} window found in this period."
    return out


def _slots_for_day(ids, trains, lives, day, span, now, limit, lead_min=15):
    """Train-free windows on one calendar day, each exactly `span` long, back to back.
    Today starts from the current time (+15 min), so nothing in the past is ever offered."""
    step = timedelta(minutes=STEP_MIN)
    day_start = datetime.combine(day, dtime(0, 0), tzinfo=IST)
    day_end = day_start + timedelta(days=1)
    cur = (now + timedelta(minutes=lead_min)).replace(second=0, microsecond=0)
    # a long lead (worker-entered asset) is rounded up to 5 minutes (0:50 + 1:20 -> 2:10), else to the quarter hour
    cur += timedelta(minutes=(-cur.minute) % (5 if lead_min > 15 else STEP_MIN))
    cur = max(cur, day_start)
    if cur >= day_end:
        return []
    ivs = []
    if ids:
        for tr in trains:
            ivs += train_intervals(tr, ids, lives.get(tr.id), cur, day_end + span, now)
        ivs += _occupied_now(trains, lives, ids, now)
    out = []
    while cur < day_end and len(out) < limit:
        end = cur + span
        clash = [iv for iv in ivs if _overlaps(iv, cur, end)]
        if not clash:
            out.append(_slot_dict(cur, end))
            cur = end
        else:
            cur = _jump_past(cur, clash)
    return out


def available_groups(asset, day=None, duration_min=None, include_today=False, now=None,
                     per_day=48, search_days=7, lead_min=15):
    """Train-free times for an asset of a given duration, grouped by day.

    - Asks for `day` (default today). If `day` is today, only times from now onwards are listed.
    - If that day has free windows, they are the answer.
    - If that day has none, the nearest EARLIER day (not before today) and the nearest LATER
      day that do have free windows are listed too.
    - include_today: when `day` is a later date, today's remaining free windows are added
      as well (Station / Division Master reschedule).
    Each window is exactly as long as the asset's work duration (2 h asset -> 2 h windows).
    """
    now = now or timezone.now()
    dur = int(duration_min or asset_duration_minutes(asset))
    dur = max(dur, 5)
    span = timedelta(minutes=dur)
    lookup, trains, lives = _context()
    found, _missing = resolve_stations(asset, lookup)
    ids = set(found)
    today = now.astimezone(IST).date()

    note_parts = []
    if day is None:
        day = today
    if day < today:
        note_parts.append(f"{day:%d %b} has already passed, so times are shown from today.")
        day = today

    cache = {}
    def slots(d):
        if d not in cache:
            cache[d] = _slots_for_day(ids, trains, lives, d, span, now, per_day, lead_min)
        return cache[d]

    def group(d, kind):
        return {'date': d.isoformat(), 'label': d.strftime('%a %d %b %Y'), 'kind': kind, 'slots': slots(d)}

    groups = []
    if slots(day):
        groups.append(group(day, 'chosen'))
        if include_today and day > today and slots(today):
            groups.insert(0, group(today, 'today'))
    else:
        note_parts.append(f"No free {_dur_label(dur)} window on {day:%d %b}.")
        prev = next((d for d in (day - timedelta(days=i) for i in range(1, search_days + 1))
                     if d >= today and slots(d)), None)
        nxt = next((d for d in (day + timedelta(days=i) for i in range(1, search_days + 1)) if slots(d)), None)
        if include_today and day > today and slots(today) and today != prev:
            groups.append(group(today, 'today'))
        if prev:
            groups.append(group(prev, 'before'))
        if nxt:
            groups.append(group(nxt, 'after'))
        if not groups:
            note_parts.append(f"No free {_dur_label(dur)} window found within {search_days} days.")
    if not ids:
        note_parts.append("No train timetable was found for this asset's stations, so trains could not be checked.")

    flat = [x for g in groups for x in g['slots']]
    return {'ok': True, 'duration_min': dur, 'duration': _dur_label(dur),
            'places': [st.name for st in found.values()], 'verified': bool(ids),
            'day': day.isoformat(), 'groups': groups, 'slots': flat, 'note': ' '.join(note_parts),
            'buffer_min': BUFFER_MIN, 'lead_min': lead_min}


def check_reschedule_window(asset, start, end, now=None):
    """Return an error message if a reschedule time is wrong, else None."""
    now = now or timezone.now()
    if start is None:
        return "Please choose the new date and time."
    if start < now - timedelta(minutes=1):
        return (f"Wrong time: {start.astimezone(IST):%d %b %Y %H:%M} is in the past. "
                "Choose a date and time from now onwards, or pick one of the next available times.")
    if end is not None and end <= start:
        return "Wrong time: the end time must be after the start time."
    end = end or start + timedelta(minutes=asset_duration_minutes(asset))
    lookup, trains, lives = _context()
    found, _m = resolve_stations(asset, lookup)
    ids = set(found)
    if not ids:
        return None
    clash = [iv for iv in _asset_intervals(asset, ids, trains, lives, start, end, now) if _overlaps(iv, start, end)]
    if clash:
        places = ', '.join(s.name for s in found.values())
        return (f"Wrong time: {_clash_text(clash[0], places)}. Work cannot be done then. "
                "Pick one of the next available times below.")
    return None


def window_clashes(asset, start, end, now=None):
    """Trains that are on the asset's stretch during start..end or within BUFFER_MIN before/after.
    Returns (stations_found, clashes); clashes is None when the asset's stations are not on any
    timetable (so trains cannot be checked). Works for past/ongoing windows too."""
    now = now or timezone.now()
    lookup, trains, lives = _context()
    found, _missing = resolve_stations(asset, lookup)
    ids = set(found)
    if not ids:
        return found, None
    places = ', '.join(s.name for s in found.values())
    out = []
    for iv in _asset_intervals(asset, ids, trains, lives, start, end, now):
        if _overlaps(iv, start, end):
            e, l = iv['enter'].astimezone(IST), iv['leave'].astimezone(IST)
            out.append({'number': iv['train'].number, 'name': iv['train'].name,
                        'enter': e.strftime('%H:%M'), 'leave': l.strftime('%H:%M'),
                        'places': places, 'note': iv['note'], 'text': _clash_text(iv, places)})
    return found, out


def _control_sender(worker):
    """Message.sender must be a user; use a Control Office user (or any admin, else the worker)."""
    u = (User.objects.filter(profile__department='CTRL').order_by('id').first()
         or User.objects.filter(is_superuser=True).order_by('id').first())
    return u or worker


def _moved_reason(asset, slot, found, missing):
    req, scan_from = slot['requested'], slot['scan_from']
    places = ', '.join(s.name for s in found.values()) or ', '.join(missing) or 'the asset location'
    when = req.astimezone(IST).strftime('%d %b %Y %H:%M')
    if req < scan_from:
        return f"the time you entered ({when}) had already passed"
    fc = slot.get('first_conflict')
    if fc:
        return f"the time you entered ({when}) is not free: {_clash_text(fc[1], places)}"
    return f"the time you entered ({when}) was not free for trains"


def _notify_worker_moved(asset, child, slot, found, missing):
    """Automatic 'why the time changed' message from the Control Office to the worker."""
    s, e = slot['start'].astimezone(IST), slot['end'].astimezone(IST)
    what = asset.work_details or asset.work_description or 'your asset'
    dur = child.total_time or _dur_label(int((slot['end'] - slot['start']).total_seconds() // 60))
    body = (f"AI rescheduled {asset.request_code} ({what}). Reason: {_moved_reason(asset, slot, found, missing)}. "
            f"New train-free time: {s:%d %b %Y} {s:%H:%M}\u2013{e:%H:%M} ({dur}). "
            f"It is now {child.request_code} and is waiting for Station Master and Division Master approval; "
            f"your original entry {asset.request_code} is replaced by it.")
    Message.objects.create(
        sender=_control_sender(asset.created_by), sender_department='CTRL', recipient_department='WORKER',
        recipient=asset.created_by, related_request=child, body=body,
    )


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------

def _next_ai_code():
    """Request code for a new AI working copy: WR-1001, WR-1002, ... (same WR- style as the
    worker's own assets). Numbers already used by ANY request are skipped, because every
    request code must be unique."""
    top = 1000
    for code in AssetRequest.objects.filter(source='AI').values_list('request_code', flat=True):
        m = re.fullmatch(r'(?:WR|AI)-(\d+)', code)
        if m:
            top = max(top, int(m.group(1)))
    n = top + 1
    while AssetRequest.objects.filter(request_code=f"WR-{n}").exists():
        n += 1
    return f"WR-{n}"


def idle_worker_ids():
    workers = set(User.objects.filter(profile__department='WORKER').values_list('id', flat=True))
    busy = set(AssetRequest.objects.filter(work_status='ACTIVE', worker__isnull=False).values_list('worker_id', flat=True))
    return workers - busy, busy


def run_ai_scan():
    """Create AI suggestions for workers' assets (see AI_FOR_ACTIVE_WORKERS). Returns a small summary dict."""
    now = timezone.now()
    idle, busy = idle_worker_ids()

    # Suggestions are never deleted. A worker who is already active keeps the AI suggestions
    # that exist for their other assets, so nothing disappears from the dashboards when a
    # worker accepts one asset.
    withdrawn = 0

    created = 0
    targets = (set(User.objects.filter(profile__department='WORKER').values_list('id', flat=True))
               if AI_FOR_ACTIVE_WORKERS else idle)
    if not targets:
        return {'created': 0, 'idle_workers': 0, 'withdrawn': withdrawn}

    lookup = _station_lookup()
    trains = list(Train.objects.filter(is_active=True).prefetch_related('stops'))
    lives = {l.train_id: l for l in TrainLiveStatus.objects.all()}

    candidates = (AssetRequest.objects
                  .filter(source='WORKER', created_by_id__in=targets)
                  .exclude(work_status__in=['ACTIVE', 'COMPLETED', 'REJECTED'])
                  .filter(ai_children__isnull=True)
                  .select_related('created_by'))

    for asset in candidates:
        if not asset_location_names(asset):
            continue
        found, missing = resolve_stations(asset, lookup)
        ids = set(found)
        if not ids:
            continue   # none of its places is on a train timetable: the AI cannot verify it, so it does not suggest it
        touching = sum(1 for tr in trains if any(s.station_id in ids for s in tr.stops.all()))
        slot = find_train_free_window(asset, ids, trains, lives, now)
        if slot is None:
            continue

        local_s, local_e = slot['start'].astimezone(IST), slot['end'].astimezone(IST)
        mins = int((slot['end'] - slot['start']).total_seconds() // 60)
        child = AssetRequest.objects.create(
            request_code=_next_ai_code(), source='AI', parent=asset,
            section=asset.section, section_code=asset.section_code, km=asset.km, track_line=asset.track_line,
            asset_type=(asset.work_details or '')[:120], work_description=asset.work_description,
            work_details=asset.work_details, division=asset.division, name=asset.name,
            phone_number=asset.phone_number, location=asset.location, priority=asset.priority or 'Normal',
            from_station=asset.from_station, to_station=asset.to_station,
            from_junction=asset.from_junction, to_junction=asset.to_junction,
            work_date=local_s.date(), start_time=local_s.time().replace(second=0, microsecond=0),
            end_time=local_e.time().replace(second=0, microsecond=0),
            total_time=' '.join(p for p in (f"{mins // 60}h" if mins // 60 else '', f"{mins % 60}m" if mins % 60 else '') if p),
            detected_at=now, is_critical=(asset.priority or '').lower() == 'critical',
            created_by=None, worker=asset.created_by,
            ai_note=_note(asset, found, missing, touching, slot, mins), trains_checked=touching,
        )
        created += 1
        # Worker entered a wrong time (past, or a train is on the line): tell them why it changed.
        req = slot.get('requested')
        if req and slot['start'] != req:
            child.rescheduled_by = 'AI'
            child.save(update_fields=['rescheduled_by'])
            _notify_worker_moved(asset, child, slot, found, missing)
    return {'created': created, 'idle_workers': len(idle), 'withdrawn': withdrawn}