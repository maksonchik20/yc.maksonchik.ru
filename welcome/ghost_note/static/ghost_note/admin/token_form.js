(function () {
  'use strict';

  function waitForDjangoJQuery(callback) {
    if (window.django && django.jQuery) {
      callback(django.jQuery);
      return;
    }
    window.setTimeout(function () {
      waitForDjangoJQuery(callback);
    }, 50);
  }

  waitForDjangoJQuery(function ($) {
    var FORM_SELECTOR = '#ghostaccesstoken_form';

    function fieldGroup(fieldName) {
      return $(FORM_SELECTOR + ' .form-group.field-' + fieldName);
    }

    function toggleTokenTypeFields() {
      var $form = $(FORM_SELECTOR);
      if (!$form.length) {
        return;
      }

      var isTest = $form.find('#id_token_type').val() === 'test';
      var $paymentGroup = fieldGroup('payment_amount');
      var $paymentInput = $form.find('#id_payment_amount');

      if (isTest) {
        $paymentGroup.addClass('d-none');
        $paymentInput.prop('required', false).prop('disabled', true).val('');
      } else {
        $paymentGroup.removeClass('d-none');
        $paymentInput.prop('required', true).prop('disabled', false);
      }

      fieldGroup('starts_at')
        .add(fieldGroup('expires_at'))
        .find('input, select')
        .prop('disabled', isTest);
    }

    function bindTokenTypeToggle() {
      var $form = $(FORM_SELECTOR);
      if (!$form.length) {
        return;
      }

      var $tokenType = $form.find('#id_token_type');
      toggleTokenTypeFields();
      $tokenType.off('.ghostTokenType');
      $tokenType.on('change.ghostTokenType select2:select.ghostTokenType', toggleTokenTypeFields);
    }

    $(bindTokenTypeToggle);
  });
})();
