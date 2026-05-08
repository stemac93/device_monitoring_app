from django.contrib import admin, messages
from .models import User, Gateway, Device, DeviceVariable, ModbusMappingVariable, DlmsMappingVariable, ComputedVariable, Button, DeviceData, EnergyData, GatewayData, GatewayMqttCredentials
from .commands import set_pin_status
from django.utils.html import format_html
from django.urls import reverse
from adminsortable2.admin import  SortableAdminBase, SortableStackedInline
from .forms import DeviceForm, DlmsMappingVariableForm

# Importa il modulo admin_mqtt per registrare GatewayMqttCredentialsAdmin
# e le funzioni di provisioning. L'import esegue il @admin.register lì dentro.
from . import admin_mqtt  # noqa: F401

class GatewayAdmin(admin.ModelAdmin):
    list_display = ('ip_address', 'name', 'protocol_mode', 'mqtt_status', 'get_users')
    list_filter = ('user', 'ip_address', 'protocol_mode')
    search_fields = ('user', 'ip_address', 'name')
    filter_horizontal = ('user',)
    exclude = ('performance', 'availability', 'production', 'consumption')
    readonly_fields = ('mqtt_bundle_link',)
    actions = ['regenerate_mqtt_credentials']

    fieldsets = (
        (None, {
            'fields': ('name', 'ip_address', 'protocol_mode', 'user', 'performance_factor'),
            'description': (
                'Imposta protocol_mode in base a come il server riceve i dati: '
                'MQTT (gateway pubblica via Telegraf), Modbus TCP diretto (server fa polling '
                'via VPN+mbusd, modalità legacy/debug), DLMS (smart meter).'
            ),
        }),
        ('MQTT', {
            'fields': ('mqtt_bundle_link',),
            'description': (
                'Visibile solo se protocol_mode=MQTT. Le credenziali MQTT vengono create '
                'automaticamente al primo salvataggio del gateway in modalità MQTT.'
            ),
            'classes': ('mqtt-section',),  # CSS hook se serve
        }),
        ('SSH (opzionale)', {
            'fields': ('ssh_username', 'ssh_password'),
            'classes': ('collapse',),
            'description': 'Solo per modalità DLMS o per debug remoto.',
        }),
    )

    def get_users(self, obj):
        return ", ".join([user.username for user in obj.user.all()])
    get_users.short_description = 'Users'

    def mqtt_status(self, obj):
        if obj.protocol_mode != 'mqtt':
            return format_html('<span style="color:#888">N/A</span>')
        cred = getattr(obj, 'mqtt_credentials', None)
        if not cred:
            return format_html('<span style="color:#888">—</span>')
        if cred.password_revealed:
            return format_html('<span style="color:#080">✓ provisioned</span>')
        return format_html('<strong style="color:#a30">⚠ password da scaricare</strong>')
    mqtt_status.short_description = 'MQTT'

    def mqtt_bundle_link(self, obj):
        if not obj.pk:
            return format_html('<em>Salva il gateway per generare le credenziali MQTT.</em>')
        if obj.protocol_mode != 'mqtt':
            return format_html(
                '<em>Sezione disponibile solo quando protocol_mode = MQTT. '
                'Cambia la modalità e salva per attivare il provisioning.</em>'
            )
        cred = getattr(obj, 'mqtt_credentials', None)
        if not cred:
            return format_html('<em>Credenziali non ancora generate.</em>')
        url = reverse('gateway_mqtt_bundle', args=[obj.pk])
        if cred.password_revealed:
            return format_html(
                '<em>Il bundle è già stato scaricato. Per ri-scaricarlo usa "Rigenera credenziali MQTT" '
                'tra le azioni della lista Gateway.</em>'
            )
        return format_html(
            '<a class="button" style="background:#28a745;color:white;padding:6px 12px;'
            'border-radius:4px;text-decoration:none;" href="{}">⬇ Scarica bundle gateway</a>'
            '<br><small>Una volta scaricato, la password non sarà più recuperabile.</small>',
            url,
        )
    mqtt_bundle_link.short_description = 'Bundle Telegraf'

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)

        # Sync users to each device under the gateway
        for device in obj.devices.all():
            for user in obj.user.all():
                device.user.add(user)

                # Also sync users to device data
                for data in device.device_data.all():
                    data.user.add(user)

                # Also sync users to energy data
                for energy_data in device.energy_data.all():
                    energy_data.user.add(user)

    @admin.action(description="Rigenera credenziali MQTT (invalida quelle esistenti)")
    def regenerate_mqtt_credentials(self, request, queryset):
        """Cancella le vecchie credenziali, ne genera di nuove, riscrive Mosquitto.

        Salta i gateway che non sono in modalità mqtt.
        """
        from .signals import _generate_password, _gateway_username, _sync_mosquitto_state
        from .mqtt import admin_client

        n_ok = 0
        n_err = 0
        n_skipped = 0
        for gw in queryset:
            if gw.protocol_mode != 'mqtt':
                n_skipped += 1
                continue

            username = _gateway_username(gw.pk)
            new_password = _generate_password()

            cred, _ = GatewayMqttCredentials.objects.update_or_create(
                gateway=gw,
                defaults={
                    'username': username,
                    'password_plaintext': new_password,
                    'password_revealed': False,
                },
            )
            try:
                admin_client.add_user(username=username, password=new_password)
                n_ok += 1
            except admin_client.MosquittoAdminError as e:
                n_err += 1
                self.message_user(
                    request,
                    f"Errore broker per gateway {gw.pk}: {e}",
                    messages.ERROR,
                )

        # Una sola sync ACL alla fine
        try:
            _sync_mosquitto_state()
        except Exception as e:
            self.message_user(request, f"Errore sync ACL: {e}", messages.WARNING)

        if n_ok:
            self.message_user(
                request,
                f"{n_ok} credenziali rigenerate. Apri il dettaglio del gateway per scaricare il nuovo bundle.",
                messages.SUCCESS,
            )
        if n_err:
            self.message_user(request, f"{n_err} con errori.", messages.WARNING)
        if n_skipped:
            self.message_user(
                request,
                f"{n_skipped} gateway saltati (protocol_mode != mqtt).",
                messages.INFO,
            )

class MemoryMappingInlineModbus(SortableStackedInline, admin.StackedInline):
    model = ModbusMappingVariable
    extra = 0
    fields = ('var_name', 'address', 'unit', 'conversion_factor', 'bit_length','endianness', 'is_signed', 'show_on_graph', 'show_in_homepage') 
    sortable = 'order'
    classes = ['modbus-inline']
    
class MemoryMappingInlineDlms(SortableStackedInline, admin.StackedInline):
    model = DlmsMappingVariable
    form = DlmsMappingVariableForm
    extra = 0
    fields = (
        'var_name', 'obis_code', 'unit', 'conversion_factor','column_idx','show_on_graph', 'show_in_homepage'
    )
    sortable = 'order'
    classes = ['dlms-inline']
    
    #class Media:
    #    js = ('admin/js/dlms_variable_inline.js',)
    
class ComputedVariableInline(SortableStackedInline, admin.StackedInline):
    model = ComputedVariable
    extra = 0
    fields = ('var_name', 'unit', 'formula','show_on_graph', 'show_in_homepage')
    sortable = 'order'


class DeviceAdmin(SortableAdminBase, admin.ModelAdmin):
    form = DeviceForm
    list_display = ('name','is_enabled', 'get_users','Gateway__name', 'Gateway__ip_address', 'protocol')
    list_filter = ('user','Gateway', 'is_enabled')
    search_fields = ('user','Gateway')
    #inlines = [MemoryMappingInlineModbus, ComputedVariableInline]
    actions = ['clone_device']
    readonly_fields = ('get_users',)
    exclude = ('user',)  # Hide the actual editable ManyToMany field
    fieldsets = (
        (None, {
            'fields': ( 'is_enabled', 'name', 'Gateway', 'protocol', 'slave_id','register_type', 'start_address', 'word_count', 'port')
        }),
        ('Energy Display Options', {
            'fields': ('show_energy','show_energy_daily', 'show_energy_weekly', 'show_energy_monthly'),
        }),
    )
    
    class Media:
        js = ('admin/js/device_admin.js',)
    
    def get_users(self, obj):
        return ", ".join([user.username for user in obj.user.all()])
    get_users.short_description = 'Users'
    
    def get_inline_instances(self, request, obj=None):
        return [
            MemoryMappingInlineModbus(self.model, self.admin_site),
            MemoryMappingInlineDlms(self.model, self.admin_site),
            ComputedVariableInline(self.model, self.admin_site),
        ]


    def clone_device(self, request, queryset):
        """
        Clone selected devices with all their related variables and settings.
        """
        cloned_count = 0
        
        for device in queryset:
            # Generate unique name for the cloned device
            base_name = f"{device.name} Copy"
            new_name = base_name
            counter = 1
            
            # Ensure the name is unique
            while Device.objects.filter(name=new_name).exists():
                new_name = f"{base_name} {counter}"
                counter += 1
            
            # Create the cloned device
            cloned_device = Device.objects.create(
                name=new_name,
                Gateway=device.Gateway,
                is_enabled=False,  # Start disabled for safety
                slave_id=device.slave_id,
                start_address=device.start_address,
                bytes_count=device.bytes_count,
                port=device.port,
                availability=device.availability,
                show_energy=device.show_energy,
                show_energy_daily=device.show_energy_daily,
                show_energy_weekly=device.show_energy_weekly,
                show_energy_monthly=device.show_energy_monthly,
                daily_production=device.daily_production,
                daily_consumption=device.daily_consumption,
                register_type=device.register_type,
                protocol=device.protocol
            )
            
            # Copy user relationships
            for user in device.user.all():
                cloned_device.user.add(user)
            
            # Clone ModbusMappingVariable instances
            for modbus_var in device.modbus_variables.all():
                ModbusMappingVariable.objects.create(
                    device=cloned_device,
                    variable_type=modbus_var.variable_type,
                    var_name=modbus_var.var_name,
                    unit=modbus_var.unit,
                    show_on_graph=modbus_var.show_on_graph,
                    show_in_homepage=modbus_var.show_in_homepage,
                    order=modbus_var.order,
                    address=modbus_var.address,
                    conversion_factor=modbus_var.conversion_factor,
                    bit_length=modbus_var.bit_length,
                    is_signed=modbus_var.is_signed
                )
            
            # Clone DlmsMappingVariable instances
            for dlms_var in device.dlms_variables.all():
                DlmsMappingVariable.objects.create(
                    device=cloned_device,
                    variable_type=dlms_var.variable_type,
                    var_name=dlms_var.var_name,
                    unit=dlms_var.unit,
                    show_on_graph=dlms_var.show_on_graph,
                    show_in_homepage=dlms_var.show_in_homepage,
                    order=dlms_var.order,
                    conversion_factor=dlms_var.conversion_factor,
                    obis_code=dlms_var.obis_code,
                    column_idx=dlms_var.column_idx
                )
            
            # Clone ComputedVariable instances
            for computed_var in device.computed_variables.all():
                ComputedVariable.objects.create(
                    device=cloned_device,
                    variable_type=computed_var.variable_type,
                    var_name=computed_var.var_name,
                    unit=computed_var.unit,
                    show_on_graph=computed_var.show_on_graph,
                    show_in_homepage=computed_var.show_in_homepage,
                    order=computed_var.order,
                    formula=computed_var.formula
                )
            
            cloned_count += 1
        
        if cloned_count == 1:
            self.message_user(request, f"Successfully cloned 1 device.")
        else:
            self.message_user(request, f"Successfully cloned {cloned_count} devices.")
    
    clone_device.short_description = "Clone selected devices"

class GatewayDataAdmin(admin.ModelAdmin):
    list_display = ('Gateway', 'timestamp','get_users')
    search_fields = ('user__username', 'Gateway__ip_address')
    list_filter = ('Gateway__ip_address', 'timestamp')
    readonly_fields = ('get_users', 'Gateway', 'timestamp','data')
    fieldsets = (
        (None, {'fields': ('get_users', 'Gateway', 'data')}),
        ('Timestamps', {'fields': ('timestamp',)}),
    )

    def get_users(self, obj):
        return ", ".join([user.username for user in obj.user.all()])
    get_users.short_description = 'Users'

class DeviceDataAdmin(admin.ModelAdmin):
    list_display = ('device_name', 'timestamp','get_users', 'Gateway__ip_address')
    search_fields = ('user__username', 'Gateway__ip_address', 'name__device_name')
    list_filter = ('Gateway__ip_address', 'device_name', 'timestamp')
    readonly_fields = ('get_users', 'device_name', 'Gateway', 'timestamp','data')
    fieldsets = (
        (None, {'fields': ('get_users', 'Gateway', 'device_name', 'data')}),
        ('Timestamps', {'fields': ('timestamp',)}),
    )

    def get_users(self, obj):
        return ", ".join([user.username for user in obj.user.all()])
    get_users.short_description = 'Users'

class EnergyDataAdmin(admin.ModelAdmin):
    list_display = ('device_name', 'timestamp','get_users', 'Gateway__ip_address')
    search_fields = ('user__username', 'Gateway__ip_address', 'device_name__name')
    list_filter = ('Gateway__ip_address', 'device_name', 'timestamp')
    readonly_fields = ('get_users', 'device_name', 'Gateway', 'timestamp','data')
    fieldsets = (
        (None, {'fields': ('get_users', 'Gateway', 'device_name', 'data')}),
        ('Timestamps', {'fields': ('timestamp',)}),
    )

    def get_users(self, obj):
        return ", ".join([user.username for user in obj.user.all()])
    get_users.short_description = 'Users'


class ButtonAdmin(admin.ModelAdmin):
    list_display = ('label','Gateway__name', 'Gateway__ip_address', 'pin_number', 'is_active', 'show_in_user_page', 'toggle_button_link')
    list_filter = ('Gateway', 'show_in_user_page')

    def get_readonly_fields(self, request, obj=None):
        # Make 'is_active' readonly
        readonly_fields = super().get_readonly_fields(request, obj)
        return readonly_fields + ("is_active",)

    def toggle_button_link(self, obj):
        """
        Display a custom toggle button in the admin interface.
        """
        url = reverse('admin:toggle_button_action', args=[obj.pk])
        return format_html(
            '<a class="button" href="{}">Toggle</a>',
            url
        )

    toggle_button_link.short_description = "Toggle Button"
    toggle_button_link.allow_tags = True

    def get_urls(self):
        """
        Add a custom URL for the toggle button action.
        """
        from django.urls import path

        urls = super().get_urls()
        custom_urls = [
            path(
                '<int:pk>/toggle/',
                self.admin_site.admin_view(self.toggle_button_action),
                name='toggle_button_action',
            ),
        ]
        return custom_urls + urls

    def toggle_button_action(self, request, pk):
        """
        Handle the toggle button action.
        """
        button = Button.objects.get(pk=pk)
        status = 'on' if not button.is_active else 'off'

        # Call the SSH function
        success, response = set_pin_status(
            device=button.Gateway,
            pin=button.pin_number,
            status=status
        )

        if success:
            button.is_active = not button.is_active
            button.save()
            self.message_user(request, f"Button '{button.label}' toggled successfully.")
        else:
            self.message_user(request, f"Failed to toggle button '{button.label}': {response}", level="error")

        # Redirect back to the button list
        from django.shortcuts import redirect
        return redirect('admin:user_devices_button_changelist')
    
# Register models in logical groups for better visual organization

# Device Settings Group
admin.site.register(Gateway, GatewayAdmin)
admin.site.register(Device, DeviceAdmin)
admin.site.register(Button, ButtonAdmin)

# Data Management Group 
admin.site.register(DeviceData, DeviceDataAdmin)
admin.site.register(EnergyData, EnergyDataAdmin)
admin.site.register(GatewayData, GatewayDataAdmin)

admin.site.site_header = 'Site Administration'

# Custom admin configuration to control model ordering
from django.contrib.admin import AdminSite
from django.contrib.admin.apps import AdminConfig

class CustomAdminSite(AdminSite):
    def index(self, request, extra_context=None):
        """
        Override the admin index to control model ordering
        """
        app_dict = self._build_app_dict(request)
        
        # Define the desired order for models within the user_devices app
        desired_order = [
            'gateway',
            'device', 
            'button',
            'devicedata',
            'energydata',
            'gatewaydata'
        ]
        
        # Reorder the models in the user_devices app
        if 'user_devices' in app_dict:
            app_dict['user_devices']['models'].sort(
                key=lambda x: desired_order.index(x['object_name'].lower()) 
                if x['object_name'].lower() in desired_order 
                else 999
            )
        
        context = dict(
            self.each_context(request),
            title=self.index_title,
            app_list=list(app_dict.values()),
        )
        context.update(extra_context or {})
        
        from django.shortcuts import render
        return render(request, 'admin/index.html', context)

# Replace the default admin site
admin.site.__class__ = CustomAdminSite