const root = document.documentElement;
const toggle = document.querySelector("#theme-toggle");
const preferenceKey = "postcardscene.control-theme";

function applyTheme(theme) {
  root.dataset.bsTheme = theme;
  toggle.setAttribute("aria-pressed", String(theme === "light"));
}

// Storage may be disabled by browser policy; the switch still works for this page.
let theme = "dark";
try {
  if (localStorage.getItem(preferenceKey) === "light") {
    theme = "light";
  }
} catch {
  // Keep the dark default when a saved preference is unavailable.
}
applyTheme(theme);
toggle.hidden = false;

toggle.addEventListener("click", () => {
  const nextTheme = root.dataset.bsTheme === "dark" ? "light" : "dark";
  applyTheme(nextTheme);
  try {
    localStorage.setItem(preferenceKey, nextTheme);
  } catch {
    // Persistence is optional; applying the theme does not require server state.
  }
});
