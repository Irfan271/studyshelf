(function () {
  function syncReason(select) {
    const form = select.closest('form');
    if (!form) return;
    const custom = form.querySelector('[data-custom-reason]');
    if (!custom) return;
    const visible = select.value === 'other';
    custom.hidden = !visible;
    custom.required = visible;
    if (!visible) custom.value = '';
  }
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-reason-select]').forEach(function (select) {
      syncReason(select);
      select.addEventListener('change', function () { syncReason(select); });
    });
  });
})();
