'use strict';
{
    document.addEventListener('DOMContentLoaded', function() {
        const ticketTypeSelect = document.querySelector('#id_ticket_type');

        // Top-level fields
        const webhookRow = document.querySelector('.field-hr_forum_webhook');
        const channelIdRow = document.querySelector('.field-Forum_Channel_ID');

        // Find fieldsets by their headers
        const fieldsets = document.querySelectorAll('fieldset');
        let privateChannelFieldset = null;

        fieldsets.forEach(fs => {
            const h2 = fs.querySelector('h2');
            if (h2 && h2.textContent.includes('Private Channel Settings')) {
                privateChannelFieldset = fs;
            }
        });

        // Show/hide elements based on ticket type
        function updateVisibility() {
            if (!ticketTypeSelect) return;

            const val = ticketTypeSelect.value;

            // Webhook row (Public Forum Threads)
            if (webhookRow) {
                webhookRow.style.display = (val === 'forum_thread') ? '' : 'none';
            }

            // Channel ID row (Private Threads in channel or forum)
            if (channelIdRow) {
                channelIdRow.style.display = (val === 'private_thread' || val === 'forum_thread') ? '' : 'none';
            }

            // Private Channel fieldset (Private ticket channels)
            if (privateChannelFieldset) {
                privateChannelFieldset.style.display = (val === 'private_channel') ? '' : 'none';
            }
        }

        if (ticketTypeSelect) {
            ticketTypeSelect.addEventListener('change', updateVisibility);
            updateVisibility(); // Initial state
        }

        // Add expand/collapse all buttons for compliance fieldsets
        addExpandCollapseButtons();
    });

    function addExpandCollapseButtons() {
        // Find the form
        const form = document.querySelector('form');
        if (!form) return;

        // Find all compliance check fieldsets
        const complianceFieldsets = document.querySelectorAll('.compliance-check-fieldset');
        if (complianceFieldsets.length === 0) return;

        // Create button container
        const buttonContainer = document.createElement('div');
        buttonContainer.style.cssText = 'margin: 20px 0; padding: 10px; background: #f8f8f8; border: 1px solid #ddd; border-radius: 4px;';

        const expandAllBtn = document.createElement('button');
        expandAllBtn.type = 'button';
        expandAllBtn.textContent = '▼ Expand All Compliance Checks';
        expandAllBtn.style.cssText = 'margin-right: 10px; padding: 8px 16px; background: #417690; color: white; border: none; border-radius: 4px; cursor: pointer; font-weight: 500;';
        expandAllBtn.addEventListener('click', function() {
            complianceFieldsets.forEach(fs => {
                if (fs.classList.contains('collapsed')) {
                    const h2 = fs.querySelector('h2');
                    if (h2) h2.click();
                }
            });
        });

        const collapseAllBtn = document.createElement('button');
        collapseAllBtn.type = 'button';
        collapseAllBtn.textContent = '▲ Collapse All Compliance Checks';
        collapseAllBtn.style.cssText = 'padding: 8px 16px; background: #6c757d; color: white; border: none; border-radius: 4px; cursor: pointer; font-weight: 500;';
        collapseAllBtn.addEventListener('click', function() {
            complianceFieldsets.forEach(fs => {
                if (!fs.classList.contains('collapsed')) {
                    const h2 = fs.querySelector('h2');
                    if (h2) h2.click();
                }
            });
        });

        buttonContainer.appendChild(expandAllBtn);
        buttonContainer.appendChild(collapseAllBtn);

        // Insert before the first compliance fieldset
        if (complianceFieldsets[0]) {
            complianceFieldsets[0].parentNode.insertBefore(buttonContainer, complianceFieldsets[0]);
        }

        // Add hover effects
        [expandAllBtn, collapseAllBtn].forEach(btn => {
            btn.addEventListener('mouseenter', function() {
                this.style.opacity = '0.9';
            });
            btn.addEventListener('mouseleave', function() {
                this.style.opacity = '1';
            });
        });
    }
}
