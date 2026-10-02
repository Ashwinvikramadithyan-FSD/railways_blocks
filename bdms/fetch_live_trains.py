import time

from django.core.management.base import BaseCommand

from bdms.live_feed import run_once


class Command(BaseCommand):
    help = "Pull live train running data into the Train dashboard (real API, or --simulate)."

    def add_arguments(self, parser):
        parser.add_argument('--simulate', action='store_true', help='No API key: move the sample trains in real time.')
        parser.add_argument('--train', nargs='*', help='Only these train numbers, e.g. --train 12671 12672')
        parser.add_argument('--loop', type=int, default=0, metavar='SECONDS', help='Repeat every N seconds (Ctrl+C to stop).')
        parser.add_argument('--raw', action='store_true', help='Print the raw API reply and save nothing.')

    def handle(self, *args, **o):
        mode = 'SIMULATOR' if o['simulate'] else 'LIVE API'
        self.stdout.write(self.style.SUCCESS(f'Train live feed started ({mode}).'))
        try:
            while True:
                self.stdout.write(time.strftime('[%H:%M:%S] updating...'))
                total = run_once(o['simulate'], o['train'], o['raw'], out=self.stdout.write)
                self.stdout.write(f'  done: {total} new detection(s)')
                if not o['loop']:
                    break
                time.sleep(o['loop'])
        except KeyboardInterrupt:
            self.stdout.write('Stopped.')