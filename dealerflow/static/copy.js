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
