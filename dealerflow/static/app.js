// Small progressive enhancements, loaded as a file so the Content-Security-Policy can forbid inline script.
document.addEventListener("submit", function (e) {
  var msg = e.target.getAttribute("data-confirm");
  if (msg && !window.confirm(msg)) e.preventDefault();
});
document.addEventListener("click", function (e) {
  var sel = e.target.getAttribute && e.target.getAttribute("data-select-all");
  if (sel) document.querySelectorAll(sel).forEach(function (c) { c.checked = e.target.checked; });
});
