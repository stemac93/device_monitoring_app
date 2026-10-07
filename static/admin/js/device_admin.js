document.addEventListener('DOMContentLoaded', function () {

    toggleFields();

    document.querySelector('#id_protocol')?.addEventListener('change', toggleFields);

    function toggleFields() {
        const protocolSelect = document.querySelector('#id_protocol');

        const modbusFields = document.querySelectorAll(`
            .form-row.field-slave_id,
            .form-row.field-apply_preset
        `);

        if (!protocolSelect) return;

        const value = protocolSelect.value;
        console.log('Protocol value:', value);

        if (value === 'modbus') {
            modbusFields.forEach(el => el.style.display = '');
        } else if (value === 'dlms') {
            modbusFields.forEach(el => el.style.display = 'none');
        }
        console.log('Found fields:', modbusFields);

        toggleInlineForms(value);       
    }

    function toggleInlineForms(value) {

        console.log('Value:', value);

        const modbusInlines = document.querySelectorAll('.modbus-inline');
        const dlmsInlines = document.querySelectorAll('.dlms-inline');
    
        if (value === 'modbus') {
            modbusInlines.forEach(el => el.classList.remove('hidden'));
            dlmsInlines.forEach(el => el.classList.add('hidden'));
        } else if (value === 'dlms') {
            modbusInlines.forEach(el => el.classList.add('hidden'));
            dlmsInlines.forEach(el => el.classList.remove('hidden'));
        }
    }

    function updateInlineRequired(fieldset, enabled) {
        const inputs = fieldset.querySelectorAll('input, select, textarea');
    
        inputs.forEach(input => {
            if (enabled) {
                input.required = true;
            } else {
                input.required = false;
    
                // Imposta valore di default solo se è vuoto
                if (!input.value) {
                    if (input.tagName === 'INPUT') {
                        input.value = '-1';  // oppure '0', '', a seconda del campo
                    }
                }
            }
        });
    }
});
