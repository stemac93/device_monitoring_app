# test_data_generator.py
import os
import sys
import django
import random
from datetime import datetime, timezone, timedelta

# Add the parent directory (project root) to Python path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

# Set up Django environment
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "energy_monitoring.settings")
django.setup()

from user_devices.models import Device, DeviceData, Gateway, User
from django.db.models import Q

def generate_test_data():
    """Generate test data for visualizing energy metrics"""
    
    # 1. Get or create a test gateway
    gateway, _ = Gateway.objects.get_or_create(
        name="Test Gateway",
        ip_address="127.0.0.1",
        defaults={
            'ssh_username': 'test',
            'ssh_password': 'test'
        }
    )
    
    # 2. Get or create a test device
    device, _ = Device.objects.get_or_create(
        name="Test Energy Device",
        Gateway=gateway,
        defaults={
            'slave_id': 1,
            'port': 502
        }
    )
    
    # 3. Assign the device to a user (get first user or create one)
    user = User.objects.first()
    if not user:
        user = User.objects.create_user(username='testuser', password='testpass')
    
    device.user.add(user)
    gateway.user.add(user)
    
    # 4. Generate data for the last 48 hours with 15-minute intervals
    now = datetime.now(datetime.timezone.utc)
    
    # Delete existing data for clean test
    DeviceData.objects.filter(device_name=device).delete()
    
    # Create a continuous time series of data
    total_energy = 0
    total_produced = 0
    total_consumed = 0
    
    for i in range(192):  # 48 hours × 4 measurements per hour
        # Alternate between consumption and production
        timestamp = now - timedelta(minutes=15 * (192-i))
        
        # Realistic power values (consumption during day, production during some hours)
        hour_of_day = timestamp.hour
        
        # Simulate solar panel: production during daylight, consumption at night
        if 8 <= hour_of_day <= 18:  # Daylight hours
            # More production during midday
            midday_factor = 1 - abs(hour_of_day - 13) / 5
            power_value = random.uniform(-2000, -500) * midday_factor
        else:  # Night hours - only consumption
            power_value = random.uniform(100, 800)
            
        # Add some randomness
        power_value += random.uniform(-100, 100)
        
        # Calculate energy change for this 15-minute period
        energy_change = power_value * 900  # 15 minutes = 900 seconds
        
        # Update running totals
        total_energy += energy_change
        if power_value < 0:
            total_produced += abs(energy_change)
        else:
            total_consumed += energy_change
        
        # Calculate period-specific values (last day, week, month)
        # These would normally be calculated by the compute_energy function
        time_diff = (now - timestamp).total_seconds()
        
        is_within_day = time_diff <= 86400  # 24 hours in seconds
        is_within_week = time_diff <= 604800  # 7 days in seconds
        is_within_month = time_diff <= 2592000  # 30 days in seconds
        
        # Conversion factor from J to kWh
        conv_factor_to_kwh = 3.6 * 10**6
        
        # Create the data entry
        data = {
            'P': {'value': power_value, 'unit': 'W'},
            'Voltage': {'value': random.uniform(220, 240), 'unit': 'V'},
            'Current': {'value': power_value / 230, 'unit': 'A'},
            'Energy': {'value': total_energy / conv_factor_to_kwh, 'unit': 'kWh'},
            'Energy_produced': {'value': total_produced / conv_factor_to_kwh, 'unit': 'kWh'},
            'Energy_consumed': {'value': total_consumed / conv_factor_to_kwh, 'unit': 'kWh'},
            'Energy_daily_produced': {'value': (total_produced if is_within_day else 0) / conv_factor_to_kwh, 'unit': 'kWh'},
            'Energy_daily_consumed': {'value': (total_consumed if is_within_day else 0) / conv_factor_to_kwh, 'unit': 'kWh'},
            'Energy_weekly_produced': {'value': (total_produced if is_within_week else 0) / conv_factor_to_kwh, 'unit': 'kWh'},
            'Energy_weekly_consumed': {'value': (total_consumed if is_within_week else 0) / conv_factor_to_kwh, 'unit': 'kWh'},
            'Energy_monthly_produced': {'value': (total_produced if is_within_month else 0) / conv_factor_to_kwh, 'unit': 'kWh'},
            'Energy_monthly_consumed': {'value': (total_consumed if is_within_month else 0) / conv_factor_to_kwh, 'unit': 'kWh'},
        }
        
        # Create DeviceData record
        device_data = DeviceData.objects.create(
            Gateway=gateway,
            device_name=device,
            data=data,
            timestamp=timestamp
        )
        
        # Link to user
        device_data.user.add(user)
        
        # Print progress occasionally
        if i % 20 == 0:
            print(f"Created data point {i}/{192}, timestamp: {timestamp}")
    
    print(f"\nTest data generation complete!")
    print(f"Created {DeviceData.objects.filter(device_name=device).count()} data points")
    print(f"Device: {device.name}")
    print(f"Gateway: {gateway.name} ({gateway.ip_address})")
    print(f"User: {user.username}")
    print(f"\nLogin with username: {user.username}, password: testpass")
    print(f"Visit http://localhost:8000/ and select the device")

if __name__ == "__main__":
    generate_test_data()