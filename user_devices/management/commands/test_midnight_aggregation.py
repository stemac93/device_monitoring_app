from django.core.management.base import BaseCommand
from user_devices.tasks import midnight_energy_aggregation
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = (
        'Esegue subito il task midnight_energy_aggregation. ATTENZIONE: non è un '
        'test isolato, scrive un record "Data Aggregate" per gateway nel DB configurato.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--confirm',
            action='store_true',
            help='Conferma di voler scrivere i record aggregati nel database',
        )

    def handle(self, *args, **options):
        if not options['confirm']:
            self.stdout.write(self.style.WARNING(
                'Questo comando scrive record EnergyData reali nel database. '
                'Rilancia con --confirm per procedere.'
            ))
            return

        self.stdout.write(
            self.style.SUCCESS('Starting midnight energy aggregation test...')
        )

        try:
            # Execute the task directly
            midnight_energy_aggregation()

            self.stdout.write(
                self.style.SUCCESS('Midnight energy aggregation test completed successfully!')
            )

        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'Error during midnight aggregation test: {e}')
            )
            logger.error(f"Test failed: {e}")
