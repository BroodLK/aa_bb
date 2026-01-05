document.addEventListener('DOMContentLoaded', function() {
    const ticketTypeSelect = document.querySelector('#id_ticket_type');

    // Top-level fields
    const webhookRow = document.querySelector('.field-hr_forum_webhook');
    const channelIdRow = document.querySelector('.field-Forum_Channel_ID');

    // Fieldsets
    // Note: Django admin fieldsets don't always have easy-to-target IDs unless we add them,
    // but we can find them by their headers.
    const fieldsets = document.querySelectorAll('fieldset');
    let privateChannelFieldset = null;

    fieldsets.forEach(fs => {
        const h2 = fs.querySelector('h2');
        if (h2 && h2.textContent.includes('Private Channel Settings (Bot)')) {
            privateChannelFieldset = fs;
        }
    });

    if (ticketTypeSelect) {
        function updateVisibility() {
            const val = ticketTypeSelect.value;

            // Webhook row (Public Forum Threads)
            if (webhookRow) {
                webhookRow.style.display = (val === 'forum_thread') ? 'block' : 'none';
            }

            // Channel ID row (Private Threads)
            if (channelIdRow) {
                channelIdRow.style.display = (val === 'private_thread') ? 'block' : 'none';
            }

            // Private Channel fieldset
            if (privateChannelFieldset) {
                privateChannelFieldset.style.display = (val === 'private_channel') ? 'block' : 'none';
            }
        }

        ticketTypeSelect.addEventListener('change', updateVisibility);
        updateVisibility();
    }
});
