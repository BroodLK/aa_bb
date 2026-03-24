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

        // Skill injection options toggle
        const spInjectCheckbox = $('#id_sp_inject_notify');
        const spInjectMode = $('#id_sp_inject_detection_mode');
        const spInjectModeField = $('.field-sp_inject_detection_mode');
        const spInjectThresholdField = $('.field-sp_inject_threshold');
        const spInjectRatioField = $('.field-sp_inject_ratio_delta');

        function toggleSpInjectFields() {
            if (!spInjectCheckbox.is(':checked')) {
                spInjectModeField.hide();
                spInjectThresholdField.hide();
                spInjectRatioField.hide();
                return;
            }

            const parentRow = spInjectCheckbox.closest('.form-row');
            spInjectModeField.insertAfter(parentRow);
            spInjectModeField.show();

            if (spInjectMode.val() === 'ratio') {
                spInjectRatioField.insertAfter(spInjectModeField);
                spInjectRatioField.show();
                spInjectThresholdField.hide();
            } else {
                spInjectThresholdField.insertAfter(spInjectModeField);
                spInjectThresholdField.show();
                spInjectRatioField.hide();
            }
        }

        if (spInjectCheckbox.length && spInjectMode.length) {
            spInjectCheckbox.on('change', toggleSpInjectFields);
            spInjectMode.on('change', toggleSpInjectFields);
            toggleSpInjectFields(); // Initial state
        }

        // Manual main corporation override toggle
        const manualMainCorpOverride = $('#id_manual_main_corporation_override');
        const manualMainCorpIdField = $('.field-manual_main_corporation_id');

        function toggleManualMainCorpField() {
            if (manualMainCorpOverride.is(':checked')) {
                manualMainCorpIdField.show();
            } else {
                manualMainCorpIdField.hide();
            }
        }

        if (manualMainCorpOverride.length && manualMainCorpIdField.length) {
            manualMainCorpOverride.on('change', toggleManualMainCorpField);
            toggleManualMainCorpField(); // Initial state
        }

    });
}
