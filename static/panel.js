/* Общий код панели: настройки, уведомления, счётчик непрочитанных, статус. */
(function () {
    "use strict";

    const DEFAULTS = {sound: true, desktop: false, preview: true, interval: 3};
    const baseTitle = document.title;

    /* ---------- настройки этого браузера ---------- */

    function getSettings() {
        try {
            return Object.assign({}, DEFAULTS, JSON.parse(localStorage.getItem("panelSettings") || "{}"));
        } catch (e) {
            return Object.assign({}, DEFAULTS);
        }
    }

    function saveSettings(patch) {
        const merged = Object.assign(getSettings(), patch);
        try { localStorage.setItem("panelSettings", JSON.stringify(merged)); } catch (e) {}
        return merged;
    }

    /* ---------- оформление всплывашек ---------- */

    const style = document.createElement("style");
    style.textContent = `
        #pnStack { position: fixed; top: 16px; right: 16px; z-index: 1000;
                   display: flex; flex-direction: column; gap: 8px; max-width: 320px; }
        .pn-toast { background: #0b1220; border: 1px solid #334155; border-left: 4px solid #3b82f6;
                    border-radius: 12px; padding: 10px 14px; color: #e5e7eb; cursor: pointer;
                    font: 14px -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
                    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4); }
        .pn-toast.error { border-left-color: #ef4444; }
        .pn-toast b { display: block; margin-bottom: 2px; }
        .pn-toast span { opacity: 0.75; display: block; overflow: hidden;
                         text-overflow: ellipsis; white-space: nowrap; }
    `;
    document.head.appendChild(style);

    function stack() {
        let el = document.getElementById("pnStack");
        if (!el) {
            el = document.createElement("div");
            el.id = "pnStack";
            document.body.appendChild(el);
        }
        return el;
    }

    function showToast(title, body, options) {
        options = options || {};
        const box = document.createElement("div");
        box.className = "pn-toast" + (options.error ? " error" : "");

        const head = document.createElement("b");
        head.textContent = title;
        box.appendChild(head);

        if (body) {
            const line = document.createElement("span");
            line.textContent = body;
            box.appendChild(line);
        }

        const close = () => box.remove();
        box.onclick = () => {
            if (options.url) location.href = options.url;
            close();
        };

        stack().appendChild(box);
        setTimeout(close, options.error ? 5000 : 7000);
    }

    function toast(text, isError) {
        showToast(text, "", {error: isError});
    }

    /* ---------- звук ---------- */

    let audioCtx = null;

    function unlockAudio() {
        if (!audioCtx) {
            const Ctx = window.AudioContext || window.webkitAudioContext;
            if (Ctx) audioCtx = new Ctx();
        }
        if (audioCtx && audioCtx.state === "suspended") audioCtx.resume();
    }
    ["pointerdown", "keydown"].forEach(name =>
        document.addEventListener(name, unlockAudio, {passive: true})
    );

    function beep() {
        try {
            if (!audioCtx) return;
            if (audioCtx.state === "suspended") audioCtx.resume();

            const now = audioCtx.currentTime;
            [[880, 0], [1175, 0.13]].forEach(([freq, delay]) => {
                const osc = audioCtx.createOscillator();
                const gain = audioCtx.createGain();
                osc.type = "sine";
                osc.frequency.value = freq;
                gain.gain.setValueAtTime(0.0001, now + delay);
                gain.gain.exponentialRampToValueAtTime(0.25, now + delay + 0.02);
                gain.gain.exponentialRampToValueAtTime(0.0001, now + delay + 0.25);
                osc.connect(gain);
                gain.connect(audioCtx.destination);
                osc.start(now + delay);
                osc.stop(now + delay + 0.3);
            });
        } catch (e) {}
    }

    /* ---------- уведомления ---------- */

    function notify(item) {
        const settings = getSettings();
        const body = settings.preview ? item.preview : "Новое сообщение";
        const url = item.user_id ? "/chat/" + item.user_id : null;

        showToast(item.name, body, {url: url});

        if (settings.desktop && "Notification" in window
            && Notification.permission === "granted" && document.hidden) {
            try {
                const n = new Notification(item.name, {
                    body: body,
                    tag: "chat-" + item.user_id,
                });
                n.onclick = () => {
                    window.focus();
                    if (url) location.href = url;
                    n.close();
                };
            } catch (e) {}
        }
    }

    async function requestDesktop() {
        if (!("Notification" in window)) return "unsupported";
        if (Notification.permission === "granted") return "granted";
        return await Notification.requestPermission();
    }

    /* ---------- счётчик непрочитанных ---------- */

    function setBadge(count) {
        document.title = (count > 0 ? "(" + count + ") " : "") + baseTitle;

        document.querySelectorAll("[data-unread-badge]").forEach(el => {
            el.textContent = count > 99 ? "99+" : count;
            el.hidden = count <= 0;
        });
    }

    /* ---------- время и статус ---------- */

    function parseUtc(value) {
        return new Date(String(value).replace(" ", "T") + "Z");
    }

    // Telegram не отдаёт ботам «онлайн», поэтому показываем последнюю активность
    function statusText(utc) {
        if (!utc) return {text: "ещё не писал(а)", online: false};

        const date = parseUtc(utc);
        const minutes = Math.floor((Date.now() - date.getTime()) / 60000);

        if (minutes < 1) return {text: "писал(а) только что", online: true};
        if (minutes < 5) return {text: "писал(а) " + minutes + " мин назад", online: true};
        if (minutes < 60) return {text: "писал(а) " + minutes + " мин назад", online: false};

        const hours = Math.floor(minutes / 60);
        if (hours < 24) return {text: "писал(а) " + hours + " ч назад", online: false};

        return {
            text: "писал(а) " + date.toLocaleDateString([], {day: "2-digit", month: "2-digit"}),
            online: false,
        };
    }

    function listTime(utc) {
        if (!utc) return "";
        const date = parseUtc(utc);
        const now = new Date();
        if (date.toDateString() === now.toDateString()) {
            return date.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
        }
        return date.toLocaleDateString([], {day: "2-digit", month: "2-digit"});
    }

    /* ---------- запросы ---------- */

    async function api(url, body) {
        const response = await fetch(url, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(body || {}),
        });

        if (response.status === 401) {
            location.href = "/login";
            return {ok: false};
        }

        try {
            return await response.json();
        } catch (e) {
            return {ok: false, error: "Ошибка сервера"};
        }
    }

    /* ---------- опрос новостей ---------- */

    function start(options) {
        options = options || {};
        let since = -1;

        async function tick() {
            try {
                const url = "/api/updates?since=" + since + (options.chats ? "&chats=1" : "");
                const response = await fetch(url, {cache: "no-store"});

                if (response.status === 401) {
                    location.href = "/login";
                    return;
                }

                const data = await response.json();

                if (since >= 0 && data.new.length) {
                    const visible = document.visibilityState === "visible";

                    // Про диалог, который открыт и виден, не уведомляем
                    const fresh = data.new.filter(
                        m => !(options.currentChat && m.user_id === options.currentChat && visible)
                    );

                    if (fresh.length) {
                        if (getSettings().sound) beep();
                        fresh.slice(-3).forEach(notify);
                    }
                }

                since = Math.max(since, data.latest);
                setBadge(data.unread_total);

                if (options.onUpdate) await options.onUpdate(data);
            } catch (e) {}

            setTimeout(tick, getSettings().interval * 1000);
        }

        tick();
    }

    window.Panel = {
        getSettings, saveSettings, beep, notify, toast, requestDesktop,
        setBadge, statusText, listTime, parseUtc, api, start,
    };
})();
