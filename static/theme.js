/* Тема оформления: применяется сразу (подключается в <head>), хранится в localStorage */
(function () {
    var THEMES = ["dark", "purple", "light"];
    var KEY = "theme";
    var root = document.documentElement;

    function saved() {
        try {
            var t = localStorage.getItem(KEY);
            return THEMES.indexOf(t) >= 0 ? t : "dark";
        } catch (e) { return "dark"; }
    }

    function mark(theme) {
        var nodes = document.querySelectorAll("[data-theme-set]");
        for (var i = 0; i < nodes.length; i++) {
            nodes[i].classList.toggle("on", nodes[i].getAttribute("data-theme-set") === theme);
        }
    }

    function apply(theme) {
        root.setAttribute("data-theme", theme);
        mark(theme);
    }

    window.setTheme = function (theme) {
        if (THEMES.indexOf(theme) < 0) return;
        try { localStorage.setItem(KEY, theme); } catch (e) {}
        apply(theme);
    };

    apply(saved());

    document.addEventListener("DOMContentLoaded", function () {
        mark(saved());
        document.addEventListener("click", function (e) {
            var el = e.target.closest && e.target.closest("[data-theme-set]");
            if (el) window.setTheme(el.getAttribute("data-theme-set"));
        });
    });

    // Синхронизация между вкладками
    window.addEventListener("storage", function (e) {
        if (e.key === KEY) apply(saved());
    });
})();
