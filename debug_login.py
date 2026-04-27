"""
Debug script: Opens a webview to global.novelpia.com, lets you log in,
then dumps EVERYTHING from the browser to help find where the auth token lives.

Usage:  python debug_login.py
        Log in with Google, then press Enter in the terminal when you see
        "Welcome! You're now signed in." on the page.
"""
import json
import os
import sys
import time

LOGIN_URL = "https://global.novelpia.com/"

def main():
    try:
        from pythonnet import load
        load()
    except Exception:
        pass

    try:
        import webview
    except ImportError:
        print("ERROR: pywebview not installed. Run: pip install pywebview")
        sys.exit(1)

    print("=" * 60)
    print("  Novelpia Global Login Debug Tool")
    print("=" * 60)
    print()
    print("1. A browser window will open to global.novelpia.com")
    print("2. Click 'Login with Google' and complete the login")
    print("3. Wait until you see 'Welcome! You're now signed in.'")
    print("4. DO NOT close the browser window")
    print("5. Come back here and press ENTER")
    print()

    window = None
    dump_done = False

    def run_debug(_window=None):
        nonlocal dump_done
        # Wait for user to signal they're logged in
        time.sleep(2)
        print("[ready] Browser is open. Log in, then come back here and press ENTER...")

        # We can't use input() in the webview thread, so we poll for a file signal
        signal_path = os.path.join(os.path.dirname(__file__), ".debug_signal")
        # Clean up old signal
        try:
            os.remove(signal_path)
        except Exception:
            pass

        # Write signal file from main would be complex, so let's just wait
        # and poll periodically
        print("[info] Will start dumping in 5 seconds... (log in first!)")
        print("[info] After login, dumps will run every 10 seconds for 3 minutes.")
        print()
        time.sleep(5)

        dump_dir = os.path.join(os.path.dirname(__file__), "output", "logs")
        os.makedirs(dump_dir, exist_ok=True)

        for round_num in range(18):  # 18 rounds × 10s = 3 minutes
            print(f"\n{'='*60}")
            print(f"  DUMP ROUND {round_num + 1}/18")
            print(f"{'='*60}")

            # 1. Dump ALL cookies (including HttpOnly via webview API)
            print("\n--- COOKIES (webview API - includes HttpOnly) ---")
            all_cookies = {}
            try:
                raw = window.get_cookies()
                if raw:
                    for cookie in raw:
                        name = getattr(cookie, "name", "")
                        value = getattr(cookie, "value", "")
                        domain = getattr(cookie, "domain", "")
                        httponly = getattr(cookie, "httponly", "?")
                        secure = getattr(cookie, "secure", "?")
                        path = getattr(cookie, "path", "?")
                        if name:
                            all_cookies[name] = value
                            print(f"  {name} = {value[:80]}{'...' if len(value) > 80 else ''}")
                            print(f"    domain={domain} httponly={httponly} secure={secure} path={path}")
                    print(f"\n  Total cookies: {len(all_cookies)}")
                else:
                    print("  (no cookies from webview API)")
            except Exception as e:
                print(f"  Error reading cookies: {e}")

            # 2. Dump document.cookie (non-HttpOnly only)
            print("\n--- document.cookie (JS-accessible only) ---")
            try:
                js_cookies = window.evaluate_js("document.cookie") or ""
                if js_cookies:
                    for part in js_cookies.split(";"):
                        part = part.strip()
                        if part:
                            print(f"  {part[:100]}")
                else:
                    print("  (empty)")
            except Exception as e:
                print(f"  Error: {e}")

            # 3. Filter auth-relevant cookies
            print("\n--- AUTH-RELEVANT COOKIES ---")
            auth_keywords = ["login", "auth", "token", "user", "tkey", "session", "key", "member"]
            for name, value in all_cookies.items():
                if any(kw in name.lower() for kw in auth_keywords):
                    print(f"  *** {name} = {value[:120]}")

            # 4. Try fetch /v1/login/me FROM the browser
            print("\n--- BROWSER FETCH: /v1/login/me ---")
            try:
                window.evaluate_js(r"""
                    window.__dbg_me_result = null;
                    window.__dbg_me_error = null;
                    window.__dbg_me_done = false;
                    fetch('https://api-global.novelpia.com/v1/login/me', {
                        method: 'GET',
                        credentials: 'include',
                        headers: {
                            'accept': 'application/json',
                            'origin': 'https://global.novelpia.com',
                            'referer': 'https://global.novelpia.com/'
                        }
                    })
                    .then(function(r) { return r.text(); })
                    .then(function(text) {
                        window.__dbg_me_result = text;
                        window.__dbg_me_done = true;
                    })
                    .catch(function(e) {
                        window.__dbg_me_error = e.message || String(e);
                        window.__dbg_me_done = true;
                    });
                """)
                for _ in range(20):
                    time.sleep(0.5)
                    done = window.evaluate_js("window.__dbg_me_done")
                    if done:
                        break
                error = window.evaluate_js("window.__dbg_me_error")
                if error:
                    print(f"  FETCH ERROR: {error}")
                else:
                    raw = window.evaluate_js("window.__dbg_me_result") or ""
                    print(f"  Response ({len(raw)} chars):")
                    print(f"  {raw[:500]}")
                    try:
                        data = json.loads(raw)
                        print(f"\n  statusCode: {data.get('statusCode')}")
                        print(f"  errmsg: {data.get('errmsg')}")
                        result = data.get("result", {})
                        if isinstance(result, dict):
                            print(f"  result keys: {list(result.keys())}")
                            login_obj = result.get("login")
                            if isinstance(login_obj, dict):
                                print(f"  result.login keys: {list(login_obj.keys())}")
                                loginat = login_obj.get("LOGINAT")
                                if loginat:
                                    print(f"\n  *** FOUND LOGINAT: {loginat[:60]}...")
                            loginat = result.get("LOGINAT")
                            if loginat:
                                print(f"\n  *** FOUND result.LOGINAT: {loginat[:60]}...")
                    except Exception:
                        pass
            except Exception as e:
                print(f"  Error: {e}")

            # 5. Try fetch /v1/login/refresh FROM the browser
            print("\n--- BROWSER FETCH: /v1/login/refresh ---")
            try:
                window.evaluate_js(r"""
                    window.__dbg_ref_result = null;
                    window.__dbg_ref_done = false;
                    fetch('https://api-global.novelpia.com/v1/login/refresh', {
                        method: 'GET',
                        credentials: 'include',
                        headers: {
                            'accept': 'application/json',
                            'origin': 'https://global.novelpia.com',
                            'referer': 'https://global.novelpia.com/'
                        }
                    })
                    .then(function(r) { return r.text(); })
                    .then(function(text) {
                        window.__dbg_ref_result = text;
                        window.__dbg_ref_done = true;
                    })
                    .catch(function(e) {
                        window.__dbg_ref_result = 'ERROR: ' + (e.message || e);
                        window.__dbg_ref_done = true;
                    });
                """)
                for _ in range(20):
                    time.sleep(0.5)
                    done = window.evaluate_js("window.__dbg_ref_done")
                    if done:
                        break
                raw = window.evaluate_js("window.__dbg_ref_result") or ""
                print(f"  Response: {raw[:500]}")
            except Exception as e:
                print(f"  Error: {e}")

            # 6. Check current URL
            print("\n--- CURRENT URL ---")
            try:
                url = window.get_current_url()
                print(f"  {url}")
            except Exception as e:
                print(f"  Error: {e}")

            # 7. Check if user appears logged in on the page
            print("\n--- PAGE LOGIN STATE ---")
            try:
                is_logged = window.evaluate_js(r"""
                    (function() {
                        var result = {};
                        // Check for user avatar/menu that appears when logged in
                        result.bodyText = document.body ? document.body.innerText.substring(0, 300) : '(no body)';
                        // Check for common auth indicators
                        try {
                            result.nuxtExists = !!window.__NUXT__;
                            result.nuxtAppExists = !!window.__nuxt_app__;
                            result.piniaExists = !!(window.__pinia && window.__pinia._s);
                        } catch(e) {}
                        // List all window properties starting with __ or $
                        try {
                            result.specialProps = Object.keys(window).filter(function(k) {
                                return k.indexOf('__') === 0 || k.indexOf('$') === 0;
                            });
                        } catch(e) {}
                        return JSON.stringify(result);
                    })()
                """)
                if is_logged:
                    data = json.loads(is_logged)
                    print(f"  __NUXT__: {data.get('nuxtExists')}")
                    print(f"  __nuxt_app__: {data.get('nuxtAppExists')}")
                    print(f"  __pinia: {data.get('piniaExists')}")
                    print(f"  Special window props: {data.get('specialProps', [])}")
                    body_text = data.get('bodyText', '')
                    if 'sign' in body_text.lower() or 'welcome' in body_text.lower() or 'logged' in body_text.lower():
                        print(f"  Page text (login-related): {body_text[:200]}")
            except Exception as e:
                print(f"  Error: {e}")

            # Save full dump to file
            dump_file = os.path.join(dump_dir, f"debug_dump_round_{round_num + 1}.json")
            try:
                with open(dump_file, "w", encoding="utf-8") as f:
                    json.dump({
                        "round": round_num + 1,
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "cookies": all_cookies,
                    }, f, indent=2)
            except Exception:
                pass

            # Check if we found it
            if any("LOGINAT" in str(v) for v in all_cookies.values()):
                print("\n\n  *** LOGINAT FOUND IN COOKIES! ***")
                break

            print(f"\n[info] Next dump in 10 seconds... (round {round_num + 1}/18)")
            time.sleep(10)

        dump_done = True
        print("\n\nDebug complete. You can close the browser window now.")
        print("Check output/logs/ for saved dumps.")

    try:
        import ctypes
        _sw = ctypes.windll.user32.GetSystemMetrics(0)
        _sh = ctypes.windll.user32.GetSystemMetrics(1)
    except Exception:
        _sw, _sh = 1200, 900

    window = webview.create_window(
        "Novelpia Global — DEBUG LOGIN",
        LOGIN_URL,
        width=int(_sw * 0.6),
        height=int(_sh * 0.7),
    )
    webview.start(run_debug, window, debug=False)

    print("\nWindow closed.")


if __name__ == "__main__":
    main()
