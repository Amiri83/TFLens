// Small form helpers for the settings pages, as an external script so that the
// page CSP (script-src 'self') needs no inline handlers:
//   <form data-confirm="...">         ask before submitting (delete buttons)
//   <select data-autosubmit>          submit the form on change
//   <input type=radio data-show-group="g" value="v"> with <fieldset data-group="g"
//   data-mode="v w">                  show only the fieldsets of the checked value
(function () {
  "use strict";
  document.addEventListener("submit", function (ev) {
    var msg = ev.target.getAttribute && ev.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) ev.preventDefault();
  });
  document.addEventListener("change", function (ev) {
    if (ev.target.matches("select[data-autosubmit]")) ev.target.form.submit();
    if (ev.target.matches("[data-show-group]")) sync(ev.target.getAttribute("data-show-group"));
  });

  function sync(group) {
    var checked = document.querySelector('[data-show-group="' + group + '"]:checked');
    var value = checked ? checked.value : "";
    document.querySelectorAll('fieldset[data-group="' + group + '"]').forEach(function (fs) {
      fs.hidden = fs.getAttribute("data-mode").split(" ").indexOf(value) < 0;
    });
  }

  function init() {
    var groups = {};
    document.querySelectorAll("[data-show-group]").forEach(function (el) {
      groups[el.getAttribute("data-show-group")] = true;
    });
    Object.keys(groups).forEach(sync);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
