// First-run setup wizard client (#978). Submits the setup form via fetch (so the request
// always carries an Origin the loopback CSRF check can verify) and renders per-field errors.
(function () {
  "use strict";
  var form = document.getElementById("setup-form");
  var submit = document.getElementById("setup-submit");
  var generalError = document.getElementById("setup-error");
  if (!form) return;

  var FIELDS = ["projects_root", "host", "port", "password", "confirm"];

  function clearErrors() {
    generalError.hidden = true;
    generalError.textContent = "";
    FIELDS.forEach(function (name) {
      var input = document.getElementById(name);
      if (input) {
        input.classList.remove("is-invalid");
        input.removeAttribute("aria-invalid");
      }
      var slot = form.querySelector('[data-error="' + name + '"]');
      if (slot) slot.textContent = "";
    });
  }

  function showFieldErrors(errors) {
    var first = null;
    Object.keys(errors).forEach(function (name) {
      var input = document.getElementById(name);
      if (input) {
        input.classList.add("is-invalid");
        input.setAttribute("aria-invalid", "true");
        if (!first) first = input;
      }
      var slot = form.querySelector('[data-error="' + name + '"]');
      if (slot) slot.textContent = errors[name];
    });
    // Move focus to the first invalid field so its aria-describedby error is announced.
    if (first) first.focus();
  }

  function showGeneral(message) {
    generalError.textContent = message;
    generalError.hidden = false;
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    clearErrors();
    submit.disabled = true;
    var payload = {
      projects_root: document.getElementById("projects_root").value,
      host: document.getElementById("host").value,
      port: document.getElementById("port").value,
      password: document.getElementById("password").value,
      confirm: document.getElementById("confirm").value,
    };
    fetch("/setup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (data) {
          return { status: res.status, data: data };
        });
      })
      .then(function (r) {
        if (r.status === 200 && r.data.ok) {
          form.hidden = true;
          var done = document.getElementById("setup-done");
          if (done) done.hidden = false;
          var link = document.getElementById("setup-url");
          if (link && r.data.url) {
            link.textContent = r.data.url;
            // A wildcard bind has no single host, so _display_url returns a "<this-host>"
            // placeholder — render it as plain text, never a link that navigates to "#".
            if (r.data.url.indexOf("<") === -1) link.setAttribute("href", r.data.url);
            else link.removeAttribute("href");
          }
          return;
        }
        submit.disabled = false;
        if (r.data.errors) {
          showFieldErrors(r.data.errors);
        } else {
          showGeneral(r.data.detail || "Setup failed (" + r.status + ").");
        }
      })
      .catch(function () {
        submit.disabled = false;
        showGeneral("Could not reach the server — please try again.");
      });
  });
})();
