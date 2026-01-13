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

        // Ensure all compliance fieldsets are expanded and not collapsible
        const complianceFieldsets = document.querySelectorAll('.compliance-check-fieldset');
        complianceFieldsets.forEach(fs => {
            fs.classList.remove('collapsed');
            fs.classList.remove('collapse');
        });
    });
}
