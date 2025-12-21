'use strict';
{
    window.addEventListener('load', function() {
        const $ = django.jQuery;
        const checkbox = $('#id_show_market_transactions');
        const fieldset = $('.market-transaction-settings-fieldset');

        function toggleFieldset() {
            if (checkbox.is(':checked')) {
                fieldset.show();
            } else {
                fieldset.hide();
            }
        }

        if (checkbox.length && fieldset.length) {
            checkbox.on('change', toggleFieldset);
            toggleFieldset(); // Initial state
        }
    });
}
