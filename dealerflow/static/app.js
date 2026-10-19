// Small progressive enhancements, loaded as a file so the Content-Security-Policy can forbid inline script.
document.addEventListener("submit", function (e) {
  var msg = e.target.getAttribute("data-confirm");
  if (msg && !window.confirm(msg)) e.preventDefault();
});
document.addEventListener("click", function (e) {
  var sel = e.target.getAttribute && e.target.getAttribute("data-select-all");
  if (sel) document.querySelectorAll(sel).forEach(function (c) { c.checked = e.target.checked; });
});
// Copy-to-clipboard for elements with data-copy (used by the Marketplace publishing package).
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  e.preventDefault();
  navigator.clipboard.writeText(btn.getAttribute("data-copy")).then(() => {
    const old = btn.textContent;
    btn.textContent = "Copied";
    setTimeout(() => (btn.textContent = old), 1200);
  });
});
