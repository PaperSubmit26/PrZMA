# VM_Agent/services/discord_service.py
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from playwright.sync_api import TimeoutError as PWTimeoutError

from .browser_service import BrowserService


class DiscordService:
    """
    Discord Web automation on top of BrowserService.
    - Recommended: run BrowserService with persistent profile so login persists.
    """

    def __init__(self, browser: BrowserService):
        self.browser = browser

    def _page(self, agent_id: str):
        return self.browser._page(agent_id)  

    def ensure_open(self, agent_id: str, timeout_ms: int = 30000) -> None:
        page = self._page(agent_id)
        if "discord.com" not in (page.url or ""):
            page.goto("https://discord.com/app", wait_until="domcontentloaded", timeout=timeout_ms)

    def action_open(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        timeout_ms = int(params.get("timeout_ms", 30000))
        self.ensure_open(agent_id, timeout_ms)
        return {"current_url": self._page(agent_id).url}

    def action_login(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Best-effort login. Prefer persistent profile in browser config.
        Handles session expiration modals/prompts that require re-login.
        If already logged in (app/channels URL or no login form after goto login), returns success without filling.
        params:
          - email
          - password
        """
        email = params.get("email")
        password = params.get("password")
        timeout_ms = int(params.get("timeout_ms", 30000))
        short_wait_ms = 5000  # for "already logged in" check

        page = self._page(agent_id)
        current = (page.url or "").strip().lower()

        # Check for session expiration modal/prompt using DOM analysis
        session_expired = page.evaluate(
            """() => {
            // Look for session expiration indicators in the page
            const bodyText = document.body.textContent || '';
            const lowerText = bodyText.toLowerCase();
            
            // Check for session expiration keywords
            const expiredKeywords = [
                'session expired',
                'log in again',
                're-authenticate',
                'please log in',
                'your session has expired',
            ];
            
            const hasExpiredText = expiredKeywords.some(keyword => lowerText.includes(keyword));
            
            // Also check for modals/dialogs that might be session expiration prompts
            const modals = document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="Modal"]');
            let hasExpiredModal = false;
            for (const modal of modals) {
                const modalText = (modal.textContent || '').toLowerCase();
                if (expiredKeywords.some(keyword => modalText.includes(keyword))) {
                    hasExpiredModal = true;
                    break;
                }
            }
            
            // Check for login buttons in modals (might be "Log In" button in session expired modal)
            const loginButtons = Array.from(document.querySelectorAll('button, [role="button"]')).filter(btn => {
                const text = (btn.textContent || '').toLowerCase();
                const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                return (text.includes('log in') || label.includes('log in')) && 
                       !text.includes('log out') && !label.includes('log out');
            });
            
            return {
                expired: hasExpiredText || hasExpiredModal,
                hasLoginButtons: loginButtons.length > 0,
                modalCount: modals.length
            };
            }"""
        )
        
        # If session expired modal detected, navigate to login page
        if session_expired.get("expired") or (session_expired.get("hasLoginButtons") and session_expired.get("modalCount", 0) > 0):
            # Close any modals first
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            page.goto("https://discord.com/login", wait_until="domcontentloaded", timeout=timeout_ms)
        # Already in app (e.g. /app, /channels) → check if really logged in or session expired
        elif "discord.com" in current and ("/app" in current or "/channels/" in current):
            # Double-check: if there's a session expired indicator, we need to login
            if not session_expired.get("expired"):
                return {"current_url": page.url, "note": "Already logged in; skipped login form."}
            # Session expired, go to login
            page.goto("https://discord.com/login", wait_until="domcontentloaded", timeout=timeout_ms)
        else:
            # Not in app, go to login page
            page.goto("https://discord.com/login", wait_until="domcontentloaded", timeout=timeout_ms)
        
        current = (page.url or "").strip().lower()
        # Redirected to app/channels (e.g. Discord sends logged-in users away from /login)
        if "/app" in current or "/channels/" in current:
            return {"current_url": page.url, "note": "Already logged in; redirected from login page."}

        # Login form present? (short wait to avoid long timeout when form is missing)
        email_locator = page.locator("input[name='email']").first
        try:
            email_locator.wait_for(state="visible", timeout=short_wait_ms)
        except Exception:
            # No email field - check if there's a session expired modal that needs handling
            page.wait_for_timeout(1000)
            session_check = page.evaluate(
                """() => {
                const bodyText = (document.body.textContent || '').toLowerCase();
                const expiredKeywords = ['session expired', 'log in again'];
                return expiredKeywords.some(k => bodyText.includes(k));
                }"""
            )
            if session_check:
                # Session expired modal might be blocking, try to find and click login button
                login_clicked = page.evaluate(
                    """() => {
                    const buttons = Array.from(document.querySelectorAll('button, [role="button"]'));
                    const loginBtn = buttons.find(btn => {
                        const text = (btn.textContent || '').toLowerCase();
                        const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                        return (text.includes('log in')) && 
                               !text.includes('log out') && !label.includes('log out');
                    });
                    if (loginBtn) {
                        loginBtn.click();
                        return true;
                    }
                    return false;
                    }"""
                )
                if login_clicked:
                    page.wait_for_timeout(2000)
                    # Retry finding email field
                    try:
                        email_locator.wait_for(state="visible", timeout=short_wait_ms)
                    except Exception:
                        return {"current_url": page.url, "note": "Session expired modal handled but login form still not found."}
            else:
                # No email field (e.g. already logged in, or different UI) → treat as success so bootstrap can continue
                return {"current_url": page.url, "note": "Login form not found; assuming already logged in."}

        if email and password:
            email_locator.fill(email, timeout=timeout_ms)
            page.locator("input[name='password']").first.fill(password, timeout=timeout_ms)
            page.locator("button[type='submit']").first.click(timeout=timeout_ms)

        # Wait for app to load (may be blocked by captcha/2FA)
        try:
            page.wait_for_url("**/app", timeout=timeout_ms)
        except Exception:
            pass

        return {"current_url": page.url, "note": "Login is best-effort; persistent profile is recommended."}

    def action_goto_channel(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Most robust: provide channel URL.
        Accepts both:
          - url (canonical in actions.json)
          - channel_url (legacy)
        """
        timeout_ms = int(params.get("timeout_ms", 30000))
        self.ensure_open(agent_id, timeout_ms)
        page = self._page(agent_id)

        channel_url = params.get("url") or params.get("channel_url")
        if channel_url:
            page.goto(channel_url, wait_until="domcontentloaded", timeout=timeout_ms)
            return {"current_url": page.url}

        # Best-effort fallback (rely on Ctrl+K quick switcher)
        target = params.get("query") or params.get("channel_name")
        if not target:
            raise ValueError("discord.goto_channel requires url/channel_url or (query/channel_name).")

        try:
            page.keyboard.press("Control+K")
            box = page.locator(
                "input[aria-label*='Quick switcher'], input[placeholder*='Where would you like to go']"
            ).first
            box.fill(target, timeout=timeout_ms)
            page.keyboard.press("Enter")
            return {"switched": target, "current_url": page.url}
        except Exception as e:
            return {"current_url": page.url, "warning": f"Failed quick switcher: {e}"}

    def _find_message_box(self, page, timeout_ms: int):
        candidates = [
            "div[role='textbox'][data-slate-editor='true']",
            "div[role='textbox']",
            "textarea",
        ]
        for sel in candidates:
            loc = page.locator(sel).first
            try:
                loc.wait_for(timeout=2000)
                return loc
            except Exception:
                continue
        return None

    def action_send_message(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        params:
          - text (required)
        """
        import re
        text = params["text"]
        timeout_ms = int(params.get("timeout_ms", 30000))
        self.ensure_open(agent_id, timeout_ms)
        page = self._page(agent_id)

        box = self._find_message_box(page, timeout_ms)
        if box is None:
            raise RuntimeError("Discord message box not found. Make sure you're inside a channel.")

        box.click(timeout=timeout_ms)
        page.wait_for_timeout(200)
        
        # Check if text contains @A1, @A2, etc. and convert to actual Discord mentions
        mention_pattern = r'@(A\d+)'
        
        # First, get actual Discord usernames from recent messages
        username_map = {}  # Maps agent_ref (A1, A2) to actual Discord username
        try:
            recent_messages = self.action_get_latest_messages(agent_id, {"limit": 50, "timeout_ms": 5000})
            messages = recent_messages.get("messages", [])
            # Collect unique authors (Discord usernames)
            unique_authors = []
            for msg in messages:
                author = msg.get("author", "").strip()
                if author and author not in unique_authors:
                    unique_authors.append(author)
            
            # Simple heuristic: if we have 2 authors, map them to A1 and A2
            # First author -> A1, second author -> A2 (or vice versa)
            # This is a best-effort mapping
            if len(unique_authors) >= 2:
                # Try to map: if current agent_id is A1, then A2 is the other author
                # If current agent_id is A2, then A1 is the other author
                other_agents = [a for a in unique_authors if a]  # Filter out None/empty
                if len(other_agents) >= 1:
                    # Use first other author as the target (simplified)
                    if agent_id == "A1" and len(other_agents) >= 1:
                        username_map["A2"] = other_agents[0]
                    elif agent_id == "A2" and len(other_agents) >= 1:
                        username_map["A1"] = other_agents[0]
        except Exception:
            pass
        
        # Process text character by character, handling mentions specially
        i = 0
        while i < len(text):
            # Check if we're at a mention pattern (@A1, @A2, etc.)
            match = re.match(mention_pattern, text[i:])
            if match:
                agent_ref = match.group(1)  # e.g., "A1", "A2"
                
                # Type @ to trigger Discord mention autocomplete
                page.keyboard.type("@")
                page.wait_for_timeout(1000)  # Wait for autocomplete popup to appear
                
                # Try to use mapped username, or fallback to agent_ref
                target_name = username_map.get(agent_ref) or agent_ref
                
                # Type the target name to filter autocomplete
                # If we have actual username, use it; otherwise use agent_ref
                if target_name and target_name != agent_ref:
                    # Type actual username
                    page.keyboard.type(target_name)
                else:
                    # Type agent_ref and hope autocomplete finds it
                    page.keyboard.type(agent_ref)
                
                page.wait_for_timeout(1200)  # Wait for autocomplete to filter and show results
                
                # Try to select first result from autocomplete dropdown using DOM analysis
                autocomplete_selected = False
                mention_selected = page.evaluate(
                    """() => {
                    // Find autocomplete dropdown
                    const listbox = document.querySelector("div[role='listbox']")
                        || document.querySelector("[class*='autocomplete']")
                        || document.querySelector("[class*='mention']");
                    
                    if (!listbox) return {selected: false, error: 'no listbox found'};
                    
                    // Find first option/mention item
                    const options = Array.from(listbox.querySelectorAll("li, div[role='option'], [class*='mention'], [class*='userMention']"));
                    if (options.length === 0) return {selected: false, error: 'no options found'};
                    
                    const firstOption = options[0];
                    
                    // Click the first option
                    firstOption.scrollIntoView({behavior: 'smooth', block: 'center'});
                    firstOption.click();
                    
                    return {selected: true, optionText: firstOption.textContent || ''};
                }"""
                )
                
                if mention_selected.get("selected"):
                    autocomplete_selected = True
                    page.wait_for_timeout(500)
                else:
                    # Fallback: try Playwright locators
                    autocomplete_selectors = [
                        "div[role='listbox'] li:first-child",
                        "div[role='listbox'] div[role='option']:first-child",
                        "[class*='autocomplete'] li:first-child",
                        "[class*='mention']:first-child",
                        "[class*='userMention']:first-child",
                        "div[role='listbox'] > div:first-child",
                        "div[role='listbox'] > *:first-child",
                    ]
                    
                    for selector in autocomplete_selectors:
                        try:
                            option = page.locator(selector).first
                            if option.count() > 0:
                                option.click(timeout=3000)
                                page.wait_for_timeout(500)
                                autocomplete_selected = True
                                break
                        except Exception:
                            continue
                
                # If clicking didn't work, try pressing ArrowDown then Enter to select first result
                if not autocomplete_selected:
                    try:
                        page.keyboard.press("ArrowDown")
                        page.wait_for_timeout(600)
                        # Verify that autocomplete is still open and option is selected
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(500)
                        autocomplete_selected = True  # Assume success
                    except Exception:
                        # If that doesn't work, just press Enter (Discord might auto-select)
                        try:
                            page.keyboard.press("Enter")
                            page.wait_for_timeout(500)
                            autocomplete_selected = True  # Assume success
                        except Exception:
                            pass
                
                # Skip the matched text (@A1, @A2, etc.)
                i += len(match.group(0))
            else:
                # Regular character - type it
                page.keyboard.type(text[i])
                i += 1
        
        page.wait_for_timeout(200)
        page.keyboard.press("Enter")
        return {"sent": True, "len": len(text), "current_url": page.url}

    def action_get_latest_messages(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Best-effort fetch of recent visible messages from current channel UI.
        params:
          - limit (optional, default 10)
        returns:
          { "messages": [ { "author": str|null, "text": str, "ts": str|null } ... ] }
        """
        limit = int(params.get("limit", 10))
        timeout_ms = int(params.get("timeout_ms", 30000))
        self.ensure_open(agent_id, timeout_ms)
        page = self._page(agent_id)

        # Wait for message list to appear (best-effort)
        try:
            page.wait_for_selector("ol[data-list-id='chat-messages']", timeout=timeout_ms)
        except Exception:
            pass

        # Use DOM evaluation to be resilient to class name changes
        js = """
        (limit) => {
          const out = [];
          // Primary container used in Discord Web
          const root = document.querySelector("ol[data-list-id='chat-messages']") || document;
          // Try to collect message items (li) in the chat list
          const items = Array.from(root.querySelectorAll("li")).slice(-Math.max(1, limit) * 3); // oversample a bit
          for (const li of items) {
            // author (best-effort)
            const authorEl =
              li.querySelector("h3 span[role='button']") ||
              li.querySelector("span[class*='username']") ||
              li.querySelector("span[aria-label*='User']");

            const author = authorEl ? (authorEl.textContent || "").trim() : null;

            // message text: Discord often uses data-slate-node, or div[role="document"]
            const msgEl =
              li.querySelector("div[data-slate-node='value']") ||
              li.querySelector("div[role='document']") ||
              li.querySelector("div[class*='messageContent']");

            const text = msgEl ? (msgEl.textContent || "").trim() : "";
            if (!text) continue;

            // timestamp (best-effort)
            const timeEl = li.querySelector("time");
            const ts = timeEl ? (timeEl.getAttribute("datetime") || null) : null;

            out.push({ author, text, ts });
          }

          // keep last N
          return out.slice(-limit);
        }
        """
        messages = page.evaluate(js, limit)
        if not isinstance(messages, list):
            messages = []

        return {"current_url": page.url, "count": len(messages), "messages": messages}

    def action_upload_file(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        params:
          - file_path (required)
          - text (optional)  # message to send with/after upload
        """
        file_path = params["file_path"]
        text = params.get("text")
        timeout_ms = int(params.get("timeout_ms", 30000))
        self.ensure_open(agent_id, timeout_ms)
        page = self._page(agent_id)

        if not os.path.exists(file_path):
            raise FileNotFoundError(file_path)

        # Locate and set files directly (Discord uses hidden input[type=file])
        input_loc = page.locator("input[type='file']").first
        try:
            input_loc.set_input_files(file_path, timeout=5000)
        except Exception:
            # Try clicking an upload/add button to reveal file input
            try:
                page.locator("button[aria-label*='Upload'], button[aria-label*='Add']").first.click(timeout=3000)
                page.locator("input[type='file']").first.set_input_files(file_path, timeout=5000)
            except Exception as e:
                raise RuntimeError(f"Discord file upload input not found: {e}")

        # If text provided, send it 
        if text:
            self.action_send_message(agent_id, {"text": text, "timeout_ms": timeout_ms})
        else:
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass

        return {"uploaded": True, "file_path": file_path, "current_url": page.url}

    def action_delete_message(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Delete a previously sent message. Best-effort: targets the last message by this user, or by index.
        params:
          - which: "last" | "last_n" (default "last")
          - n: int (when which is "last_n", delete last n messages; default 1)
        """
        which = (params.get("which") or "last").lower()
        n = int(params.get("n", 1))
        timeout_ms = int(params.get("timeout_ms", 30000))
        self.ensure_open(agent_id, timeout_ms)
        page = self._page(agent_id)

        # Open message context menu (right-click or hover + kebab) then click "Delete Message"
        js = """
        (n) => {
            const root = document.querySelector("ol[data-list-id='chat-messages']") || document;
            const items = Array.from(root.querySelectorAll("li[id]")).slice(-Math.max(1, n));
            if (items.length === 0) return { ok: false, reason: "no_messages" };
            const last = items[items.length - 1];
            last.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, clientX: last.getBoundingClientRect().left + 10, clientY: last.getBoundingClientRect().top + 10 }));
            return { ok: true, count: items.length };
        }
        """
        try:
            page.evaluate(js, n)
            page.wait_for_timeout(400)
            delete_btn = page.locator("div[role='menuitem']:has-text('Delete'), button:has-text('Delete Message'), [id*='delete']").first
            delete_btn.click(timeout=5000)
            page.wait_for_timeout(300)
            confirm = page.locator("button:has-text('Delete'), div[role='dialog'] button:has-text('Delete')").first
            if confirm.count() > 0:
                confirm.click(timeout=3000)
            return {"deleted": True, "which": which, "current_url": page.url}
        except Exception as e:
            return {"deleted": False, "error": str(e), "current_url": page.url}

    def action_reply_message(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Reply to a specific message. Uses "last" as target (reply to the last visible message).
        params:
          - text (required)
          - target: "last" | "last_other" (default "last" = last message in channel)
        """
        text = params["text"]
        target = (params.get("target") or "last").lower()
        timeout_ms = int(params.get("timeout_ms", 30000))
        self.ensure_open(agent_id, timeout_ms)
        page = self._page(agent_id)

        # Click reply on the target message (last message in list)
        try:
            root = page.locator("ol[data-list-id='chat-messages']").first
            last_msg = root.locator("li").last
            last_msg.hover(timeout=timeout_ms)
            page.wait_for_timeout(300)
            reply_btn = last_msg.locator("button[aria-label*='Reply'], button[aria-label*='reply'], [class*='reply']").first
            reply_btn.click(timeout=5000)
            page.wait_for_timeout(500)
        except Exception as e:
            return {"sent": False, "error": f"Could not open reply: {e}", "current_url": page.url}

        box = self._find_message_box(page, timeout_ms)
        if box is None:
            return {"sent": False, "error": "Message box not found after reply", "current_url": page.url}
        box.click(timeout=timeout_ms)
        box.type(text, timeout=timeout_ms)
        page.keyboard.press("Enter")
        return {"sent": True, "len": len(text), "target": target, "current_url": page.url}

    def action_react_message(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Add an emoji reaction to a message. Target: last message by default.
        Uses DOM/HTML analysis to find and click the Add reaction button.
        params:
          - emoji (required): Unicode emoji or short name e.g. "👍", "thumbsup", "❤️"
          - target: "last" (default)
        """
        emoji = params.get("emoji") or params.get("emoji_name")
        if not emoji:
            raise ValueError("discord.react_message requires emoji (or emoji_name)")
        target = (params.get("target") or "last").lower()
        timeout_ms = int(params.get("timeout_ms", 30000))
        self.ensure_open(agent_id, timeout_ms)
        page = self._page(agent_id)

        try:
            # Dismiss overlays (voice panel, modals) so message area is clickable
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)

            # Use JavaScript to read DOM, find last message, hover, and click Add reaction button
            result = page.evaluate(
                """() => {
                // Find message list
                const list = document.querySelector("ol[data-list-id='chat-messages']");
                if (!list) return {success: false, error: 'no message list found'};
                
                const items = Array.from(list.querySelectorAll("li"));
                if (!items.length) return {success: false, error: 'no messages found'};
                
                const last = items[items.length - 1];
                
                // Scroll message into view
                last.scrollIntoView({behavior: 'smooth', block: 'center'});
                
                // Trigger hover to reveal action buttons
                last.dispatchEvent(new MouseEvent('mouseenter', {bubbles: true, cancelable: true}));
                
                // Wait a moment for hover effects (we'll wait in Python)
                return {success: true, messageId: last.id || '', messageHtml: last.outerHTML.substring(0, 500)};
            }"""
            )
            
            if not result.get("success"):
                return {"reacted": False, "error": result.get("error", "Unknown error"), "current_url": page.url}
            
            page.wait_for_timeout(1200)  # Wait for hover effects to show buttons
            
            # Now read DOM again to find Add reaction button after hover
            reaction_result = page.evaluate(
                """() => {
                const list = document.querySelector("ol[data-list-id='chat-messages']");
                if (!list) return {found: false, error: 'no message list'};
                
                const items = Array.from(list.querySelectorAll("li"));
                if (!items.length) return {found: false, error: 'no messages'};
                const last = items[items.length - 1];
                
                // Find Add reaction button by reading DOM structure
                // Strategy 1: aria-label containing "reaction" or "Add"
                let btn = Array.from(last.querySelectorAll("button, [role='button']")).find(el => {
                    const label = (el.getAttribute('aria-label') || '').toLowerCase();
                    return label.includes('reaction') || label.includes('add reaction');
                });
                
                // Strategy 2: Look in action containers (messageActions, buttonGroup, etc.)
                if (!btn) {
                    const actionContainers = last.querySelectorAll("[class*='action'], [class*='buttonGroup'], [class*='buttonContainer']");
                    for (const cont of actionContainers) {
                        const buttons = cont.querySelectorAll("button, [role='button']");
                        for (const b of buttons) {
                            const label = (b.getAttribute('aria-label') || '').toLowerCase();
                            if (label.includes('reaction') || label.includes('add') || !btn) {
                                btn = b;
                                if (label.includes('reaction')) break;
                            }
                        }
                        if (btn && btn.getAttribute('aria-label') && btn.getAttribute('aria-label').toLowerCase().includes('reaction')) break;
                    }
                }
                
                // Strategy 3: First button in message (Discord usually shows Add reaction first)
                if (!btn) {
                    btn = last.querySelector("button, [role='button']");
                }
                
                if (btn) {
                    // Build selector for the button
                    let selector = '';
                    if (btn.id) {
                        selector = '#' + CSS.escape(btn.id);
                    } else {
                        const ariaLabel = btn.getAttribute('aria-label');
                        if (ariaLabel) {
                            selector = `button[aria-label="${CSS.escape(ariaLabel)}"], [role="button"][aria-label="${CSS.escape(ariaLabel)}"]`;
                        } else {
                            // Build path
                            const path = [];
                            let current = btn;
                            while (current && current !== last && path.length < 10) {
                                const tag = current.tagName.toLowerCase();
                                const classes = Array.from(current.classList || []).filter(c => c.length > 0).slice(0, 2);
                                let part = tag;
                                if (classes.length > 0) {
                                    part += '.' + classes.map(c => CSS.escape(c)).join('.');
                                }
                                path.unshift(part);
                                current = current.parentElement;
                            }
                            selector = path.join(' > ');
                        }
                    }
                    
                    btn.scrollIntoView({behavior: 'smooth', block: 'center'});
                    btn.click();
                    return {found: true, selector: selector, ariaLabel: btn.getAttribute('aria-label') || ''};
                }
                
                return {found: false, error: 'no button found in message', debug: {messageId: last.id, buttonCount: last.querySelectorAll('button').length}};
            }"""
            )
            
            if not reaction_result.get("found"):
                return {"reacted": False, "error": reaction_result.get("error", "Could not find Add reaction button"), "debug": reaction_result.get("debug"), "current_url": page.url}
            
            page.wait_for_timeout(1000)  # Wait for emoji picker to open
            
            # Find and interact with emoji picker using DOM analysis
            emoji_result = page.evaluate(
                """(emojiStr) => {
                // Find emoji picker by reading DOM
                const picker = document.querySelector("div[role='menu']")
                    || document.querySelector("[class*='emojiPicker']")
                    || document.querySelector("[class*='emoji-picker']")
                    || document.querySelector("[id*='emoji-picker']");
                
                if (!picker) {
                    return {found: false, error: 'emoji picker not found'};
                }
                
                // Find search input and type emoji (or emoji name for special cases)
                const searchInput = picker.querySelector("input[placeholder*='Search' i]")
                    || picker.querySelector("input[type='text']")
                    || picker.querySelector("input");
                
                if (searchInput) {
                    searchInput.focus();
                    // Map common emojis to search terms
                    let searchTerm = emojiStr;
                    if (emojiStr === '✅' || emojiStr === '✓' || emojiStr === '✔') {
                        searchTerm = 'white check mark';
                    } else if (emojiStr === '👍') {
                        searchTerm = 'thumbs up';
                    } else if (emojiStr === '❤️' || emojiStr === '❤') {
                        searchTerm = 'red heart';
                    } else if (emojiStr === '😀' || emojiStr === '😃') {
                        searchTerm = 'grinning';
                    }
                    searchInput.value = searchTerm;
                    searchInput.dispatchEvent(new Event('input', {bubbles: true}));
                    searchInput.dispatchEvent(new Event('change', {bubbles: true}));
                }
                
                // Wait a moment for search results (we'll wait in Python)
                return {found: true, hasSearchInput: !!searchInput};
            }""",
                str(emoji)
            )
            
            if not emoji_result.get("found"):
                # Do NOT type emoji into message box—that sends it as message text, not as a reaction.
                # Return failure so caller can retry or use a different flow.
                return {"reacted": False, "error": "Emoji picker not found; reaction must be added via Add reaction button and picker, not by typing in message box.", "emoji": str(emoji), "target": target, "current_url": page.url}
            
            page.wait_for_timeout(800)  # Wait for search results
            
            # Find and click emoji button using DOM analysis
            emoji_click_result = page.evaluate(
                """(emojiStr) => {
                const picker = document.querySelector("div[role='menu']")
                    || document.querySelector("[class*='emojiPicker']")
                    || document.querySelector("[class*='emoji-picker']")
                    || document.querySelector("[id*='emoji-picker']");
                
                if (!picker) return {clicked: false, error: 'picker not found'};
                
                // Find emoji button by reading DOM - get all buttons
                const buttons = Array.from(picker.querySelectorAll("button, [role='button']"));
                
                // Normalize emoji string for comparison (handle Unicode variations)
                const normalizeEmoji = (str) => {
                    // Remove variation selectors and zero-width joiners
                    return str.replace(/\\uFE0F/g, '').replace(/\\u200D/g, '').trim();
                };
                const normalizedEmoji = normalizeEmoji(emojiStr);
                
                // Try to find exact match first (by aria-label, text content, or emoji character)
                let emojiBtn = buttons.find(btn => {
                    const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                    const text = (btn.textContent || '').trim();
                    const btnEmoji = normalizeEmoji(text);
                    
                    // Check if aria-label contains emoji name or description
                    if (label.includes(emojiStr.toLowerCase()) || label.includes('thumbs up') || label.includes('check mark') || label.includes('white check mark')) {
                        return true;
                    }
                    // Check if text content matches emoji exactly
                    if (text === emojiStr || btnEmoji === normalizedEmoji) {
                        return true;
                    }
                    // Check if text contains the emoji
                    if (text.includes(emojiStr) || btnEmoji.includes(normalizedEmoji)) {
                        return true;
                    }
                    return false;
                });
                
                // If not found, try first visible button (usually first search result)
                if (!emojiBtn && buttons.length > 0) {
                    // Filter visible buttons
                    const visibleButtons = buttons.filter(btn => {
                        const rect = btn.getBoundingClientRect();
                        const style = window.getComputedStyle(btn);
                        return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                    });
                    if (visibleButtons.length > 0) {
                        emojiBtn = visibleButtons[0];
                    } else {
                        emojiBtn = buttons[0];
                    }
                }
                
                if (emojiBtn) {
                    emojiBtn.scrollIntoView({behavior: 'smooth', block: 'center'});
                    emojiBtn.click();
                    return {clicked: true, ariaLabel: emojiBtn.getAttribute('aria-label') || '', text: emojiBtn.textContent || ''};
                }
                
                return {clicked: false, error: 'no emoji button found', buttonCount: buttons.length};
            }""",
                str(emoji)
            )
            
            page.wait_for_timeout(300)
            
            if emoji_click_result.get("clicked"):
                return {"reacted": True, "emoji": str(emoji), "target": target, "current_url": page.url}
            # Do not use Enter fallback—focus might be in message box and would send message. Only success when emoji button was actually clicked.
            return {"reacted": False, "error": "Could not select emoji in picker; ensure Add reaction was clicked and picker is open.", "emoji": str(emoji), "target": target, "current_url": page.url}
        except Exception as e:
            return {"reacted": False, "error": str(e), "current_url": page.url}

    def execute(self, agent_id: str, action_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        verb = action_name.split(".", 1)[1] if "." in action_name else action_name

        if verb == "open":
            return self.action_open(agent_id, params)
        if verb == "login":
            return self.action_login(agent_id, params)
        if verb == "goto_channel":
            return self.action_goto_channel(agent_id, params)
        if verb == "send_message":
            return self.action_send_message(agent_id, params)
        if verb == "get_latest_messages":
            return self.action_get_latest_messages(agent_id, params)
        if verb == "upload_file":
            return self.action_upload_file(agent_id, params)
        if verb == "delete_message":
            return self.action_delete_message(agent_id, params)
        if verb == "reply_message":
            return self.action_reply_message(agent_id, params)
        if verb == "react_message":
            return self.action_react_message(agent_id, params)

        raise ValueError(f"Unsupported discord action: {action_name}")
