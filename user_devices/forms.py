from django import forms
from .models import Device, DlmsMappingVariable

class DeviceForm(forms.ModelForm):
    class Meta:
        model = Device
        fields = '__all__'

    def clean(self):
        cleaned_data = super().clean()
        protocol = cleaned_data.get('protocol')
        
        modbus_fields = ['slave_id', 'register_type', 'start_address', 'word_count']

        if protocol == 'dlms':
            # Optional: remove validation errors for modbus fields
            for field in modbus_fields:
                self._errors.pop(field, None)
                cleaned_data[field] = None  # or a default like 0 or ''

        elif protocol == 'modbus':
            # `is None`/'' e non `not value`: 0 è un indirizzo di partenza valido
            for field in modbus_fields:
                value = cleaned_data.get(field)
                if value is None or value == '':
                    self.add_error(field, f"{field.replace('_', ' ').capitalize()} is required for Modbus.")

            slave_id = cleaned_data.get('slave_id')
            if slave_id is not None and not 1 <= slave_id <= 247:
                self.add_error('slave_id', "Slave ID must be between 1 and 247.")

            start_address = cleaned_data.get('start_address')
            if start_address not in (None, ''):
                try:
                    int(str(start_address), 16)
                except ValueError:
                    self.add_error('start_address', "Start address must be hexadecimal (e.g. 0x0280).")

            word_count = cleaned_data.get('word_count')
            if word_count is not None and word_count <= 0:
                self.add_error('word_count', "Word count must be greater than 0.")

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