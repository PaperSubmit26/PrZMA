# VM_Agent/services/browser_service.py
from __future__ import annotations

import os
import time
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional, List

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page, Playwright, TimeoutError as PWTimeoutError


@dataclass
class BrowserSession:
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page
    browser_type: str
    channel: Optional[str]
    headless: bool
    user_data_dir: Optional[str]
    profile_name: Optional[str]


class BrowserService:
    """
    Per-agent browser automation service (Playwright).
    - Supports Chrome/Edge/Chromium via `channel`.
    - Keeps one active Page per agent session.
    """

    def __init__(self):
        self._sessions: Dict[str, BrowserSession] = {}

    # Session lifecycle
    def ensure_launched(self, agent_id: str, cfg: Dict[str, Any]) -> BrowserSession:
        if agent_id in self._sessions:
            sess = self._sessions[agent_id]
            try:
                if sess.page.is_closed():
                    raise RuntimeError("page closed")
                _ = sess.page.url  # test
                return sess
            except Exception:
                # If the session is stale, clean it up and recreate
                self.close(agent_id)

        browser_type = (cfg.get("browser") or "chromium").lower()
        headless = bool(cfg.get("headless", False))
        channel = cfg.get("channel")  # e.g., "chrome", "msedge"
        user_data_dir = cfg.get("user_data_dir")
        
        profile_name = cfg.get("profile_name")
        locale = cfg.get("locale") or "en-US"
        tz = cfg.get("timezone") or "UTC"
        extra_args: List[str] = list(cfg.get("extra_args") or [])

        pw = sync_playwright().start()

        # Use chromium engine for chrome/msedge channels
        chromium = pw.chromium

        # always use persistent context so artifacts land in a stable profile directory.
        if not user_data_dir:
            drive = os.environ.get("SYSTEMDRIVE") or "C:"
            # e.g. C:\PrZMA\profiles\A1\chrome  or  C:\PrZMA\profiles\A1\msedge
            user_data_dir = os.path.join(drive + os.sep, "PrZMA", "profiles", agent_id, (channel or "chromium"))
            os.makedirs(user_data_dir, exist_ok=True)

        # Immediately after user_data_dir is confirmed (before launch_persistent_context is called)
        if channel == "msedge":
            os.environ["PRZMA_EDGE_ROOT"] = user_data_dir
        else:
            os.environ["PRZMA_CHROME_ROOT"] = user_data_dir


        context = chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            channel=channel,
            headless=headless,
            locale=locale,
            timezone_id=tz,
            args=extra_args,
        )
        page = context.new_page()
        # Ensure page is ready before returning
        try:
            page.goto("about:blank", wait_until="domcontentloaded", timeout=15000)
        except Exception:
            # If goto fails, try to verify page is still valid
            try:
                _ = page.url  # Test if page is accessible
            except Exception:
                # Page is invalid, create a new one
                try:
                    page.close()
                except Exception:
                    pass
                page = context.new_page()
                try:
                    page.goto("about:blank", wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
        browser = context.browser

        sess = BrowserSession(
            playwright=pw,
            browser=browser,
            context=context,
            page=page,
            browser_type=browser_type,
            channel=channel,
            headless=headless,
            user_data_dir=user_data_dir,
            profile_name=profile_name,
        )
        self._sessions[agent_id] = sess
        return sess

    def close(self, agent_id: str) -> None:
        sess = self._sessions.pop(agent_id, None)
        if not sess:
            return
        try:
            sess.context.close()
        except Exception:
            pass
        try:
            sess.browser.close()
        except Exception:
            pass
        try:
            sess.playwright.stop()
        except Exception:
            pass

    # helper 
    def _page(self, agent_id: str) -> Page:
        if agent_id not in self._sessions:
            raise RuntimeError(f"Browser session not launched for agent_id={agent_id}")
        sess = self._sessions[agent_id]
        # Verify page is still valid
        try:
            if sess.page.is_closed():
                raise RuntimeError("page closed")
            _ = sess.page.url  # Test if page is accessible
        except Exception:
            # Page is closed or invalid, create a new one
            try:
                sess.page.close()
            except Exception:
                pass
            sess.page = sess.context.new_page()
            try:
                sess.page.goto("about:blank", wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
        return sess.page
    # Action handlers
    def action_launch(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        params.browser_config: shared.schemas.BrowserConfig.to_dict()
        """
        cfg = params.get("browser_config") or {}
        self.ensure_launched(agent_id, cfg)
        return {"status": "launched", "channel": cfg.get("channel"), "headless": cfg.get("headless", False)}

    def action_goto(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        url = params["url"]
        wait = params.get("wait", "domcontentloaded")  # load | domcontentloaded | networkidle
        timeout_ms = int(params.get("timeout_ms", 60000))
        page = self._page(agent_id)
        page.goto(url, wait_until=wait, timeout=timeout_ms)
        return {"current_url": page.url}

    def action_click(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        selector = params["selector"]
        timeout_ms = int(params.get("timeout_ms", 15000))
        page = self._page(agent_id)
        page.locator(selector).first.click(timeout=timeout_ms)
        return {"clicked": selector}

    def action_get_clickables(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Enumerate clickable elements on the current page (runtime DOM).
        Returns: { "current_url": str, "clickables": [ {"selector": str, "tag": str, "text": str}, ... ] }
        Used by full-trigger: click each → trigger snapshot → goto base_url → next.
        """
        timeout_ms = int(params.get("timeout_ms", 30000))  # Increased default timeout
        page = self._page(agent_id)
        
        # Wait for page to be ready, especially for Discord SPA
        current_url = page.url or ""
        if "discord.com" in current_url:
            # Wait for Discord to load: look for message list or main content
            try:
                # Wait for either message list or main app container
                page.wait_for_selector(
                    "ol[data-list-id='chat-messages'], div[class*='chatContent'], div[class*='app']",
                    timeout=min(timeout_ms, 15000),
                    state="attached"
                )
            except Exception:
                pass  # Continue even if selector not found
            # Additional wait for dynamic content
            page.wait_for_timeout(2000)
        
        js = """
        () => {
            const clickables = [];
            
            // More comprehensive selector for Discord and modern SPAs
            // Discord uses div[role="button"], div with click handlers, etc.
            const selectors = [
                'a[href]',
                'button:not([disabled])',
                '[role="button"]',
                '[role="link"]',
                '[role="menuitem"]',
                '[role="option"]',
                'input[type="submit"]',
                'input[type="button"]',
                '[onclick]',
                '[tabindex]:not([tabindex="-1"])',
                // Discord-specific: divs with click handlers or cursor pointer
                'div[class*="button"]',
                'div[class*="Button"]',
                'div[style*="cursor: pointer"]',
            ];
            
            // Collect all candidates
            const candidates = new Set();
            for (const sel of selectors) {
                try {
                    document.querySelectorAll(sel).forEach(el => candidates.add(el));
                } catch (e) {
                    // Invalid selector, skip
                }
            }
            
            // Also check for elements with click event listeners (Discord pattern)
            // Walk through all elements and check if they're clickable
            const allElements = document.querySelectorAll('*');
            for (const el of allElements) {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                // Check if element looks clickable
                if (style.cursor === 'pointer' || 
                    el.onclick !== null ||
                    el.getAttribute('onclick') ||
                    (el.tagName === 'DIV' && rect.width > 20 && rect.height > 20 && style.cursor !== 'default')) {
                    candidates.add(el);
                }
            }
            
            function buildSelector(el) {
                // Try ID first
                if (el.id && /^[a-zA-Z][\\w.-]*$/.test(el.id)) {
                    return '#' + CSS.escape(el.id);
                }
                
                // Try data attributes (Discord uses these)
                if (el.getAttribute('data-list-id')) {
                    return `[data-list-id="${CSS.escape(el.getAttribute('data-list-id'))}"]`;
                }
                if (el.getAttribute('aria-label')) {
                    const label = el.getAttribute('aria-label');
                    return `[aria-label="${CSS.escape(label)}"]`;
                }
                
                // Build path
                const path = [];
                let current = el;
                while (current && current.nodeType === 1 && path.length < 15) {
                    let part = current.tagName.toLowerCase();
                    if (current.id && /^[a-zA-Z][\\w.-]*$/.test(current.id)) {
                        path.unshift('#' + CSS.escape(current.id));
                        break;
                    }
                    // Add class if unique enough
                    const classes = Array.from(current.classList || []).filter(c => c.length > 0);
                    if (classes.length > 0 && classes.length < 5) {
                        part += '.' + classes.map(c => CSS.escape(c)).join('.');
                    }
                    let sibling = current;
                    let nth = 1;
                    while (sibling.previousElementSibling) {
                        sibling = sibling.previousElementSibling;
                        if (sibling.tagName === current.tagName) nth++;
                    }
                    if (nth > 1) {
                        path.unshift(part + ':nth-of-type(' + nth + ')');
                    } else {
                        path.unshift(part);
                    }
                    current = current.parentElement;
                    if (current && current.tagName === 'BODY') break;
                }
                return path.join(' > ');
            }
            
            const seen = new Set();
            for (const el of candidates) {
                if (!el || el.nodeType !== 1) continue;
                
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                
                // Skip hidden elements
                if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) < 0.1) {
                    continue;
                }
                
                // Skip very small elements (but allow small icons)
                if (rect.width < 1 && rect.height < 1 && el.tagName !== 'BODY') {
                    continue;
                }
                
                // Skip elements outside viewport (but include some margin)
                if (rect.bottom < -50 || rect.top > window.innerHeight + 50) {
                    continue;
                }
                
                const selector = buildSelector(el);
                if (!selector || seen.has(selector)) continue;
                seen.add(selector);
                
                const text = (el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 120);
                clickables.push({ 
                    selector, 
                    tag: el.tagName.toLowerCase(), 
                    text: text || selector.slice(0, 50),
                    role: el.getAttribute('role') || '',
                });
            }
            
            return { 
                clickables, 
                current_url: window.location.href, 
                count: clickables.length,
                debug: {
                    candidates_found: candidates.size,
                    after_filter: clickables.length
                }
            };
        }
        """
        try:
            out = page.evaluate(js)
            current_url = out.get("current_url") if isinstance(out, dict) else None
            if not current_url:
                current_url = page.url
            if not current_url:
                # Fallback: try to get URL from page
                try:
                    current_url = page.evaluate("() => window.location.href")
                except Exception:
                    current_url = ""
            
            clickables_list = (out.get("clickables") or []) if isinstance(out, dict) else []
            count = out.get("count", len(clickables_list)) if isinstance(out, dict) else len(clickables_list)
            
            # If no clickables found on Discord, wait a bit more and retry once
            if len(clickables_list) == 0 and "discord.com" in current_url:
                page.wait_for_timeout(3000)
                try:
                    out = page.evaluate(js)
                    clickables_list = (out.get("clickables") or []) if isinstance(out, dict) else []
                except Exception:
                    pass
            
            return {
                "current_url": current_url or "",
                "clickables": clickables_list,
                "count": len(clickables_list),
            }
        except Exception as e:
            try:
                fallback_url = page.url or page.evaluate("() => window.location.href")
            except Exception:
                fallback_url = ""
            return {"current_url": fallback_url, "clickables": [], "error": str(e), "count": 0}

    def action_type(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        selector = params["selector"]
        text = params.get("text", "")
        clear = bool(params.get("clear", True))
        timeout_ms = int(params.get("timeout_ms", 15000))
        page = self._page(agent_id)
        loc = page.locator(selector).first
        if clear:
            loc.fill("", timeout=timeout_ms)
        loc.type(text, timeout=timeout_ms)
        return {"typed": True, "selector": selector, "len": len(text)}

    def action_press(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        key = params["key"]  # e.g., "Enter"
        page = self._page(agent_id)
        page.keyboard.press(key)
        return {"pressed": key}

    def action_screenshot(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        path = params.get("path")
        full_page = bool(params.get("full_page", True))
        page = self._page(agent_id)
        if not path:
            ts = int(time.time() * 1000)
            path = os.path.abspath(f".\\_shots\\{agent_id}_{ts}.png")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        page.screenshot(path=path, full_page=full_page)
        return {"screenshot_path": path}

    def capture_page_state(self, agent_id: str, out_dir: str) -> Dict[str, str]:
        """
        Capture current page for schema-tracking: HTML, DOM tree, screenshot, IndexedDB schema.
        Must be called from the thread that owns the page (e.g. ActionWorker).
        Returns paths relative to out_dir: html_path, dom_path, screenshot_path, schema_path.
        """
        page = self._page(agent_id)
        os.makedirs(out_dir, exist_ok=True)
        base = os.path.join(out_dir, "web_state")
        html_path = base + "_page.html"
        dom_path = base + "_dom.json"
        screenshot_path = base + "_screenshot.png"
        schema_path = base + "_indexeddb_schema.json"

        # 1) Full HTML
        try:
            html = page.content()
            with open(html_path, "w", encoding="utf-8", errors="replace") as f:
                f.write(html)
        except Exception:
            pass

        # 2) Simplified DOM tree (tagName, child count, id/class for comparison)
        try:
            dom_script = """
            () => {
                function walk(el, depth) {
                    if (depth > 50) return null;
                    const obj = { tag: el.tagName ? el.tagName.toLowerCase() : null, id: el.id || null, class: el.className && typeof el.className === 'string' ? el.className.slice(0, 200) : null, childCount: el.childElementCount || 0, children: [] };
                    for (let i = 0; i < Math.min((el.children && el.children.length) || 0, 100); i++) {
                        const c = walk(el.children[i], depth + 1);
                        if (c) obj.children.push(c);
                    }
                    return obj;
                }
                return walk(document.documentElement || document.body, 0);
            }
            """
            dom_tree = page.evaluate(dom_script)
            with open(dom_path, "w", encoding="utf-8") as f:
                json.dump(dom_tree, f, ensure_ascii=False, indent=0)
        except Exception:
            pass

        # 3) Screenshot
        try:
            page.screenshot(path=screenshot_path, full_page=True)
        except Exception:
            pass

        # 4) IndexedDB schema (databases + object store names)
        try:
            # Use page.evaluate() with async function - Playwright automatically awaits async functions
            # But we'll add explicit error handling and logging
            idb_script = """
            async () => {
                if (typeof indexedDB === 'undefined') {
                    return [{ error: 'indexedDB is undefined' }];
                }
                try {
                    // indexedDB.databases() is available in modern browsers
                    let dbs = [];
                    if (indexedDB.databases) {
                        dbs = await indexedDB.databases();
                    } else {
                        return [{ error: 'indexedDB.databases() not available' }];
                    }
                    
                    if (!dbs || dbs.length === 0) {
                        return [{ info: 'No IndexedDB databases found' }];
                    }
                    
                    const out = [];
                    for (const db of dbs) {
                        try {
                            const info = await new Promise((res) => {
                                const r = indexedDB.open(db.name);
                                let resolved = false;
                                
                                r.onsuccess = () => {
                                    if (resolved) return;
                                    resolved = true;
                                    try {
                                        const d = r.result;
                                        const stores = Array.from(d.objectStoreNames || []);
                                        d.close();
                                        res({ name: db.name, version: db.version, objectStores: stores });
                                    } catch (e) {
                                        res({ name: db.name, error: String(e) });
                                    }
                                };
                                
                                r.onerror = () => {
                                    if (resolved) return;
                                    resolved = true;
                                    res({ name: db.name, error: String(r.error || 'Unknown error') });
                                };
                                
                                // Timeout fallback (5 seconds)
                                setTimeout(() => {
                                    if (!resolved) {
                                        resolved = true;
                                        res({ name: db.name, error: 'Timeout opening database (5s)' });
                                    }
                                }, 5000);
                            });
                            out.push(info);
                        } catch (e) {
                            out.push({ name: db.name || 'unknown', error: String(e) });
                        }
                    }
                    return out;
                } catch (e) {
                    return [{ error: `Failed to enumerate databases: ${String(e)}` }];
                }
            }
            """
            # Playwright's evaluate() automatically awaits async functions
            schema_list = page.evaluate(idb_script)
            
            # Ensure we got a list (not a Promise or other type)
            if not isinstance(schema_list, list):
                if schema_list is None:
                    schema_list = []
                else:
                    schema_list = [{"error": f"Unexpected result type: {type(schema_list).__name__}", "raw": str(schema_list)[:200]}]
            
            # Log if empty to help debug
            import logging
            if len(schema_list) == 0:
                logging.getLogger(__name__).warning("IndexedDB schema extraction returned empty list - no databases found")
            else:
                logging.getLogger(__name__).debug(f"IndexedDB schema extraction found {len(schema_list)} database(s)")
            
            with open(schema_path, "w", encoding="utf-8") as f:
                json.dump(schema_list, f, ensure_ascii=False, indent=2)
        except Exception as e:
            # Log error but don't fail snapshot collection
            import logging
            import traceback
            logging.getLogger(__name__).warning(f"Failed to extract IndexedDB schema: {e}\n{traceback.format_exc()}")
            with open(schema_path, "w", encoding="utf-8") as f:
                json.dump([{"error": str(e), "type": type(e).__name__, "traceback": traceback.format_exc()}], f, ensure_ascii=False, indent=2)

        return {
            "html_path": html_path,
            "dom_path": dom_path,
            "screenshot_path": screenshot_path,
            "schema_path": schema_path,
        }

    def action_scroll(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Scroll the page.
        params:
        - direction: str (up, down, left, right, top, bottom)
        - pixels: int (optional, amount to scroll)
        - selector: str (optional, scroll to element)
        """
        direction = params.get("direction", "down")
        pixels = params.get("pixels")
        selector = params.get("selector")
        
        page = self._page(agent_id)
        
        # Scroll to element if selector provided
        if selector:
            page.locator(selector).first.scroll_into_view_if_needed()
            return {"scrolled_to": selector}
        
        # Scroll by direction
        if direction == "top":
            page.evaluate("window.scrollTo(0, 0)")
        elif direction == "bottom":
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        elif direction == "down":
            amt = pixels or 500
            page.evaluate(f"window.scrollBy(0, {amt})")
        elif direction == "up":
            amt = pixels or 500
            page.evaluate(f"window.scrollBy(0, -{amt})")
        elif direction == "right":
            amt = pixels or 500
            page.evaluate(f"window.scrollBy({amt}, 0)")
        elif direction == "left":
            amt = pixels or 500
            page.evaluate(f"window.scrollBy(-{amt}, 0)")
        
        return {"scrolled": direction, "pixels": pixels}

    def action_get_text(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        selector = params.get("selector")
        page = self._page(agent_id)
        if selector:
            txt = page.locator(selector).first.inner_text()
        else:
            txt = page.content()
        return {"text": txt}

    def action_search_google(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        params:
          - query: str (required)
          - num_results: int (default 5)
          - open_first: bool (default False)
        """
        query = params["query"]
        num_results = int(params.get("num_results", 5))
        open_first = bool(params.get("open_first", False))
        timeout_ms = int(params.get("timeout_ms", 60000))

        page = self._page(agent_id)
        page.goto("https://www.google.com", wait_until="domcontentloaded", timeout=timeout_ms)

        # Consent dialogs vary (best-effort close)
        try:
            page.locator("button:has-text('I agree')").first.click(timeout=2000)
        except Exception:
            pass
        try:
            page.locator("button:has-text('Accept all')").first.click(timeout=2000)
        except Exception:
            pass

        box = page.locator("textarea[name='q'], input[name='q']").first
        box.click(timeout=timeout_ms)
        box.fill(query)
        page.keyboard.press("Enter")

        # Results
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        results = []
        cards = page.locator("a:has(h3)").all()
        for a in cards[: max(1, num_results)]:
            try:
                title = a.locator("h3").first.inner_text()
                href = a.get_attribute("href")
                if href and title:
                    results.append({"title": title, "url": href})
            except Exception:
                continue

        if open_first and results:
            page.goto(results[0]["url"], wait_until="load", timeout=timeout_ms)

        return {"query": query, "results": results, "current_url": page.url}

    # Smart helpers (no LLM)
    def _is_probably_noise_text(self, s: str) -> bool:
        s = (s or "").strip().lower()
        if not s:
            return True
        noise = [
            "accept", "agree", "cookie", "consent", "privacy", "terms",
            "sign in", "log in", "login", "subscribe", "newsletter",
            "ad", "sponsored", "skip", "close", "allow all",
        ]
        return any(x in s for x in noise)

    def _pick_best_click_candidate(
        self,
        candidates: List[Dict[str, Any]],
        *,
        strategy: str = "primary",
        prefer_text_keywords: Optional[List[str]] = None,
        avoid_text_keywords: Optional[List[str]] = None,
        randomize: bool = False,
    ) -> Optional[Dict[str, Any]]:
        prefer_text_keywords = [x.lower() for x in (prefer_text_keywords or [])]
        avoid_text_keywords = [x.lower() for x in (avoid_text_keywords or [])]

        scored: List[tuple[float, Dict[str, Any]]] = []
        for c in candidates:
            txt = (c.get("text") or "").strip()
            href = (c.get("href") or "").strip()
            role = (c.get("role") or "").strip().lower()
            tag = (c.get("tag") or "").strip().lower()
            area = float(c.get("area") or 0.0)

            t = txt.lower()

            # hard filters
            if area <= 400:  # Exclude low-signal UI clicks (icons, minor buttons)
                continue
            if self._is_probably_noise_text(t):
                continue
            if any(k in t for k in avoid_text_keywords):
                continue

            score = 0.0

            if txt:
                score += 2.0

            if tag == "button":
                score += 3.0
            if tag == "a" and href:
                score += 2.0

            if role in ("button", "link"):
                score += 1.5

            if area >= 200000:
                score -= 2.0

            if prefer_text_keywords and any(k in t for k in prefer_text_keywords):
                score += 3.0

            if strategy == "search_result":
                if tag == "a" and href and txt:
                    score += 1.5
            elif strategy == "primary":
                if tag == "button":
                    score += 1.5

            scored.append((score, c))

        if not scored:
            return None

        scored.sort(key=lambda x: x[0], reverse=True)

        if randomize:
            top = scored[: min(5, len(scored))]
            return random.choice(top)[1]

        return scored[0][1]

    def action_smart_click(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Click a contextually reasonable element without a selector.
        params:
          - strategy: "primary" | "search_result" | "link" (default "primary")
          - prefer_text_keywords: [str] (optional)
          - avoid_text_keywords: [str] (optional)
          - randomize: bool (default True; choose among top candidates)
          - timeout_ms: int (default 15000)
        """
        strategy = (params.get("strategy") or "primary").lower()
        prefer_text_keywords = params.get("prefer_text_keywords") or []
        avoid_text_keywords = params.get("avoid_text_keywords") or []
        randomize = bool(params.get("randomize", True))
        timeout_ms = int(params.get("timeout_ms", 15000))

        page = self._page(agent_id)

        if strategy == "search_result":
            try:
                a = page.locator("a:has(h3)").first
                if a.count() > 0:
                    txt = ""
                    href = a.get_attribute("href") or ""
                    try:
                        txt = a.locator("h3").first.inner_text()
                    except Exception:
                        pass
                    a.click(timeout=timeout_ms)
                    return {"smart_clicked": True, "strategy": strategy, "text": txt, "href": href, "current_url": page.url}
            except Exception:
                pass

        js = r"""
        () => {
          const els = Array.from(document.querySelectorAll('a, button, [role="button"], [role="link"], input[type="submit"], input[type="button"]'));
          const out = [];
          for (const el of els) {
            const r = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            if (!r || r.width <= 0 || r.height <= 0) continue;
            if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue;

            // Prefer in-viewport elements
            const inView = r.bottom > 0 && r.right > 0 && r.top < (window.innerHeight || 0) && r.left < (window.innerWidth || 0);
            if (!inView) continue;

            const tag = (el.tagName || '').toLowerCase();
            const role = (el.getAttribute('role') || '').toLowerCase();
            const href = (tag === 'a') ? (el.getAttribute('href') || '') : '';
            let text = (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
            text = text.replace(/\s+/g, ' ').slice(0, 120);

            out.push({
              tag, role, href, text,
              x: Math.round(r.x), y: Math.round(r.y),
              w: Math.round(r.width), h: Math.round(r.height),
              area: Math.round(r.width * r.height)
            });
          }
          return out;
        }
        """
        candidates = page.evaluate(js) or []
        if not isinstance(candidates, list) or not candidates:
            raise RuntimeError("smart_click: no clickable candidates found")

        chosen = self._pick_best_click_candidate(
            candidates,
            strategy=strategy,
            prefer_text_keywords=prefer_text_keywords if isinstance(prefer_text_keywords, list) else None,
            avoid_text_keywords=avoid_text_keywords if isinstance(avoid_text_keywords, list) else None,
            randomize=randomize,
        )
        if not chosen:
            raise RuntimeError("smart_click: no suitable candidate after filtering")

        cx = int(chosen["x"] + max(1, chosen["w"]) * 0.5)
        cy = int(chosen["y"] + max(1, chosen["h"]) * 0.5)

        page.mouse.click(cx, cy, timeout=timeout_ms)

        return {
            "smart_clicked": True,
            "strategy": strategy,
            "picked": {
                "tag": chosen.get("tag"),
                "role": chosen.get("role"),
                "text": chosen.get("text"),
                "href": chosen.get("href"),
                "box": {"x": chosen.get("x"), "y": chosen.get("y"), "w": chosen.get("w"), "h": chosen.get("h")},
            },
            "current_url": page.url,
        }

    def action_smart_type(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Type into a contextually reasonable input without a selector.
        params:
          - text: str (required)
          - intent: "search" | "generic" (default "search")
          - press_enter: bool (default True if intent=search else False)
          - timeout_ms: int (default 15000)
          - clear: bool (default True)
        """
        text = params.get("text")
        if text is None:
            raise ValueError("smart_type requires 'text'")
        text = str(text)

        intent = (params.get("intent") or "search").lower()
        timeout_ms = int(params.get("timeout_ms", 15000))
        clear = bool(params.get("clear", True))

        press_enter = params.get("press_enter")
        if press_enter is None:
            press_enter = (intent == "search")
        press_enter = bool(press_enter)

        page = self._page(agent_id)

        js = r"""
        (intent) => {
          const sels = [
            'textarea',
            'input',
            '[contenteditable="true"]'
          ];
          const els = Array.from(document.querySelectorAll(sels.join(',')));
          const out = [];
          for (const el of els) {
            const r = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            if (!r || r.width <= 0 || r.height <= 0) continue;
            if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue;

            const inView = r.bottom > 0 && r.right > 0 && r.top < (window.innerHeight || 0) && r.left < (window.innerWidth || 0);
            if (!inView) continue;

            const tag = (el.tagName || '').toLowerCase();
            const type = (tag === 'input') ? ((el.getAttribute('type') || '').toLowerCase()) : '';
            if (tag === 'input') {
              // Only allow text/search/email/url inputs
              const ok = ['text','search','email','url',''].includes(type);
              if (!ok) continue;
            }

            const name = (el.getAttribute('name') || '').toLowerCase();
            const placeholder = (el.getAttribute('placeholder') || '').toLowerCase();
            const aria = (el.getAttribute('aria-label') || '').toLowerCase();
            const role = (el.getAttribute('role') || '').toLowerCase();

            out.push({
              tag, type, role, name, placeholder, aria,
              x: Math.round(r.x), y: Math.round(r.y),
              w: Math.round(r.width), h: Math.round(r.height),
              area: Math.round(r.width * r.height)
            });
          }
          return out;
        }
        """
        candidates = page.evaluate(js, intent) or []
        if not isinstance(candidates, list) or not candidates:
            raise RuntimeError("smart_type: no input candidates found")

        # Scoring: If intent=search, searchbox/placeholder/name/q takes precedence
        def score_input(c: Dict[str, Any]) -> float:
            score = 0.0
            area = float(c.get("area") or 0.0)
            score += min(3.0, area / 60000.0)  # Prefer a moderately large input box

            if intent == "search":
                if c.get("type") == "search":
                    score += 3.0
                if c.get("role") == "searchbox":
                    score += 3.0
                blob = " ".join([c.get("name",""), c.get("placeholder",""), c.get("aria","")]).lower()
                if "search" in blob:
                    score += 2.5
                if c.get("name") in ("q", "query", "s", "keyword"):
                    score += 2.0
                if c.get("placeholder","").find("search") >= 0:
                    score += 1.5
            return score

        candidates_scored = sorted([(score_input(c), c) for c in candidates], key=lambda x: x[0], reverse=True)
        chosen = candidates_scored[0][1]

        # Click on the coordinates to focus and input using the keyboard
        cx = int(chosen["x"] + max(1, chosen["w"]) * 0.2)  # Left click is stable
        cy = int(chosen["y"] + max(1, chosen["h"]) * 0.5)
        page.mouse.click(cx, cy, timeout=timeout_ms)

        if clear:
            # Ctrl+A, Backspace for clear
            try:
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
            except Exception:
                pass

        page.keyboard.type(text, delay=30)

        if press_enter:
            page.keyboard.press("Enter")

        return {
            "smart_typed": True,
            "intent": intent,
            "press_enter": press_enter,
            "picked": {
                "tag": chosen.get("tag"),
                "type": chosen.get("type"),
                "role": chosen.get("role"),
                "name": chosen.get("name"),
                "placeholder": chosen.get("placeholder"),
                "aria": chosen.get("aria"),
                "box": {"x": chosen.get("x"), "y": chosen.get("y"), "w": chosen.get("w"), "h": chosen.get("h")},
            },
            "len": len(text),
            "current_url": page.url,
        }


    # Dispatcher
    def execute(self, agent_id: str, action_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        action_name examples:
          - browser.launch
          - browser.goto
          - browser.search_google
          - browser.click / type / press / screenshot / get_text
        """
        verb = action_name.split(".", 1)[1] if "." in action_name else action_name

        if verb == "launch":
            return self.action_launch(agent_id, params)
        if verb == "goto":
            return self.action_goto(agent_id, params)
        if verb == "click":
            return self.action_click(agent_id, params)
        if verb == "get_clickables":
            return self.action_get_clickables(agent_id, params)
        if verb == "type":
            return self.action_type(agent_id, params)
        if verb == "press":
            return self.action_press(agent_id, params)
        if verb == "scroll":
            return self.action_scroll(agent_id, params)
        if verb == "screenshot":
            return self.action_screenshot(agent_id, params)
        if verb == "get_text":
            return self.action_get_text(agent_id, params)
        if verb == "search_google":
            return self.action_search_google(agent_id, params)
        if verb == "smart_click":
            return self.action_smart_click(agent_id, params)
        if verb == "smart_type":
            return self.action_smart_type(agent_id, params)
        if verb == "close":
            self.close(agent_id)
            return {"status": "closed"}

        raise ValueError(f"Unsupported browser action: {action_name}")
