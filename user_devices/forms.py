from django import forms
from .models import Device, DlmsMappingVariable
from .presets import preset_choices

class DeviceForm(forms.ModelForm):
    apply_preset = forms.ChoiceField(
        label="Preset",
        required=False,
        help_text="Crea automaticamente blocchi di lettura e variabili per il modello scelto. "
                  "Lascia vuoto per mappare a mano.",
    )

    class Meta:
        model = Device
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['apply_preset'].choices = preset_choices()

    def clean(self):
        cleaned_data = super().clean()
        protocol = cleaned_data.get('protocol')
        
        modbus_fields = ['slave_id']

        if protocol == 'dlms':
            # Optional: remove validation errors for modbus fields
            for field in modbus_fields:
                self._errors.pop(field, None)
                cleaned_data[field] = None  # or a default like 0 or ''

        elif protocol == 'modbus':
            # `is None`/'' e non `not value`: 0 non va trattato come vuoto
            for field in modbus_fields:
                value = cleaned_data.get(field)
                if value is None or value == '':
                    self.add_error(field, f"{field.replace('_', ' ').capitalize()} is required for Modbus.")

            slave_id = cleaned_data.get('slave_id')
            if slave_id is not None and not 1 <= slave_id <= 247:
                self.add_error('slave_id', "Slave ID must be between 1 and 247.")

            gateway = cleaned_data.get('Gateway')
            if gateway is not None and gateway.protocol_mode == 'dlms':
                self.add_error('protocol', "Il gateway è in modalità DLMS: i device Modbus non verrebbero letti.")

        return cleaned_data


#class DlmsMappingVariableForm(forms.ModelForm):
#    class Meta:
#        model = DlmsMappingVariable
#        fields = '__all__'

#    def __init__(self, *args, **kwargs):
#        super().__init__(*args, **kwargs)
#        # Initially hide `days` field if type is clock
#        if self.instance and self.instance.data_type != 'profile':
#            self.fields['days'].widget.attrs['style'] = 'display:none;'
        

class DlmsMappingVariableForm(forms.ModelForm):
    class Meta:
        model = DlmsMappingVariable
        fields = '__all__'

    #def __init__(self, *args, **kwargs):
    #    super().__init__(*args, **kwargs)

    #    # Initially hide `days` field if type is clock
    #    #if self.instance and self.instance.data_type != 'profile':
    #    #    self.fields['days'].widget.attrs['style'] = 'display:none;'

    #    count = self.initial.get('column_count') or self.instance.column_count or 1
    #    meta = self.initial.get('column_metadata') or self.instance.column_metadata or {}

    #    for i in range(count):
    #        self.fields[f'unit_{i}'] = forms.CharField(
    #            required=False,
    #            label=f'Unit for column {i}',
    #            initial=meta.get(str(i), {}).get('unit', '')
    #        )
    #        self.fields[f'conv_{i}'] = forms.FloatField(
    #            required=False,
    #            label=f'Conversion factor for column {i}',
    #            initial=meta.get(str(i), {}).get('factor', 1.0)
    #        )

    #def clean(self):
    #    cleaned_data = super().clean()
    #    count = cleaned_data.get('column_count') or 1
    #    metadata = {}
    #    for i in range(count):
    #        metadata[str(i)] = {
    #            'unit': cleaned_data.pop(f'unit_{i}', ''),
    #            'factor': cleaned_data.pop(f'conv_{i}', 1.0)
    #        }
    #    cleaned_data['column_metadata'] = metadata
    #    return cleaned_data