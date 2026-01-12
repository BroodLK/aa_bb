'use strict';
{
    window.addEventListener('load', function() {
        const $ = django.jQuery;

        // Market transactions toggle
        const marketCheckbox = $('#id_show_market_transactions');
        const marketFieldset = $('.market-transaction-settings-fieldset');

        function toggleMarketFieldset() {
            if (marketCheckbox.is(':checked')) {
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
                customHaulingField.show();
            } else {
                customHaulingField.hide();
            }
        }

        if (haulingCheckbox.length && customHaulingField.length) {
            haulingCheckbox.on('change', toggleCustomHaulingField);
            toggleCustomHaulingField(); // Initial state
        }

        // Add expand/collapse all buttons for sections
        addExpandCollapseButtons();
    });

    function addExpandCollapseButtons() {
        const form = document.querySelector('form');
        if (!form) return;

        // Find all collapsed fieldsets (except market transaction settings)
        const collapsedFieldsets = [];
        document.querySelectorAll('fieldset.collapse').forEach(fs => {
            if (!fs.classList.contains('market-transaction-settings-fieldset')) {
                collapsedFieldsets.push(fs);
            }
        });

        if (collapsedFieldsets.length === 0) return;

        // Create button container
        const buttonContainer = document.createElement('div');
        buttonContainer.style.cssText = 'margin: 20px 0; padding: 10px; background: #f0f8ff; border: 1px solid #b0d4f1; border-radius: 4px;';

        const expandAllBtn = document.createElement('button');
        expandAllBtn.type = 'button';
        expandAllBtn.textContent = '▼ Expand All Sections';
        expandAllBtn.style.cssText = 'margin-right: 10px; padding: 8px 16px; background: #417690; color: white; border: none; border-radius: 4px; cursor: pointer; font-weight: 500;';
        expandAllBtn.addEventListener('click', function() {
            collapsedFieldsets.forEach(fs => {
                if (fs.classList.contains('collapsed')) {
                    const h2 = fs.querySelector('h2');
                    if (h2) h2.click();
                }
            });
        });

        const collapseAllBtn = document.createElement('button');
        collapseAllBtn.type = 'button';
        collapseAllBtn.textContent = '▲ Collapse All Sections';
        collapseAllBtn.style.cssText = 'padding: 8px 16px; background: #6c757d; color: white; border: none; border-radius: 4px; cursor: pointer; font-weight: 500;';
        collapseAllBtn.addEventListener('click', function() {
            collapsedFieldsets.forEach(fs => {
                if (!fs.classList.contains('collapsed')) {
                    const h2 = fs.querySelector('h2');
                    if (h2) h2.click();
                }
            });
        });

        buttonContainer.appendChild(expandAllBtn);
        buttonContainer.appendChild(collapseAllBtn);

        // Insert after the "Core Settings" fieldset
        const coreFieldset = document.querySelector('fieldset');
        if (coreFieldset && coreFieldset.nextSibling) {
            coreFieldset.parentNode.insertBefore(buttonContainer, coreFieldset.nextSibling);
        }

        // Add hover effects
        [expandAllBtn, collapseAllBtn].forEach(btn => {
            btn.addEventListener('mouseenter', function() {
                this.style.opacity = '0.9';
                this.style.transform = 'translateY(-1px)';
                this.style.transition = 'all 0.2s';
            });
            btn.addEventListener('mouseleave', function() {
                this.style.opacity = '1';
                this.style.transform = 'translateY(0)';
            });
        });
    }
}
