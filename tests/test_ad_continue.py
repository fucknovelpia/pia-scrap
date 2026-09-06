import json
import shutil
import subprocess
import unittest

from src.ad_continue import CONTINUE_AD_JS


_HARNESS = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
function fixture() {
    const state = {button: null, clicks: [], messages: [], order: [], observers: [],
                   intervals: [], covered: false, throwClick: false, ids: 0};
    const visibleStyle = () => ({display:'block', visibility:'visible', opacity:'1'});
    function makeButton(name = 'provider') {
        const button = {
            name, tagName:'BUTTON', textContent:'Continue', disabled:false, parentElement:null,
            style:visibleStyle(), attributes:{}, rectangles:[1],
            rect:{left:10, top:10, right:110, bottom:50, width:100, height:40},
            matches:() => false, getAttribute:key => button.attributes[key] || null,
            getClientRects:() => button.rectangles,
            getBoundingClientRect:() => button.rect,
            contains:target => target && target.owner === button,
            click:() => {
                if (state.throwClick) throw new Error('not dispatched');
                state.clicks.push(name); state.order.push('click');
            }
        };
        return button;
    }
    const window = {
        location:{origin:'https://global.novelpia.com', pathname:'/viewer/670403'},
        innerWidth:1000, innerHeight:780,
        getComputedStyle:node => node.style,
        crypto:{randomUUID:() => 'click-' + (++state.ids)},
        chrome:{webview:{postMessage:message => {
            state.messages.push(message); state.order.push('message');
        }}},
        addEventListener:() => {throw new Error('Must not wait for page load');}
    };
    window.top = window;
    const document = {
        readyState:'loading',
        getElementById:id => id === 'ez-rewarded-continue-button' ? state.button : null,
        elementFromPoint:() => state.covered ? {} : state.button,
        addEventListener:() => {throw new Error('Must not wait for document ready');}
    };
    class MutationObserver {
        constructor(callback) {this.callback = callback; state.observers.push(this);}
        observe(target, options) {this.target = target; this.options = options;}
    }
    const context = vm.createContext({window, document, MutationObserver,
        setInterval:(callback, milliseconds) => {
            state.intervals.push({callback, milliseconds}); return state.intervals.length;
        }
    });
    return {
        state, window, document, makeButton,
        run:() => vm.runInContext(input.bootstrap, context),
        mutate:() => state.observers.forEach(observer => observer.callback([])),
        tick:() => state.intervals.forEach(interval => interval.callback())
    };
}
const result = new Function('fixture', input.scenario)(fixture);
process.stdout.write(JSON.stringify(result));
"""


@unittest.skipUnless(shutil.which("node"), "Node is required for the document-start script tests")
class ContinueScriptTests(unittest.TestCase):
    def scenario(self, source):
        script = (CONTINUE_AD_JS
                  .replace("__EPISODE_PATH__", json.dumps("/viewer/670403"))
                  .replace("__EPISODE_NO__", "670403")
                  .replace("__BRIDGE_TOKEN__", json.dumps("per-window-bridge-token")))
        completed = subprocess.run(
            [shutil.which("node"), "-e", _HARNESS],
            input=json.dumps({"bootstrap": script, "scenario": source}),
            capture_output=True, text=True, check=True, timeout=10,
        )
        return json.loads(completed.stdout)

    def test_insertion_is_clicked_and_reported_before_page_load(self):
        result = self.scenario("""
            const env = fixture(); env.run();
            env.state.button = env.makeButton(); env.mutate();
            return {readyState:env.document.readyState, clicks:env.state.clicks,
                    order:env.state.order, messages:env.state.messages};
        """)
        self.assertEqual(result["readyState"], "loading")
        self.assertEqual(result["clicks"], ["provider"])
        self.assertEqual(result["order"], ["click", "message"])
        self.assertEqual(result["messages"], [{
            "type": "pia-ad-continue", "episode_no": 670403,
            "token": "per-window-bridge-token", "click_id": "click-1",
        }])

    def test_countdown_and_disabled_control_are_respected_until_provider_updates(self):
        result = self.scenario("""
            const env = fixture(); const button = env.makeButton();
            button.textContent = 'Continue in 10'; button.disabled = true;
            env.state.button = button; env.run(); env.tick();
            const early = env.state.clicks.length;
            button.textContent = 'Continue'; env.mutate();
            const stillDisabled = env.state.clicks.length;
            button.disabled = false; env.mutate();
            return {early, stillDisabled, clicks:env.state.clicks.length,
                    text:button.textContent, disabled:button.disabled};
        """)
        self.assertEqual(result, {
            "early": 0, "stillDisabled": 0, "clicks": 1, "text": "Continue", "disabled": False,
        })

    def test_attribute_changes_and_css_transition_fallback_finish_promptly(self):
        result = self.scenario("""
            const env = fixture(); const button = env.makeButton();
            button.style.display = 'none'; env.state.button = button; env.run();
            const hidden = env.state.clicks.length;
            button.style.display = 'block'; button.style.opacity = '0'; env.mutate();
            const transparent = env.state.clicks.length;
            button.style.opacity = '1'; env.tick();
            return {hidden, transparent, clicks:env.state.clicks.length,
                    interval:env.state.intervals[0].milliseconds,
                    observed:env.state.observers[0].options};
        """)
        self.assertEqual((result["hidden"], result["transparent"], result["clicks"]), (0, 0, 1))
        self.assertEqual(result["interval"], 100)
        self.assertTrue(result["observed"]["characterData"])
        for attribute in ("disabled", "style", "class", "hidden", "aria-disabled", "aria-hidden"):
            self.assertIn(attribute, result["observed"]["attributeFilter"])

    def test_each_button_clicks_once_and_duplicate_injection_does_not_add_watchers(self):
        result = self.scenario("""
            const env = fixture(); env.state.button = env.makeButton('first');
            env.run(); env.run(); env.mutate(); env.tick();
            env.state.button = env.makeButton('second'); env.mutate(); env.tick();
            return {clicks:env.state.clicks, ids:env.state.messages.map(m => m.click_id),
                    observers:env.state.observers.length, intervals:env.state.intervals.length};
        """)
        self.assertEqual(result, {
            "clicks": ["first", "second"], "ids": ["click-1", "click-2"],
            "observers": 1, "intervals": 1,
        })

    def test_reload_creates_a_fresh_watcher(self):
        result = self.scenario("""
            const first = fixture(); first.state.button = first.makeButton(); first.run();
            const reloaded = fixture(); reloaded.state.button = reloaded.makeButton(); reloaded.run();
            return [first.state.clicks.length, reloaded.state.clicks.length];
        """)
        self.assertEqual(result, [1, 1])

    def test_iframe_wrong_origin_and_wrong_episode_never_install_or_click(self):
        result = self.scenario("""
            const variations = [
                env => {env.window.top = {};},
                env => {env.window.location.origin = 'https://global.novelpia.com.evil.test';},
                env => {env.window.location.origin = 'http://global.novelpia.com';},
                env => {env.window.location.origin = 'https://global.novelpia.com:8443';},
                env => {env.window.location.pathname = '/viewer/670404';},
                env => {env.window.location.pathname = '/login';}
            ];
            return variations.map(change => {
                const env = fixture(); env.state.button = env.makeButton(); change(env); env.run();
                return [env.state.clicks.length, env.state.observers.length, env.state.intervals.length];
            });
        """)
        self.assertEqual(result, [[0, 0, 0]] * 6)

    def test_spa_navigation_is_rechecked_before_clicking(self):
        result = self.scenario("""
            const env = fixture(); env.run(); env.state.button = env.makeButton();
            env.window.location.pathname = '/viewer/670404'; env.mutate(); env.tick();
            return env.state.clicks.length;
        """)
        self.assertEqual(result, 0)

    def test_unrelated_invisible_disabled_or_covered_controls_never_click(self):
        result = self.scenario("""
            const variations = [
                (env,b) => {b.tagName = 'DIV';},
                (env,b) => {b.textContent = 'Skip';},
                (env,b) => {b.attributes['aria-disabled'] = 'true';},
                (env,b) => {b.attributes['aria-hidden'] = 'true';},
                (env,b) => {b.matches = () => true;},
                (env,b) => {b.rectangles = [];},
                (env,b) => {b.style.visibility = 'hidden';},
                (env,b) => {b.parentElement = {style:{display:'block',visibility:'visible',opacity:'0'}};},
                (env,b) => {b.rect.top = 900; b.rect.bottom = 940;},
                (env,b) => {env.state.covered = true;},
                (env,b) => {env.document.getElementById = () => null;}
            ];
            return variations.map(change => {
                const env = fixture(); const button = env.makeButton(); env.state.button = button;
                change(env,button); env.run(); env.mutate(); env.tick();
                return env.state.clicks.length;
            });
        """)
        self.assertEqual(result, [0] * 11)

    def test_failed_click_is_not_reported_and_can_retry_when_control_recovers(self):
        result = self.scenario("""
            const env = fixture(); env.state.button = env.makeButton(); env.state.throwClick = true;
            env.run(); const early = env.state.messages.length;
            env.state.throwClick = false; env.mutate(); env.tick();
            return {early, clicks:env.state.clicks.length, messages:env.state.messages.length};
        """)
        self.assertEqual(result, {"early": 0, "clicks": 1, "messages": 1})

    def test_missing_native_bridge_does_not_create_unreported_clicks(self):
        result = self.scenario("""
            const env = fixture(); env.state.button = env.makeButton(); delete env.window.chrome;
            env.run(); return env.state.clicks.length;
        """)
        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
