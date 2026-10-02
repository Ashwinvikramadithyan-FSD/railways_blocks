"""SAMPLE stations, trains, timetables and detection history for the Train dashboard.

Every train is named "Sample ..." and numbered 90001-90014 so it can never be
mistaken for a real timetable. Timetables are generated around the current time,
so when you load them some trains are running, some are at a station, one has
reached its destination, one has not started and one is cancelled.

Load / remove from the dashboard (Live Trains page buttons) or from a terminal:
    python manage.py seed_trains            /   python manage.py seed_trains --clear
"""
from datetime import timedelta

from django.utils import timezone

from .models import Station, Train, TrainStop, TrainLiveStatus, DetectionEvent
from .train_views import IST, record_detection

# code, name, division, platforms
STATIONS = [
    ('MAS', 'Chennai Central', 'Chennai', 12), ('BBQ', 'Basin Bridge Jn', 'Chennai', 4),
    ('PER', 'Perambur', 'Chennai', 4), ('AVD', 'Avadi', 'Chennai', 3), ('TRL', 'Tiruvallur', 'Chennai', 4),
    ('AJJ', 'Arakkonam Jn', 'Chennai', 6), ('KPD', 'Katpadi Jn', 'Chennai', 5),
    ('TBM', 'Tambaram', 'Chennai', 6), ('CGL', 'Chengalpattu Jn', 'Chennai', 5),
    ('JTJ', 'Jolarpettai Jn', 'Salem', 4), ('SA', 'Salem Jn', 'Salem', 6), ('ED', 'Erode Jn', 'Salem', 7),
    ('TUP', 'Tiruppur', 'Salem', 3), ('CBE', 'Coimbatore Jn', 'Salem', 6),
    ('VM', 'Villupuram Jn', 'Tiruchchirappalli', 5), ('VRI', 'Vriddhachalam Jn', 'Tiruchchirappalli', 4),
    ('TPJ', 'Tiruchchirappalli Jn', 'Tiruchchirappalli', 7),
    ('DG', 'Dindigul Jn', 'Madurai', 4), ('MDU', 'Madurai Jn', 'Madurai', 5),
    ('TEN', 'Tirunelveli Jn', 'Madurai', 5), ('NCJ', 'Nagercoil Jn', 'Thiruvananthapuram', 3),
    ('TVC', 'Thiruvananthapuram Central', 'Thiruvananthapuram', 8), ('QLN', 'Kollam Jn', 'Thiruvananthapuram', 4),
    ('ERS', 'Ernakulam Jn', 'Thiruvananthapuram', 6), ('TCR', 'Thrissur', 'Palakkad', 4),
    ('PGT', 'Palakkad Jn', 'Palakkad', 6),
]

# (station code, km from origin, minutes after departure from origin, halt minutes)
R_MAS_CBE = [('MAS', 0, 0, 0), ('AJJ', 69, 70, 2), ('KPD', 132, 135, 2), ('JTJ', 214, 200, 2),
             ('SA', 335, 285, 5), ('ED', 393, 335, 3), ('TUP', 447, 375, 2), ('CBE', 495, 415, 0)]
R_MAS_MDU = [('MAS', 0, 0, 0), ('TBM', 30, 35, 2), ('CGL', 56, 60, 2), ('VM', 160, 140, 3),
             ('VRI', 215, 180, 2), ('TPJ', 332, 270, 5), ('DG', 410, 330, 2), ('MDU', 497, 385, 0)]
R_MAS_TVC = [('MAS', 0, 0, 0), ('VM', 160, 150, 3), ('TPJ', 332, 300, 5), ('MDU', 497, 400, 5),
             ('TEN', 622, 520, 3), ('NCJ', 690, 600, 2), ('TVC', 765, 650, 0)]
R_SA_PGT = [('SA', 0, 0, 0), ('ED', 58, 60, 2), ('TUP', 112, 110, 2), ('CBE', 160, 150, 3), ('PGT', 210, 200, 0)]
R_PGT_TVC = [('PGT', 0, 0, 0), ('TCR', 75, 70, 2), ('ERS', 130, 130, 5), ('QLN', 260, 240, 3), ('TVC', 305, 290, 0)]
R_SUBURBAN = [('MAS', 0, 0, 0), ('BBQ', 5, 8, 1), ('PER', 9, 16, 1), ('AVD', 26, 35, 1), ('TRL', 41, 50, 1), ('AJJ', 69, 75, 0)]
R_FREIGHT = [('MAS', 0, 0, 0), ('AJJ', 69, 110, 0), ('KPD', 132, 200, 0), ('JTJ', 214, 290, 0), ('SA', 335, 420, 0)]


def reverse(route):
    km_total, t_total = route[-1][1], route[-1][2]
    out = [(c, km_total - km, t_total - m, h) for c, km, m, h in reversed(route)]
    out[0] = (out[0][0], out[0][1], out[0][2], 0)
    out[-1] = (out[-1][0], out[-1][1], out[-1][2], 0)
    return out


# number, name, type, route, running days (Mon..Sun), minutes since it left its origin
# (negative = leaves in the future), typical delay in minutes, stations it passes without stopping
TRAINS = [
    dict(no='90001', name='Sample Chennai - Coimbatore Express', type='EXPRESS', route=R_MAS_CBE, days='1111111', since=299, delay=8, hold=5),
    dict(no='90002', name='Sample Coimbatore - Chennai Superfast', type='SUPERFAST', route=reverse(R_MAS_CBE), days='1111111', since=240, delay=0),
    dict(no='90003', name='Sample Chennai - Madurai Express', type='EXPRESS', route=R_MAS_MDU, days='1111100', since=100, delay=15),
    dict(no='90004', name='Sample Madurai - Chennai Mail', type='MAIL', route=reverse(R_MAS_MDU), days='1111111', since=300, delay=-3),
    dict(no='90005', name='Sample Salem - Palakkad Passenger', type='PASSENGER', route=R_SA_PGT, days='1111111', since=95, delay=5),
    dict(no='90006', name='Sample Chennai - Coimbatore Weekend Special', type='SPECIAL', route=R_MAS_CBE, days='0000011', since=-45, delay=0),
    dict(no='90007', name='Sample Palakkad - Thiruvananthapuram Express', type='EXPRESS', route=R_PGT_TVC, days='1111111', since=120, delay=12),
    dict(no='90008', name='Sample Thiruvananthapuram - Palakkad Express', type='EXPRESS', route=reverse(R_PGT_TVC), days='1111111', since=200, delay=25),
    dict(no='90009', name='Sample Chennai - Thiruvananthapuram Superfast', type='SUPERFAST', route=R_MAS_TVC, days='1111111', since=380, delay=0),
    dict(no='90010', name='Sample Thiruvananthapuram - Chennai Mail', type='MAIL', route=reverse(R_MAS_TVC), days='1111111', since=262, delay=6, hold=4),
    dict(no='90011', name='Sample Chennai - Arakkonam Local', type='SUBURBAN', route=R_SUBURBAN, days='1111111', since=40, delay=2),
    dict(no='90012', name='Sample Arakkonam - Chennai Local', type='SUBURBAN', route=reverse(R_SUBURBAN), days='1111111', since=90, delay=0),
    dict(no='90013', name='Sample Chennai - Salem Goods', type='FREIGHT', route=R_FREIGHT, days='1111111', since=250, delay=40, passes={'AJJ', 'KPD', 'JTJ'}),
    dict(no='90014', name='Sample Chennai - Madurai Special (cancelled)', type='SPECIAL', route=R_MAS_MDU, days='1111111', since=-30, delay=0, cancelled=True),
]
SAMPLE_NUMBERS = [t['no'] for t in TRAINS]


def _replay(train, stops, stations, start, delay, salt, now, passes, hold=None):
    """Record the detections a train running on `start` would have produced up to `now`.
    `hold` = stop number where the train is still standing at the platform (no departure yet)."""
    number = int(train.number)
    one_min = timedelta(minutes=1)
    prev_dep_det = None
    made = 0
    for idx, s in enumerate(stops):
        code, halt = s['code'], s['halt']
        first, last = idx == 0, idx == len(stops) - 1
        arr_dt = start + timedelta(minutes=s['mins'])
        dep_dt = arr_dt + timedelta(minutes=halt)
        jitter = ((idx * 5 + number + salt) % 5) - 2
        ad = delay + (0 if first else jitter)
        arr_det = arr_dt + timedelta(minutes=ad)
        if prev_dep_det is not None:
            arr_det = max(arr_det, prev_dep_det + one_min)
        dep_det = max(dep_dt + timedelta(minutes=ad - (1 if halt >= 3 else 0)), arr_det + one_min)
        plat = s['plat']
        if code in passes and not first and not last:
            if arr_det <= now:
                record_detection(train, stations[code], 'PASSED', arr_det, plat, 'Sample data'); made += 1
            prev_dep_det = arr_det
            continue
        if not first and arr_det <= now:
            record_detection(train, stations[code], 'ARRIVED', arr_det, plat, 'Sample data'); made += 1
        if hold == idx + 1:          # standing at the platform right now
            break
        if not last and dep_det <= now:
            record_detection(train, stations[code], 'DEPARTED', dep_det, plat, 'Sample data'); made += 1
        prev_dep_det = dep_det
    return made


def load_sample_data():
    """(Re)create the sample data. Safe to run more than once. Returns a summary string."""
    stations = {}
    for code, name, division, plats in STATIONS:
        stations[code], _ = Station.objects.get_or_create(
            code=code, defaults={'name': name, 'division': division, 'platforms': plats})

    now = timezone.now()
    now_ist = now.astimezone(IST)
    events_total = 0
    for spec in TRAINS:
        Train.objects.filter(number=spec['no']).delete()
        train = Train.objects.create(number=spec['no'], name=spec['name'], train_type=spec['type'], running_days=spec['days'])
        route = spec['route']
        start = now_ist - timedelta(minutes=spec['since'])
        stops = []
        for seq, (code, km, mins, halt) in enumerate(route, start=1):
            first, last = seq == 1, seq == len(route)
            arr_dt = start + timedelta(minutes=mins)
            dep_dt = arr_dt + timedelta(minutes=halt)
            plat = str(1 + (seq * 2 + int(spec['no'][-1])) % stations[code].platforms)
            TrainStop.objects.create(
                train=train, station=stations[code], seq=seq,
                arrival=None if first else arr_dt.time().replace(second=0, microsecond=0),
                departure=None if last else dep_dt.time().replace(second=0, microsecond=0),
                day_offset=0 if first else (arr_dt.date() - start.date()).days,
                distance_km=km, platform=plat)
            stops.append({'code': code, 'mins': mins, 'halt': halt, 'plat': plat})
        live, _ = TrainLiveStatus.objects.get_or_create(train=train)
        passes = spec.get('passes', set())

        # Yesterday's completed run -> detection history.
        prev_start = start - timedelta(days=1)
        if spec['days'][prev_start.weekday()] == '1' and not spec.get('cancelled'):
            events_total += _replay(train, stops, stations, prev_start, spec['delay'] + 3, 7, now, passes)

        runs_today = spec['days'][start.weekday()] == '1'
        if spec.get('cancelled'):
            live.state, live.last_seq, live.last_detected_at = 'CANCELLED', None, None
            live.delay_minutes, live.platform, live.source = 0, '', ''
            live.save()
        elif spec['since'] < 0 or not runs_today:
            live.state, live.last_seq, live.last_detected_at = 'NOT_STARTED', None, None
            live.delay_minutes, live.platform, live.source = 0, '', ''
            live.save()
        else:
            events_total += _replay(train, stops, stations, start, spec['delay'], 0, now, passes, spec.get('hold'))
    return (f"Loaded {len(TRAINS)} sample trains with full timetables, {len(STATIONS)} stations "
            f"and {events_total} detection records.")


def clear_sample_data():
    """Remove the sample trains (and any sample station nothing else uses)."""
    trains = Train.objects.filter(number__in=SAMPLE_NUMBERS)
    count = trains.count()
    trains.delete()
    for code, *_ in STATIONS:
        st = Station.objects.filter(code=code).first()
        if st and not st.stops.exists() and not st.events.exists():
            st.delete()
    return f"Removed {count} sample trains."