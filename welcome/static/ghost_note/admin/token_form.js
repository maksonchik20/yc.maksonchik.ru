(function () {
  'use strict';

  function getJQuery() {
    return window.jQuery || (window.django && window.django.jQuery);
  }

  function isTestType(value) {
    return value === 'test';
  }

  function paymentFieldForTokenTypeSelect(select) {
    if (!select || !select.form) {
      return null;
    }
    var name = select.name || '';
    if (name === 'token_type') {
      return select.form.querySelector('#id_payment_amount');
    }
    if (name.indexOf('-token_type') !== -1) {
      var prefix = name.slice(0, name.length - '-token_type'.length);
      return select.form.querySelector('#id_' + prefix + '-payment_amount');
    }
    return null;
  }

  function paymentContainer(paymentInput) {
    if (!paymentInput) {
      return null;
    }
    return (
      paymentInput.closest('.form-group.field-payment_amount')
      || paymentInput.closest('.form-group')
      || paymentInput.closest('td.field-payment_amount')
      || paymentInput.closest('td')
    );
  }

  function dateContainersForSelect(select) {
    if (!select || !select.form) {
      return [];
    }
    var name = select.name || '';
    var containers = [];
    if (name === 'token_type') {
      ['starts_at', 'expires_at'].forEach(function (fieldName) {
        var input = select.form.querySelector('#id_' + fieldName + '_0, #id_' + fieldName);
        var container = input && paymentContainer(input);
        if (container) {
          containers.push(container);
        }
      });
      return containers;
    }
    if (name.indexOf('-token_type') !== -1) {
      return containers;
    }
    return containers;
  }

  function setContainerVisible(container, visible) {
    if (!container) {
      return;
    }
    container.style.display = visible ? '' : 'none';
  }

  function setInputsDisabled(container, disabled) {
    if (!container) {
      return;
    }
    container.querySelectorAll('input, select, textarea').forEach(function (input) {
      input.disabled = disabled;
    });
  }

  function toggleTokenTypeSelect(select) {
    var isTest = isTestType(select.value);
    var paymentInput = paymentFieldForTokenTypeSelect(select);
    var paymentBox = paymentContainer(paymentInput);

    if (paymentBox) {
      setContainerVisible(paymentBox, !isTest);
    }
    if (paymentInput) {
      paymentInput.required = !isTest;
      paymentInput.disabled = isTest;
      if (isTest) {
        paymentInput.value = '';
      }
    }

    dateContainersForSelect(select).forEach(function (container) {
      setInputsDisabled(container, isTest);
    });
  }

  function initAllTokenTypeSelects(root) {
    var scope = root || document;
    scope.querySelectorAll('select[name="token_type"], select[name$="-token_type"]').forEach(function (select) {
      toggleTokenTypeSelect(select);
    });
  }

  function onTokenTypeChange(event) {
    var target = event.target;
    if (!target || target.tagName !== 'SELECT') {
      return;
    }
    var name = target.name || '';
    if (name !== 'token_type' && name.indexOf('-token_type') === -1) {
      return;
    }
    toggleTokenTypeSelect(target);
  }

  function bindFormsetAdded() {
    var $ = getJQuery();
    if (!$ || !window.django || !window.django.jQuery) {
      return;
    }
    window.django.jQuery(document).on('formset:added', function (event, row) {
      if (row && row.length) {
        initAllTokenTypeSelects(row[0]);
        return;
      }
      initAllTokenTypeSelects(document);
    });
  }

  function init() {
    initAllTokenTypeSelects(document);
    document.addEventListener('change', onTokenTypeChange, true);
    bindFormsetAdded();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
