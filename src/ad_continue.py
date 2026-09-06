"""React promptly to the ad provider's ordinary finished-ad Continue control."""

# Register this at document creation, substituting JSON literals for the three
# placeholders. The provider owns the countdown and decides when to expose and
# enable its control. This script never changes either condition.
CONTINUE_AD_JS = r"""
(() => {
    const expectedPath = __EPISODE_PATH__;
    const episodeNo = __EPISODE_NO__;
    const bridgeToken = __BRIDGE_TOKEN__;
    const onViewer = () => window.top === window &&
        window.location.origin === 'https://global.novelpia.com' &&
        (window.location.pathname === expectedPath ||
         window.location.pathname === expectedPath + '/');
    if (!onViewer() || window.__piaAdContinueWatcher === bridgeToken) return;
    window.__piaAdContinueWatcher = bridgeToken;

    const checkContinue = () => {
        if (!onViewer()) return;
        const button = document.getElementById('ez-rewarded-continue-button');
        if (!button || button.tagName !== 'BUTTON' || button.__piaAdContinueClicked ||
            button.disabled || button.matches(':disabled') ||
            button.getAttribute('aria-disabled') === 'true' ||
            button.getAttribute('aria-hidden') === 'true' ||
            button.textContent.trim() !== 'Continue' ||
            button.getClientRects().length === 0) return;

        // Geometry also catches hidden ancestors. Check opacity on ancestors
        // because their opacity is not inherited by getComputedStyle(button).
        for (let node = button; node; node = node.parentElement) {
            const style = window.getComputedStyle(node);
            if (style.display === 'none' || style.visibility !== 'visible' ||
                Number(style.opacity) === 0) return;
        }
        const rect = button.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0 || rect.bottom <= 0 || rect.right <= 0 ||
            rect.top >= window.innerHeight || rect.left >= window.innerWidth) return;
        const x = (Math.max(0, rect.left) + Math.min(window.innerWidth, rect.right)) / 2;
        const y = (Math.max(0, rect.top) + Math.min(window.innerHeight, rect.bottom)) / 2;
        const target = document.elementFromPoint(x, y);
        if (target !== button && !button.contains(target)) return;

        const bridge = window.chrome && window.chrome.webview;
        if (!bridge || typeof bridge.postMessage !== 'function') return;
        const clickId = window.crypto && typeof window.crypto.randomUUID === 'function'
            ? window.crypto.randomUUID()
            : Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
        button.__piaAdContinueClicked = true;
        try {
            button.click();
        } catch (_) {
            delete button.__piaAdContinueClicked;
            return;
        }
        // Notify only after dispatching the real click. Server responses remain
        // the separate evidence that the advertisement unlocked the chapter.
        try {
            bridge.postMessage({
                type: 'pia-ad-continue', episode_no: episodeNo,
                token: bridgeToken, click_id: clickId,
            });
        } catch (_) {}
    };

    const observer = new MutationObserver(checkContinue);
    observer.observe(document, {
        childList: true, subtree: true, characterData: true, attributes: true,
        attributeFilter: ['disabled', 'style', 'class', 'hidden', 'aria-disabled', 'aria-hidden'],
    });
    // CSS transitions may finish without another DOM mutation. Do not wait for
    // window.load: unrelated slow ad resources can keep that event pending.
    setInterval(checkContinue, 100);
    checkContinue();
})();
"""
