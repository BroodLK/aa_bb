'use strict';
{
    window.addEventListener('load', function() {
        const $ = django.jQuery;

        // Market transactions toggle
        const marketCheckbox = $('#id_show_market_transactions');
        const marketFieldset = $('.market-transaction-settings-fieldset');

        function toggleMarketFieldset() {
            if (marketCheckbox.is(':checked')) {
                const parentRow = marketCheckbox.closest('.form-row');
                marketFieldset.insertAfter(parentRow);
                marketFieldset.show();
            } else {
                marketFieldset.hide();
            }
        }

        if (marketCheckbox.length && marketFieldset.length) {
            marketCheckbox.on('change', toggleMarketFieldset);
            toggleMarketFieldset(); // Initial state
        }

        // Hauling corps exclusion toggle
        const haulingCheckbox = $('#id_exclude_hauling_corps_from_courier');
        const customHaulingField = $('.field-custom_hauling_corps');

        function toggleCustomHaulingField() {
            if (haulingCheckbox.is(':checked')) {
                const parentRow = haulingCheckbox.closest('.form-row');
                customHaulingField.insertAfter(parentRow);
                customHaulingField.show();
            } else {
                customHaulingField.hide();
            }
        }

        if (haulingCheckbox.length && customHaulingField.length) {
            haulingCheckbox.on('change', toggleCustomHaulingField);
            toggleCustomHaulingField(); // Initial state
        }

    });
}
