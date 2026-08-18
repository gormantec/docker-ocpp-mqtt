const IOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
const STANDALONE = window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;

const createInstallBanner = () => {
  const wrap = document.createElement("div");
  wrap.className = "pwa-install-banner";
  wrap.innerHTML = `
    <div class="pwa-install-copy">
      <strong>Install OCPP Bridge</strong>
      <span>Open faster from your home screen.</span>
    </div>
    <div class="pwa-install-actions">
      <button class="btn btn-primary pwa-install-btn" type="button">Install</button>
      <button class="btn btn-secondary pwa-dismiss-btn" type="button" aria-label="Dismiss install hint">Later</button>
    </div>
  `;
  document.body.appendChild(wrap);
  return wrap;
};

export function initPwaInstall() {
  if (!("serviceWorker" in navigator) || STANDALONE) return;

  const dismissKey = "ocpp-pwa-install-dismissed";
  if (localStorage.getItem(dismissKey) === "1") return;

  let deferredPrompt = null;
  let banner = null;

  const ensureBanner = () => {
    if (!banner) banner = createInstallBanner();
    return banner;
  };

  const dismiss = () => {
    if (!banner) return;
    banner.remove();
    banner = null;
    localStorage.setItem(dismissKey, "1");
  };

  const wireButtons = () => {
    const host = ensureBanner();
    const installBtn = host.querySelector(".pwa-install-btn");
    const dismissBtn = host.querySelector(".pwa-dismiss-btn");

    installBtn.onclick = async () => {
      if (deferredPrompt) {
        deferredPrompt.prompt();
        await deferredPrompt.userChoice;
        deferredPrompt = null;
        dismiss();
        return;
      }

      if (IOS) {
        alert("On iPhone: tap Share, then Add to Home Screen.");
        dismiss();
      }
    };

    dismissBtn.onclick = dismiss;
  };

  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    deferredPrompt = event;
    wireButtons();
  });

  if (IOS) {
    wireButtons();
  }

  navigator.serviceWorker.register(`${import.meta.env.BASE_URL}sw.js`).catch(() => {});
}
