from django.core.management.base import BaseCommand

from bdms.train_sample import load_sample_data, clear_sample_data


class Command(BaseCommand):
    help = "Load (or with --clear remove) SAMPLE trains, timetables and detection history."

    def add_arguments(self, parser):
        parser.add_argument('--clear', action='store_true', help='Remove the sample data instead.')

    def handle(self, *args, **opts):
        msg = clear_sample_data() if opts['clear'] else load_sample_data()
        self.stdout.write(self.style.SUCCESS(msg))