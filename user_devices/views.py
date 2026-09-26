from django.http import HttpResponse
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.shortcuts import render
from django.shortcuts import render, get_object_or_404
from django.db.models import Avg
from .models import Device, Button, Gateway, ComputedVariable, ModbusMappingVariable, DlmsMappingVariable, DeviceData, EnergyData, GatewayData
from django.shortcuts import redirect
from .commands import set_pin_status
from user_devices.helper_funcs import sanitize_variable_name, convert_to_local_time
import json
import logging 
from datetime import datetime, timedelta
from django.utils import timezone
import csv
from io import StringIO

def base_redirect(request):
    if request.user.is_authenticated:
        return redirect('home/')
    else:
        return redirect('login/')

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
logger = logging.getLogger(__name__)


def home_view(request):

    if not request.user.is_authenticated:
        return redirect('login')
    
    user = request.user
    gateways = Gateway.objects.filter(user=user)
    # Get user's gateways
    gateways = Gateway.objects.filter(user=user)
    
    # Handle date selection
    selected_date = request.GET.get('date')
    if selected_date:
        try:
            selected_date = datetime.strptime(selected_date, '%Y-%m-%d').date()
        except ValueError:
            selected_date = None
    
    if not selected_date:
        selected_date = timezone.localdate()  # data locale, non UTC
    
    # Calculate start and end of selected day
    start_of_day = timezone.make_aware(datetime.combine(selected_date, datetime.min.time()))
    end_of_day = timezone.make_aware(datetime.combine(selected_date, datetime.max.time()))
    
    # Get devices through the gateway relationship
    all_devices = Device.objects.filter(Gateway__in=gateways)
    devices = all_devices.filter(is_enabled=True)

    # Variabili da mostrare in homepage
    modbus_vars = ModbusMappingVariable.objects.filter(show_in_homepage=True, device__in=devices)
    dlms_vars = DlmsMappingVariable.objects.filter(show_in_homepage=True, device__in=devices)
    computed_vars = ComputedVariable.objects.filter(show_in_homepage=True, device__in=devices)

    # Add debug logging
    logger.info("==================== HOME VIEW DEBUG INFO ====================")
    logger.info(f"User: {user.username}")
    logger.info(f"User ID: {user.id}")
    logger.info(f"User's gateways: {gateways.count()}")
    logger.info("Gateway details:")
    for gateway in gateways:
        logger.info(f"  Gateway: {gateway.name} ({gateway.ip_address})")
        logger.info(f"  Devices on this gateway:")
        for dev in gateway.devices.all():
            logger.info(f"    - {dev.name} (enabled: {dev.is_enabled})")
    
    logger.info(f"\nTotal devices through gateways: {all_devices.count()}")
    logger.info(f"Enabled devices: {devices.count()}")
    logger.info("=========================================================")
    
    # Separate tables for gateway/plant data and device data
    gateway_rows = []
    device_rows = []
    
    # Show gateway production and consumption data
    for gateway in gateways:
        last_data = GatewayData.objects.filter(Gateway=gateway).order_by('-timestamp').first()
        if last_data:
            for key, value in last_data.data.items():
                gateway_rows.append({
                    'gateway_name': gateway.name,
                    'var_name': key,
                    'value': value.get('value', ''),
                    'unit': value.get('unit', ''),
                    'conversion_factor': '',
                    'timestamp': last_data.timestamp,
                })
    logger.info(f"Gateway rows: {gateway_rows}")

    logger.info(f"Modbus vars: {modbus_vars}")
    logger.info(f"Dlms vars: {dlms_vars}")
    logger.info(f"Computed vars: {computed_vars}")

    # Show device data
    for var in list(modbus_vars) + list(dlms_vars) + list(computed_vars):
        last_data = DeviceData.objects.filter(device_name=var.device).order_by('-timestamp').first()
        logger.info(f"Last data: {last_data}")
        logger.info(f"Var name: {var.var_name}")
        logger.info(f"Last data data: {last_data.data}")
        sanitized_name = sanitize_variable_name(var.var_name)
        if last_data and sanitized_name in last_data.data:
            raw = last_data.data.get(sanitized_name)
            if isinstance(var, DlmsMappingVariable) and isinstance(raw, dict):
                value = raw.get("value", "N/A")
                timestamp_raw = raw.get("timestamp", last_data.timestamp)
                try:
                    # Parse the timestamp and convert to local time
                    parsed_timestamp = datetime.fromisoformat(timestamp_raw)
                    timestamp = convert_to_local_time(parsed_timestamp)
                except Exception:
                    timestamp = convert_to_local_time(last_data.timestamp)
            elif isinstance(raw, dict) and "value" in raw:
                value = raw["value"]
                timestamp = convert_to_local_time(last_data.timestamp)
            else:
                value = raw
                timestamp = convert_to_local_time(last_data.timestamp)

            device_rows.append({
                'device_name': var.device.name,
                'var_name': var.var_name,
                'value': value,
                'unit': var.unit,
                'conversion_factor': var.conversion_factor,
                'timestamp': timestamp,
            })

    # Energy data
    for device in devices:
        last_data = EnergyData.objects.filter(device_name=device).order_by('-timestamp').first()
        if not last_data:
            continue
        energy_data = last_data.data

        def add_energy_row(name, value):
            if isinstance(value, dict):
                val = value.get("value", "N/A")
                val = round(val,2)
            else:
                val = value
            device_rows.append({
                'device_name': device.name,
                'var_name': name.replace("_"," "),
                'value': val,
                'unit': 'kWh',
                'conversion_factor': '',
                'timestamp': last_data.timestamp,
            })

        if device.show_energy and 'Energy' in energy_data:
            add_energy_row('Energy', energy_data['Energy'])

        if device.show_energy_daily:
            for key, val in energy_data.items():
                logger.info(f"Energy daily: {key} - {val}")
                if key.startswith('Energy_daily'):
                    add_energy_row(key, val)

        if device.show_energy_weekly:
            for key, val in energy_data.items():
                if key.startswith('Energy_weekly'):                 
                    add_energy_row(key, val)

        if device.show_energy_monthly:
            for key, val in energy_data.items():
                if key.startswith('Energy_monthly'):
                    add_energy_row(key, val)

    # Generate 24-hour time grid (15-minute intervals)
    def generate_24h_grid():
        grid = []
        for hour in range(24):
            for minute in [0, 15, 30, 45]:
                time_str = f"{hour:02d}:{minute:02d}"
                grid.append(time_str)
        return grid
    
    # Collect chart data for each gateway
    gateway_chart_data = {}
    time_grid = generate_24h_grid()  # Fixed 24-hour grid
    
    for gateway in gateways:
        # Get GatewayData for the selected day
        gateway_data = GatewayData.objects.filter(
            Gateway=gateway,
            timestamp__gte=start_of_day,
            timestamp__lte=end_of_day
        ).order_by('timestamp')
        
        # Create data mapping for the 24-hour grid
        data_map = {}
        for entry in gateway_data:
            # Convert to local time and round to nearest 15-minute interval
            local_time = convert_to_local_time(entry.timestamp)
            hour = local_time.hour
            minute = (local_time.minute // 15) * 15
            time_key = f"{hour:02d}:{minute:02d}"
            
            # Extract data values
            data = entry.data
            data_map[time_key] = {
                'production': data.get('production', {}).get('value', 0),
                'performance': data.get('performance', {}).get('value', 0),
                'availability': data.get('availability', {}).get('value', 0),
                'radiance': data.get('radiance', {}).get('value', 0),
            }
        
        # Generate data arrays for the complete 24-hour grid
        production_data = []
        performance_data = []
        availability_data = []
        radiance_data = []
        
        for time_slot in time_grid:
            if time_slot in data_map:
                production_data.append(data_map[time_slot]['production'])
                performance_data.append(data_map[time_slot]['performance'])
                availability_data.append(data_map[time_slot]['availability'])
                radiance_data.append(data_map[time_slot]['radiance'])
            else:
                # No data for this time slot - use null to show empty
                production_data.append(None)
                performance_data.append(None)
                availability_data.append(None)
                radiance_data.append(None)
        
        gateway_chart_data[gateway.id] = {
            'gateway_name': gateway.name,
            'labels': time_grid,
            'production': production_data,
            'performance': performance_data,
            'availability': availability_data,
            'radiance': radiance_data,
        }

    # Convert gateway chart data to JSON for JavaScript
    gateway_chart_data_json = json.dumps(gateway_chart_data)

    return render(request, 'home.html', {
        'user': user,
        'gateways': gateways,
        'devices': devices,
        'gateway_rows': gateway_rows,
        'device_rows': device_rows,
        'gateway_chart_data': gateway_chart_data,
        'gateway_chart_data_json': gateway_chart_data_json,
        'selected_date': selected_date,
    })


def device_detail_view(request, device_name):
    # Get user's gateways first
    user_gateways = Gateway.objects.filter(user=request.user)
    
    # Find device through gateway relationship
    device = get_object_or_404(
        Device, 
        name=device_name,
        Gateway__in=user_gateways  # Check device belongs to user's gateways
    )

    # Retrieve the buttons for this device
    buttons = Button.objects.filter(Gateway=device.Gateway, show_in_user_page=True)

    # Pass everything to the template
    context = {
        "gateway": device.Gateway,
        "device": device,
        "buttons": buttons,
        "y_label": "",
        "x_data": [],
        "y_data": [],
        "chart_error": "No data configure for this device yet.",
        "data": {}
    }

    # Retrieve last data from the device
    energy_data = EnergyData.objects.filter(device_name=device).order_by('-timestamp').first()
    device_data = DeviceData.objects.filter(device_name=device).order_by('-timestamp').first()
    
    # Only process energy_data if it exists
    if energy_data:
        for key, value in energy_data.data.items():
            
            if key == "Energy_daily_produced":
                context["data"]["Energy_daily_produced"] = value
            if key == "Energy_daily_consumed":
                context["data"]["Energy_daily_consumed"] = value
            if not key.startswith("Energy") and key != "timestamp":
                context["data"][key] = value

    # Only process device_data if it exists
    if device_data:
        # Get ordered variables to maintain admin page ordering
        modbus_vars = device.modbus_variables.all().order_by('order')
        dlms_vars = device.dlms_variables.all().order_by('order')
        computed_vars = device.computed_variables.all().order_by('order')
        
        # Process variables in the order they appear in admin (ordered by 'order' field)
        all_vars = list(modbus_vars) + list(dlms_vars) + list(computed_vars)
        
        for var in all_vars:
            sanitized_name = sanitize_variable_name(var.var_name)
            if sanitized_name in device_data.data:
                value = device_data.data[sanitized_name]
                context["data"][var.var_name] = value

    # Retrieve historic data for chart
    y_variable = ComputedVariable.objects.filter(device=device, show_on_graph=True).first() or \
    ModbusMappingVariable.objects.filter(device=device, show_on_graph=True).first() or \
    DlmsMappingVariable.objects.filter(device=device, show_on_graph=True).first()

    if y_variable:
        logger.info(f"y_variable found: {y_variable}")
        logger.info(f"y_variable name: {y_variable.var_name}")
        logger.info(f"Device protocol: {device.protocol}")

        # Get data from last 24 hours
        from datetime import timedelta
        from django.utils import timezone
        
        # Use timezone-aware datetime
        now = timezone.now()
        twenty_four_hours_ago = now - timedelta(hours=24)
        
        # First, let's check if there's any data at all for this device
        all_data_count = DeviceData.objects.filter(device_name=device).count()
        logger.info(f"Total data records for device {device.name}: {all_data_count}")
        
        if all_data_count > 0:
            # Show the latest timestamp
            latest_data = DeviceData.objects.filter(device_name=device).order_by('-timestamp').first()
            logger.info(f"Latest data timestamp: {latest_data.timestamp}")
            logger.info(f"Current time: {now}")
            logger.info(f"24 hours ago: {twenty_four_hours_ago}")
        
        chart_data = DeviceData.objects.filter(
            device_name=device, 
            timestamp__gte=twenty_four_hours_ago
        ).order_by('timestamp')
        logger.info(f"Chart data count: {len(chart_data)} (from {twenty_four_hours_ago} to {now})")
        
        if chart_data:
            logger.info(f"First chart data entry: {chart_data[0].data}")
            # Convert to list to safely access last element
            chart_data_list = list(chart_data)
            if chart_data_list:
                logger.info(f"Last chart data entry: {chart_data_list[-1].data}")
            # Reassign the list for further processing
            chart_data = chart_data_list
        
        sanitized_name = sanitize_variable_name(y_variable.var_name)
        logger.info(f"Sanitized variable name: {sanitized_name}")
        
        if device.protocol == "dlms":
            timestamps = []
            for entry in chart_data:
                # For DLMS, timestamp is at root level, not inside the variable
                timestamp_str = entry.data.get("timestamp", "")
                if timestamp_str:
                    try:
                        # Parse the timestamp and convert to local time
                        parsed_timestamp = datetime.fromisoformat(timestamp_str)
                        timestamp = parsed_timestamp.strftime("%H:%M")
                        timestamps.append(timestamp)
                    except Exception as e:
                        logger.info(f"Error parsing timestamp '{timestamp_str}': {e}")
                        # Fallback to entry timestamp
                        timestamp = entry.timestamp.strftime("%H:%M")
                        timestamps.append(timestamp)
                else:
                    # Fallback to entry timestamp if no timestamp in data
                    timestamp = entry.timestamp.strftime("%H:%M")
                    timestamps.append(timestamp)      
        elif device.protocol == "modbus":  # Corretto da "modubs" a "modbus"
            timestamps = [
                convert_to_local_time(entry.timestamp).strftime("%H:%M")  # Formato consistente con DLMS
                for entry in chart_data
            ]  
        else:
            timestamps = []
            logger.info(f"Unknown protocol: {device.protocol}")
            
        x_data = timestamps  # Example X values
        y_data = [entry.data.get(sanitized_name, {}).get("value", None) for entry in chart_data]
        
        logger.info(f"X data length: {len(x_data)}")
        logger.info(f"Y data length: {len(y_data)}")
        logger.info(f"X data sample: {x_data[:3] if x_data else 'Empty'}")
        logger.info(f"Y data sample: {y_data[:3] if y_data else 'Empty'}")
        
        # Debug: show what we're extracting for Y data
        logger.info("Y data extraction details:")
        for i, entry in enumerate(chart_data):
            var_data = entry.data.get(sanitized_name, {})
            value = var_data.get("value", None) if isinstance(var_data, dict) else None

        # Assume you have logic to generate x_data and y_data
        context["x_data"] = json.dumps(x_data)
        context["y_data"] = json.dumps(y_data)
        context["y_label"] = y_variable.var_name
        context["chart_error"] = None  # Clear the error
    else:
        logger.info("No y_variable found for chart")

    return render(request, 'device_detail.html', context)

def toggle_button_status(request, button_id):
    button = get_object_or_404(Button, id=button_id, Gateway__user=request.user)
    status = 'on' if not button.is_active else 'off'

    # Use the utility function to toggle the button's state
    success, response = set_pin_status(button.Gateway, button.pin_number, status)
    if success:
        button.is_active = not button.is_active
        button.save()
        messages.success(request, f"Button '{button.label}' updated successfully.")
    else:
        messages.error(request, f"Error updating button: {response}")
    # Redirect back to the referring page
    return redirect(request.META.get('HTTP_REFERER', '/'))

def download_data(request):
    """Handle data download requests with date range validation"""
    if not request.user.is_authenticated:
        return redirect('login')
    
    if request.method != 'POST':
        messages.error(request, 'Invalid request method.')
        return redirect('home')
    
    # Get form data
    data_type = request.POST.get('data_type')
    start_date_str = request.POST.get('start_date')
    end_date_str = request.POST.get('end_date')
    
    # Validate required fields
    if not all([data_type, start_date_str, end_date_str]):
        messages.error(request, 'All fields are required.')
        return redirect('home')
    
    try:
        # Parse dates
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    except ValueError:
        messages.error(request, 'Invalid date format.')
        return redirect('home')
    
    # Validate date range (max 30 days)
    if (end_date - start_date).days > 30:
        messages.error(request, 'Date range cannot exceed 30 days.')
        return redirect('home')
    
    if start_date > end_date:
        messages.error(request, 'Start date must be before end date.')
        return redirect('home')
    
    # Get user's gateways for security check
    user_gateways = Gateway.objects.filter(user=request.user)
    
    # Determine data source and get data
    if data_type.startswith('gateway_'):
        gateway_id = int(data_type.split('_')[1])
        try:
            gateway = Gateway.objects.get(id=gateway_id, user=request.user)
        except Gateway.DoesNotExist:
            messages.error(request, 'Gateway not found or access denied.')
            return redirect('home')
        
        # Get gateway data
        start_datetime = timezone.make_aware(datetime.combine(start_date, datetime.min.time()))
        end_datetime = timezone.make_aware(datetime.combine(end_date, datetime.max.time()))
        
        data_queryset = GatewayData.objects.filter(
            Gateway=gateway,
            timestamp__gte=start_datetime,
            timestamp__lte=end_datetime
        ).order_by('timestamp')
        
        source_name = gateway.name
        data_type_name = 'Gateway'
        
    elif data_type.startswith('device_'):
        device_id = int(data_type.split('_')[1])
        try:
            device = Device.objects.get(id=device_id, Gateway__in=user_gateways)
        except Device.DoesNotExist:
            messages.error(request, 'Device not found or access denied.')
            return redirect('home')
        
        # Get device data
        start_datetime = timezone.make_aware(datetime.combine(start_date, datetime.min.time()))
        end_datetime = timezone.make_aware(datetime.combine(end_date, datetime.max.time()))
        
        data_queryset = DeviceData.objects.filter(
            device_name=device,
            timestamp__gte=start_datetime,
            timestamp__lte=end_datetime
        ).order_by('timestamp')
        
        source_name = device.name
        data_type_name = 'Device'
        
    else:
        messages.error(request, 'Invalid data type selected.')
        return redirect('home')
    
    # Check if data exists
    if not data_queryset.exists():
        messages.error(request, f'No data found for {source_name} in the selected date range.')
        return redirect('home')
    
    # Generate CSV
    response = HttpResponse(content_type='text/csv')
    filename = f"{data_type_name}_{source_name}_{start_date_str}_to_{end_date_str}.csv"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    
    writer = csv.writer(response)
    
    # Write header
    writer.writerow(['Timestamp', 'Variable Name', 'Value', 'Unit'])
    
    # Write data
    for entry in data_queryset:
        timestamp = convert_to_local_time(entry.timestamp).strftime('%Y-%m-%d %H:%M:%S')
        
        if hasattr(entry, 'data') and entry.data:
            for var_name, var_data in entry.data.items():
                if isinstance(var_data, dict) and 'value' in var_data:
                    value = var_data['value']
                    unit = var_data.get('unit', '')
                else:
                    value = var_data
                    unit = ''
                
                writer.writerow([timestamp, var_name, value, unit])
    
    return response