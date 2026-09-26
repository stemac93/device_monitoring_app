from django.core.management.base import BaseCommand
from django.utils import timezone
from user_devices.models import Gateway, GatewayData, Device, EnergyData
from datetime import datetime, timedelta
import random
import math


class Command(BaseCommand):
    help = 'Generate 30 days of fake gateway and energy meter data for testing'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=30,
            help='Number of days to generate (default: 30)'
        )
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Clear existing data before generating new data'
        )

    def handle(self, *args, **options):
        days = options['days']
        clear_data = options['clear']
        
        # Clear existing data if requested: solo l'intervallo che verrà rigenerato
        # (GatewayData) e solo i device "energy meter" (EnergyData), non tutto il DB
        if clear_data:
            self.stdout.write('Clearing existing data...')
            since = timezone.make_aware(datetime.combine(timezone.localdate() - timedelta(days=days - 1), datetime.min.time()))
            gateway_data_qs = GatewayData.objects.filter(timestamp__gte=since)
            energy_data_qs = EnergyData.objects.filter(
                timestamp__gte=since, device_name__name__icontains='energy meter')
            gateway_data_count = gateway_data_qs.count()
            energy_data_count = energy_data_qs.count()

            if gateway_data_count > 0 or energy_data_count > 0:
                confirm = input(f'This will delete {gateway_data_count} gateway records and {energy_data_count} energy records since {since:%Y-%m-%d}. Continue? (y/N): ')
                if confirm.lower() != 'y':
                    self.stdout.write(self.style.WARNING('Operation cancelled.'))
                    return

                gateway_data_qs.delete()
                energy_data_qs.delete()
                self.stdout.write(self.style.SUCCESS(f'Deleted {gateway_data_count} gateway records and {energy_data_count} energy records.'))
            else:
                self.stdout.write('No existing data to clear.')
        
        # Get all gateways
        gateways = Gateway.objects.all()
        if not gateways.exists():
            self.stdout.write(
                self.style.ERROR('No gateways found. Please create at least one gateway first.')
            )
            return
        
        self.stdout.write(f'Generating {days} days of fake data for {gateways.count()} gateway(s)...')
        
        total_gateway_records = 0
        total_energy_records = 0
        
        for gateway in gateways:
            self.stdout.write(f'Processing gateway: {gateway.name}')
            
            # Generate data for each day
            for day_offset in range(days):
                current_date = timezone.localdate() - timedelta(days=day_offset)
                
                # Generate data for each 15-minute interval of the day
                for hour in range(24):
                    for minute in [0, 15, 30, 45]:
                        # Create timestamp for this interval
                        timestamp = timezone.make_aware(
                            datetime.combine(current_date, datetime.min.time())
                        ) + timedelta(hours=hour, minutes=minute)
                        
                        # Generate realistic solar data based on time of day
                        data = self.generate_solar_data(hour, minute)
                        
                        # Create or update GatewayData record
                        gateway_data, created = GatewayData.objects.get_or_create(
                            Gateway=gateway,
                            timestamp=timestamp,
                            defaults={'data': data}
                        )
                        
                        if created:
                            total_gateway_records += 1
                            
                            # Set users if gateway has users
                            if hasattr(gateway, 'user'):
                                gateway_data.user.set(gateway.user.all())
            
            self.stdout.write(f'  Generated gateway data for {gateway.name}')
        
        # Generate energy meter data
        self.stdout.write('Generating energy meter data...')
        energy_meters = Device.objects.filter(name__icontains='energy meter')
        if not energy_meters.exists():
            self.stdout.write(
                self.style.WARNING('No energy meter devices found. Please create devices with "energy meter" in the name.')
            )
        else:
            for energy_meter in energy_meters:
                self.stdout.write(f'Processing energy meter: {energy_meter.name}')
                
                # Generate energy data for each day
                for day_offset in range(days):
                    current_date = timezone.localdate() - timedelta(days=day_offset)
                    
                    # Generate data for each 15-minute interval of the day
                    for hour in range(24):
                        for minute in [0, 15, 30, 45]:
                            # Create timestamp for this interval with 15-minute shift for energy data
                            timestamp = timezone.make_aware(
                                datetime.combine(current_date, datetime.min.time())
                            ) + timedelta(hours=hour, minutes=minute + 15)
                            
                            # Generate realistic energy data based on time of day
                            energy_data = self.generate_energy_meter_data(hour, minute, day_offset)
                            
                            # EnergyData.timestamp è auto_now_add: il valore passato a
                            # create() viene ignorato, quindi lo imposto dopo con update()
                            created = not EnergyData.objects.filter(
                                device_name=energy_meter, timestamp=timestamp).exists()
                            if created:
                                energy_record = EnergyData.objects.create(
                                    Gateway=energy_meter.Gateway,
                                    device_name=energy_meter,
                                    data=energy_data,
                                )
                                EnergyData.objects.filter(pk=energy_record.pk).update(timestamp=timestamp)

                            if created:
                                total_energy_records += 1
                                
                                # Set users if device has users
                                if hasattr(energy_meter, 'user'):
                                    energy_record.user.set(energy_meter.user.all())
                
                self.stdout.write(f'  Generated energy data for {energy_meter.name}')
        
        self.stdout.write(
            self.style.SUCCESS(f'Successfully generated {total_gateway_records} gateway records and {total_energy_records} energy records!')
        )

    def generate_solar_data(self, hour, minute):
        """Generate realistic solar energy data based on time of day"""
        
        # Convert to decimal hour for calculations
        decimal_hour = hour + minute / 60.0
        
        # Solar irradiance pattern (peaks around noon)
        # Assuming daylight hours from 6 AM to 6 PM
        if 6 <= decimal_hour <= 18:
            # Normalized solar angle (0 at sunrise/sunset, 1 at noon)
            solar_angle = math.sin(math.pi * (decimal_hour - 6) / 12)
            
            # Base radiance with some randomness
            base_radiance = solar_angle * 1000  # Max 1000 W/m²
            radiance = max(0, base_radiance + random.uniform(-100, 100))
            
            # Production based on radiance (typical solar panel efficiency ~20%)
            production = radiance * 0.2 * random.uniform(0.8, 1.2)  # Max ~240 kW
            
            # Performance varies with conditions
            performance = random.uniform(75, 95) if solar_angle > 0.3 else random.uniform(60, 80)
            
            # Availability is usually high during daylight
            availability = random.uniform(95, 100)
            
        else:
            # Night time - no solar production
            radiance = 0
            production = 0
            performance = 0
            availability = random.uniform(90, 100)  # Still some availability
        
        # Add some realistic noise and variations
        production += random.uniform(-10, 10)
        performance += random.uniform(-5, 5)
        availability += random.uniform(-2, 2)
        
        # Ensure values are within reasonable bounds
        production = max(0, min(500, production))  # Cap at 500 kW
        radiance = max(0, min(1400, radiance))     # Cap at 1400 W/m²
        performance = max(0, min(100, performance)) # Cap at 100%
        availability = max(0, min(100, availability)) # Cap at 100%
        
        return {
            'production': {'value': round(production, 2), 'unit': 'kW'},
            'radiance': {'value': round(radiance, 2), 'unit': 'W/m²'},
            'performance': {'value': round(performance, 2), 'unit': '%'},
            'availability': {'value': round(availability, 2), 'unit': '%'},
        }

    def generate_energy_meter_data(self, hour, minute, day_offset):
        """Generate realistic energy meter data in the required JSON format"""
        
        # Convert to decimal hour for calculations
        decimal_hour = hour + minute / 60.0
        
        # Generate timestamp for the data
        current_time = timezone.now() - timedelta(days=day_offset)
        timestamp_str = current_time.replace(hour=hour, minute=minute, second=0, microsecond=0).isoformat()
        
        # Energy consumption pattern (higher during day, lower at night)
        if 6 <= decimal_hour <= 22:
            # Daytime consumption (higher)
            base_consumption = random.uniform(0.5, 2.0)  # 0.5-2.0 kWh per 15min
        else:
            # Nighttime consumption (lower)
            base_consumption = random.uniform(0.0, 0.5)  # 0-0.5 kWh per 15min
        
        # Energy production pattern (solar-like, peaks around noon)
        if 6 <= decimal_hour <= 18:
            # Solar production pattern
            solar_angle = math.sin(math.pi * (decimal_hour - 6) / 12)
            base_production = solar_angle * random.uniform(2.0, 4.0)  # 0-4 kWh per 15min
        else:
            # No production at night
            base_production = 0
        
        # Add some realistic noise
        consumption = max(0, base_consumption + random.uniform(-0.2, 0.2))
        production = max(0, base_production + random.uniform(-0.3, 0.3))
        
        # Calculate daily totals (accumulate over the day)
        # For simplicity, we'll generate realistic daily totals
        daily_consumed = random.uniform(15, 35)  # 15-35 kWh per day
        daily_produced = random.uniform(20, 50)  # 20-50 kWh per day
        
        # Weekly and monthly are typically the same as daily for this example
        # In a real implementation, these would be calculated from historical data
        weekly_consumed = daily_consumed
        weekly_produced = daily_produced
        monthly_consumed = daily_consumed
        monthly_produced = daily_produced
        
        return {
            'timestamp': timestamp_str,
            'Energy_consumed': {
                'unit': 'kWh',
                'value': round(consumption, 2)
            },
            'Energy_produced': {
                'unit': 'kWh',
                'value': round(production, 2)
            },
            'Energy_daily_consumed': {
                'unit': 'kWh',
                'value': round(daily_consumed, 2)
            },
            'Energy_daily_produced': {
                'unit': 'kWh',
                'value': round(daily_produced, 2)
            },
            'Energy_weekly_consumed': {
                'unit': 'kWh',
                'value': round(weekly_consumed, 2)
            },
            'Energy_weekly_produced': {
                'unit': 'kWh',
                'value': round(weekly_produced, 2)
            },
            'Energy_monthly_consumed': {
                'unit': 'kWh',
                'value': round(monthly_consumed, 2)
            },
            'Energy_monthly_produced': {
                'unit': 'kWh',
                'value': round(monthly_produced, 2)
            }
        }
