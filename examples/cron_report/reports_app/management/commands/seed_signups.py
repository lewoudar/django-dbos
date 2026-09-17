import uuid

from django.core.management.base import BaseCommand, CommandParser

from reports_app.models import Signup


class Command(BaseCommand):
    help = 'Create a few fake signups so the next cron firing has something to report.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('count', type=int, nargs='?', default=3)

    def handle(self, *args: object, **options: object) -> None:
        count = options['count']
        signups = [Signup(email=f'{uuid.uuid4().hex[:8]}@example.com') for _ in range(count)]
        Signup.objects.bulk_create(signups)
        self.stdout.write(self.style.SUCCESS(f'Created {count} signup(s).'))
