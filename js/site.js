const toggle = document.querySelector(".nav-toggle");
const nav = document.querySelector("#site-nav");
if (toggle && nav) {
  toggle.addEventListener("click", () => {
    const open = nav.classList.toggle("open");
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  });
  nav.querySelectorAll("a").forEach((link) => {
    link.addEventListener("click", () => {
      nav.classList.remove("open");
      toggle.setAttribute("aria-expanded", "false");
    });
  });
}

const detail = document.querySelector("#invest-detail");
document.querySelectorAll(".name[data-investigator]").forEach((button) => {
  button.addEventListener("click", () => {
    const id = button.getAttribute("data-investigator");
    const already = button.getAttribute("aria-expanded") === "true";
    document.querySelectorAll(".name[data-investigator]").forEach((other) => {
      other.setAttribute("aria-expanded", "false");
    });
    detail.querySelectorAll("[data-detail]").forEach((card) => {
      card.hidden = true;
    });
    if (already) {
      detail.hidden = true;
      return;
    }
    button.setAttribute("aria-expanded", "true");
    const card = detail.querySelector(`[data-detail="${id}"]`);
    const plate = document.querySelector(`.plate[data-investigator="${id}"]`);
    if (card) {
      card.hidden = false;
      card.style.setProperty("--accent", plate ? getComputedStyle(plate).getPropertyValue("--accent") : "#159af7");
      detail.hidden = false;
    }
  });
});
