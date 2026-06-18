(function () {
  'use strict';

  function isTestType(value) {
    return value === 'test';
  }

  function setFieldVisible(fieldRow, visible) {
    if (!fieldRow) {
      return;
    }
    fieldRow.style.display = visible ? '' : 'none';
  }

  function setFieldDisabled(fieldRow, disabled) {
    if (!fieldRow) {
      return;
    }
    fieldRow.querySelectorAll('input, select, textarea').forEach(function (input) {
      input.disabled = disabled;
    });
  }

  function toggleTokenForm(form) {
    var typeSelect = form.querySelector('[name="token_type"]');
    if (!typeSelect) {
      return;
    }

    var isTest = isTestType(typeSelect.value);
    var paymentRow = form.querySelector('.field-payment_amount');
    var startsRow = form.querySelector('.field-starts_at');
    var expiresRow = form.querySelector('.field-expires_at');

    setFieldVisible(paymentRow, !isTest);
    setFieldDisabled(startsRow, isTest);
    setFieldDisabled(expiresRow, isTest);

    if (paymentRow) {
      paymentRow.querySelectorAll('input').forEach(function (input) {
        input.required = !isTest;
      });
    }
  }

  function initTokenForms() {
    document.querySelectorAll('#ghostaccesstoken_form, form').forEach(function (form) {
      if (!form.querySelector('[name="token_type"]')) {
        return;
      }
      toggleTokenForm(form);
      form.querySelector('[name="token_type"]').addEventListener('change', function () {
        toggleTokenForm(form);
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initTokenForms);
  } else {
    initTokenForms();
  }
})();
