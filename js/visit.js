(function () {
  var path = location.pathname || "/";
  var ref = document.referrer || "";
  var base = "/v/hit?js=1&path=" + encodeURIComponent(path) + "&ref=" + encodeURIComponent(ref);

  function send(human) {
    var url = base + (human ? "&human=1" : "");
    if (window.fetch) {
      fetch(url, { method: "GET", credentials: "omit", keepalive: true }).catch(function () {});
      return;
    }
    var image = new Image();
    image.src = url;
  }

  send(false);
  var confirmed = false;
  function confirmHuman() {
    if (confirmed) return;
    confirmed = true;
    send(true);
  }
  ["pointerdown", "keydown", "scroll"].forEach(function (name) {
    window.addEventListener(name, confirmHuman, { once: true, passive: true });
  });
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") confirmHuman();
  });
})();
