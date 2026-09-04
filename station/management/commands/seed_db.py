import datetime
from django.core.management.base import BaseCommand
from django.utils import timezone
from station.models import (
    FuelType, FuelInventory, Pump, LoyaltyTier, FuelPriceHistory
)


class Command(BaseCommand):
    help = 'Seed the database with initial data'

    def add_arguments(self, parser):
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Delete all seed data and exit (does not re-seed)',
        )

    def handle(self, *args, **options):
        if options['clear']:
            self.stdout.write('Deleting seed data...')
            FuelPriceHistory.objects.all().delete()
            Pump.objects.all().delete()
            FuelInventory.objects.all().delete()
            FuelType.objects.all().delete()
            LoyaltyTier.objects.all().delete()
            self.stdout.write(self.style.SUCCESS('Seed data deleted.'))
            return

        self._seed_fuel_types()
        self._seed_fuel_inventory()
        self._seed_pumps()
        self._seed_loyalty_tiers()
        self._seed_price_history()

        self.stdout.write(self.style.SUCCESS('Database seeded successfully.'))

    def _seed_fuel_types(self):
        fuel_types = [
            {'name': 'petrol',  'description': 'Unleaded Petrol',            'current_price': 14.54},
            {'name': 'diesel',  'description': 'High Speed Diesel',          'current_price': 13.89},
            {'name': 'super',   'description': 'Super Petrol',               'current_price': 14.00},
            {'name': 'lpg',     'description': 'Liquefied Petroleum Gas',    'current_price': 9.50},
        ]
        for ft in fuel_types:
            obj, created = FuelType.objects.get_or_create(
                name=ft['name'],
                defaults={'description': ft['description'], 'current_price': ft['current_price']},
            )
            status = 'Created' if created else 'Already exists'
            self.stdout.write(f'  FuelType [{status}]: {obj.get_name_display()}')

    def _seed_fuel_inventory(self):
        inventory = [
            {'fuel': 'petrol',  'qty': 25000, 'min': 2000, 'max': 50000, 'cost': 12.00},
            {'fuel': 'diesel',  'qty': 30000, 'min': 2000, 'max': 60000, 'cost': 11.50},
            {'fuel': 'super',   'qty': 25000, 'min': 2000, 'max': 50000, 'cost': 12.00},
            {'fuel': 'lpg',     'qty': 5000,  'min': 500,  'max': 10000, 'cost': 7.00},
        ]
        for inv in inventory:
            fuel = FuelType.objects.get(name=inv['fuel'])
            obj, created = FuelInventory.objects.get_or_create(
                fuel_type=fuel,
                defaults={
                    'quantity_liters': inv['qty'],
                    'min_threshold': inv['min'],
                    'max_capacity': inv['max'],
                    'cost_per_liter': inv['cost'],
                },
            )
            status = 'Created' if created else 'Already exists'
            self.stdout.write(f'  FuelInventory [{status}]: {fuel.get_name_display()}')

    def _seed_pumps(self):
        pumps = [
            {'number': 'P1', 'fuel': 'petrol', 'status': 'operational'},
            {'number': 'P2', 'fuel': 'petrol', 'status': 'operational'},
            {'number': 'D1', 'fuel': 'diesel', 'status': 'operational'},
            {'number': 'D2', 'fuel': 'diesel', 'status': 'maintenance'},
            {'number': 'S1', 'fuel': 'super',   'status': 'operational'},
            {'number': 'L1', 'fuel': 'lpg',    'status': 'operational'},
        ]
        for p in pumps:
            fuel = FuelType.objects.get(name=p['fuel'])
            obj, created = Pump.objects.get_or_create(
                pump_number=p['number'],
                defaults={
                    'fuel_type': fuel,
                    'status': p['status'],
                    'installation_date': datetime.date(2024, 1, 15),
                },
            )
            status = 'Created' if created else 'Already exists'
            self.stdout.write(f'  Pump [{status}]: {obj.pump_number}')

    def _seed_loyalty_tiers(self):
        tiers = [
            {'name': 'bronze',   'min_points': 0,    'multiplier': 1.00, 'discount': 0.00, 'benefits': 'Basic rewards membership'},
            {'name': 'silver',   'min_points': 500,  'multiplier': 1.25, 'discount': 2.00, 'benefits': '2% discount, priority service'},
            {'name': 'gold',     'min_points': 1500, 'multiplier': 1.50, 'discount': 5.00, 'benefits': '5% discount, free car wash monthly'},
            {'name': 'platinum', 'min_points': 5000, 'multiplier': 2.00, 'discount': 10.0, 'benefits': '10% discount, dedicated attendant'},
        ]
        for t in tiers:
            obj, created = LoyaltyTier.objects.get_or_create(
                name=t['name'],
                defaults={
                    'min_points': t['min_points'],
                    'points_multiplier': t['multiplier'],
                    'discount_percentage': t['discount'],
                    'benefits': t['benefits'],
                },
            )
            status = 'Created' if created else 'Already exists'
            self.stdout.write(f'  LoyaltyTier [{status}]: {obj.get_name_display()}')

    def _seed_price_history(self):
        records = [
            {'diesel': 12.5,  'petrol': 25.5,  'super': 26.0,  'current_rate': 13.4,  'date': datetime.date(2026, 5, 5)},
            {'diesel': 14.2,  'petrol': 45.99, 'super': 46.5,  'current_rate': 15.2,  'date': datetime.date(2026, 5, 5)},
            {'diesel': 12.5,  'petrol': 25.5,  'super': 26.0,  'current_rate': 13.4,  'date': datetime.date(2026, 5, 5)},
            {'diesel': 1.55,  'petrol': 1.45,  'super': 1.55,  'current_rate': 0.85,  'date': datetime.date(2026, 5, 7)},
            {'diesel': 1.55,  'petrol': 1.45,  'super': 1.55,  'current_rate': 0.85,  'date': datetime.date(2026, 5, 9)},
            {'diesel': 39.0,  'petrol': 18.0,  'super': 18.5,  'current_rate': 17.0,  'date': datetime.date(2026, 5, 9)},
            {'diesel': 397.0, 'petrol': 18.0,  'super': 18.5,  'current_rate': 17.0,  'date': datetime.date(2026, 5, 11)},
            {'diesel': 84.0,  'petrol': 23.0,  'super': 23.5,  'current_rate': 67.0,  'date': datetime.date(2026, 5, 13)},
            {'diesel': 84.0,  'petrol': 23.0,  'super': 23.5,  'current_rate': 67.0,  'date': datetime.date(2026, 6, 9)},
        ]
        created_count = 0
        for r in records:
            exists = FuelPriceHistory.objects.filter(
                diesel=r['diesel'],
                petrol=r['petrol'],
                super=r['super'],
                current_rate=r['current_rate'],
                created_at=r['date'],
            ).exists()
            if not exists:
                FuelPriceHistory.objects.create(
                    diesel=r['diesel'],
                    petrol=r['petrol'],
                    super=r['super'],
                    current_rate=r['current_rate'],
                    created_at=r['date'],
                )
                created_count += 1
        self.stdout.write(f'  FuelPriceHistory: {created_count} new records added ({len(records) - created_count} already existed)')
