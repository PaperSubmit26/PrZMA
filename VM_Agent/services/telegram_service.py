# VM_Agent/services/telegram_service.py
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.sync_api import TimeoutError as PWTimeoutError

from .browser_service import BrowserService


class TelegramService:
    """
    Telegram Web automation (web.telegram.org).
    Recommended:
      - Use BrowserService with persistent profile (user_data_dir) and login once manually.
    """

    def __init__(self, browser: BrowserService):
        self.browser = browser

    def _page(self, agent_id: str):
        return self.browser._page(agent_id)

    def ensure_open(self, agent_id: str, variant: str = "k", timeout_ms: int = 30000) -> None:
        page = self._page(agent_id)
        base = "https://web.telegram.org"
        url = f"{base}/{variant}/"
        if "web.telegram.org" not in (page.url or ""):
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

    def action_open(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        variant = (params.get("variant") or "k").lower()  # "k" or "a"
        timeout_ms = int(params.get("timeout_ms", 30000))
        self.ensure_open(agent_id, variant, timeout_ms)
        return {"current_url": self._page(agent_id).url, "variant": variant}

    def action_select_chat(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        params:
          - chat: str (chat name / username)
        """
        import time
        chat = params["chat"]
        timeout_ms = int(params.get("timeout_ms", 30000))
        self.ensure_open(agent_id, (params.get("variant") or "k"), timeout_ms)
        page = self._page(agent_id)

        # Wait for Telegram UI to load (check for main UI elements)
        try:
            # Wait for either chat list or search box to appear
            page.wait_for_selector("#page-chats, #column-left, input[placeholder*='Search'], input[type='text']", timeout=10000)
            time.sleep(1.0)  # Additional wait for UI to stabilize
        except Exception:
            pass  # Continue anyway

        # Try search box with more specific selectors
        candidates = [
            "input[placeholder*='Search']",
            "input[placeholder*='search']",
            ".input-search input",
            "#column-left input[type='text']",
            "input[type='text']",
        ]
        box = None
        for sel in candidates:
            loc = page.locator(sel).first
            try:
                if loc.is_visible(timeout=3000):
                    box = loc
                    break
            except Exception:
                continue

        if box is None:
            # Try clicking search button first (if search box is hidden)
            try:
                search_btn = page.locator("button[aria-label*='Search'], .sidebar-header__btn-container button, .input-search-button").first
                if search_btn.is_visible(timeout=2000):
                    search_btn.click(timeout=5000)
                    time.sleep(0.5)
                    # Retry finding search box
                    for sel in candidates:
                        loc = page.locator(sel).first
                        try:
                            if loc.is_visible(timeout=2000):
                                box = loc
                                break
                        except Exception:
                            continue
            except Exception:
                pass

        if box is None:
            return {"warning": "Search box not found. Ensure Telegram Web is logged in and UI loaded.", "current_url": page.url}

        box.click(timeout=timeout_ms)
        box.fill(chat, timeout=timeout_ms)
        time.sleep(0.5)  # Wait for search results

        # click first result (improved selectors)
        original_url = page.url
        chat_opened = False
        final_chat_url = original_url
        
        try:
            # Try more specific selectors for chat list items
            result_selectors = [
                ".chatlist a",
                ".chatlist .row",
                "div[role='listitem']",
                ".ListItem",
                ".search-super-content-chats a",
                "a[href*='#']",
                ".search-result .ListItem-button",
                ".chat-item-clickable",
                ".left-search-local-suggestion .ListItem-button",
            ]
            for sel in result_selectors:
                try:
                    result = page.locator(sel).first
                    if result.is_visible(timeout=2000):
                        result.click(timeout=timeout_ms)
                        time.sleep(2.0)  # Wait for chat to open
                        # Check if URL changed (chat opened)
                        new_url = page.url
                        if new_url != original_url:
                            final_chat_url = new_url
                            if "/#" in new_url:
                                chat_opened = True
                        break
                except Exception:
                    continue
            if not chat_opened:
                # fallback (press enter)
                page.keyboard.press("Enter")
                time.sleep(2.0)
                new_url = page.url
                if new_url != original_url:
                    final_chat_url = new_url
                    if "/#" in new_url:
                        chat_opened = True
        except Exception:
            # fallback (press enter)
            try:
                page.keyboard.press("Enter")
                time.sleep(2.0)
                new_url = page.url
                if new_url != original_url:
                    final_chat_url = new_url
                    if "/#" in new_url:
                        chat_opened = True
            except Exception:
                pass

        # If URL didn't change, wait a bit more and check again (Telegram Web sometimes delays URL update)
        if not chat_opened:
            time.sleep(3.0)  # Longer wait for Telegram Web to update URL
            final_url = page.url
            final_chat_url = final_url
            if final_url != original_url and "/#" in final_url:
                chat_opened = True
        
        # Also check if message input box is visible (indicates chat is open)
        # If chat is open but URL hasn't updated, use the current URL anyway
        message_input_visible = False
        try:
            message_input_selectors = [
                "div[contenteditable='true']",
                "div[role='textbox']",
                "textarea",
                ".input-message-input",
                "#column-center div[contenteditable='true']",
            ]
            for sel in message_input_selectors:
                try:
                    if page.locator(sel).first.is_visible(timeout=2000):
                        message_input_visible = True
                        chat_opened = True  # If message input is visible, chat is open
                        # Get current URL again (might have updated)
                        final_chat_url = page.url
                        break
                except Exception:
                    continue
        except Exception:
            pass

        # Return the final URL (even if it's the same, Telegram Web might not update URL immediately)
        # But if message input is visible, we know chat is open
        return {"selected": chat, "current_url": final_chat_url, "chat_opened": chat_opened, "message_input_visible": message_input_visible}

    def _select_chat_if_requested(self, agent_id: str, params: Dict[str, Any], timeout_ms: int) -> None:
        if params.get("chat"):
            self.action_select_chat(
                agent_id,
                {"chat": params["chat"], "timeout_ms": timeout_ms, "variant": params.get("variant", "k")},
            )

    def _find_message_box(self, page, timeout_ms: int):
        candidates = [
            "#column-center div[contenteditable='true']",
            "div.input-message-input[contenteditable='true']",
            "div[contenteditable='true'][role='textbox']",
            "div[contenteditable='true']",
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

    def _latest_message_point(self, page) -> Optional[Dict[str, float]]:
        return page.evaluate(
            """() => {
            const selectors = [
                "#column-center .message",
                "#column-center [class*='Message']",
                ".bubbles .bubble",
                ".bubble",
                "[data-message-id]",
                "[id^='message']"
            ];
            let nodes = [];
            for (const sel of selectors) {
                nodes = Array.from(document.querySelectorAll(sel)).filter(el => {
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                });
                if (nodes.length) break;
            }
            if (!nodes.length) return null;
            const el = nodes[nodes.length - 1];
            el.scrollIntoView({behavior: "instant", block: "center"});
            const rect = el.getBoundingClientRect();
            return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
        }"""
        )

    def action_get_latest_messages(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Best-effort read of recent visible Telegram Web messages.
        params:
          - limit: optional, default 10
          - chat: optional, select chat before reading
        """
        limit = int(params.get("limit", 10))
        timeout_ms = int(params.get("timeout_ms", 30000))
        variant = (params.get("variant") or "k").lower()
        self.ensure_open(agent_id, variant, timeout_ms)
        page = self._page(agent_id)
        self._select_chat_if_requested(agent_id, params, timeout_ms)

        messages: List[Dict[str, Any]] = page.evaluate(
            """(limit) => {
            const selectors = [
                "#column-center .message",
                "#column-center [class*='Message']",
                ".bubbles .bubble",
                ".bubble",
                "[data-message-id]",
                "[id^='message']"
            ];
            let nodes = [];
            for (const sel of selectors) {
                nodes = Array.from(document.querySelectorAll(sel)).filter(el => {
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                });
                if (nodes.length) break;
            }
            const picked = nodes.slice(-Math.max(1, limit));
            return picked.map((el, idx) => {
                const authorEl =
                    el.querySelector(".message-title")
                    || el.querySelector("[class*='sender']")
                    || el.querySelector("[class*='author']");
                const textEl =
                    el.querySelector(".text-content")
                    || el.querySelector("[class*='text-content']")
                    || el.querySelector("[class*='message']")
                    || el;
                const timeEl = el.querySelector("time") || el.querySelector("[class*='time']");
                const attachments = [];
                const controls = Array.from(el.querySelectorAll("a[href], button, [role='button']"));
                for (const control of controls) {
                    const href = control.getAttribute("href") || "";
                    const label = (control.getAttribute("aria-label") || control.getAttribute("title") || control.textContent || "").trim();
                    const lower = label.toLowerCase();
                    if (!href && !lower.includes("download") && !lower.includes("save") && !lower.includes("document") && !lower.includes("file")) {
                        continue;
                    }
                    const rect = control.getBoundingClientRect();
                    const filename = (() => {
                        if (label && !lower.includes("download") && !lower.includes("save")) return label;
                        try {
                            const clean = href.split("?")[0];
                            const last = clean.split("/").filter(Boolean).pop();
                            return last ? decodeURIComponent(last) : "";
                        } catch (_) {
                            return "";
                        }
                    })();
                    attachments.push({
                        attachment_index: attachments.length,
                        filename,
                        label,
                        href,
                        downloadable: true,
                        x: rect.left + rect.width / 2,
                        y: rect.top + rect.height / 2
                    });
                }
                return {
                    index: idx,
                    message_index: idx,
                    author: authorEl ? (authorEl.textContent || "").trim() : null,
                    text: textEl ? (textEl.textContent || "").trim() : "",
                    ts: timeEl ? ((timeEl.getAttribute("datetime") || timeEl.textContent || "").trim()) : null,
                    attachments: attachments.map((att, attachment_index) => ({
                        ...att,
                        attachment_index,
                        attachment_id: `telegram_att_${idx}_${attachment_index}`
                    }))
                };
            }).filter(x => x.text || (x.attachments && x.attachments.length));
        }""",
            limit,
        )
        attachment_count = sum(len(m.get("attachments") or []) for m in messages if isinstance(m, dict))
        return {"messages": messages, "count": len(messages), "attachment_count": attachment_count, "current_url": page.url}

    def action_send_message(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        params:
          - chat (optional): if provided, will select chat first
          - text (required)
        """
        text = params["text"]
        timeout_ms = int(params.get("timeout_ms", 30000))
        self.ensure_open(agent_id, (params.get("variant") or "k"), timeout_ms)
        page = self._page(agent_id)

        self._select_chat_if_requested(agent_id, params, timeout_ms)
        box = self._find_message_box(page, timeout_ms)

        if box is None:
            raise RuntimeError("Telegram message box not found. Make sure a chat is open and you are logged in.")

        box.click(timeout=timeout_ms)
        box.type(text, timeout=timeout_ms)
        page.keyboard.press("Enter")
        return {"sent": True, "len": len(text), "current_url": page.url}

    def action_reply_message(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        text = params["text"]
        timeout_ms = int(params.get("timeout_ms", 30000))
        variant = (params.get("variant") or "k").lower()
        self.ensure_open(agent_id, variant, timeout_ms)
        page = self._page(agent_id)
        self._select_chat_if_requested(agent_id, params, timeout_ms)

        point = self._latest_message_point(page)
        if not point:
            return {"sent": False, "error": "No visible Telegram message found.", "current_url": page.url}

        try:
            page.mouse.click(float(point["x"]), float(point["y"]), button="right")
            page.wait_for_timeout(500)
            reply = page.locator("[role='menuitem']:has-text('Reply'), .MenuItem:has-text('Reply'), button:has-text('Reply')").first
            reply.click(timeout=5000)
        except Exception:
            try:
                page.mouse.click(float(point["x"]), float(point["y"]))
                page.keyboard.press("Control+ArrowUp")
            except Exception as e:
                return {"sent": False, "error": f"Could not open Telegram reply UI: {e}", "current_url": page.url}

        box = self._find_message_box(page, timeout_ms)
        if box is None:
            return {"sent": False, "error": "Message box not found after opening reply UI.", "current_url": page.url}
        box.click(timeout=timeout_ms)
        box.type(text, timeout=timeout_ms)
        page.keyboard.press("Enter")
        return {"sent": True, "len": len(text), "target": "last", "current_url": page.url}

    def action_react_message(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        emoji = params["emoji"]
        timeout_ms = int(params.get("timeout_ms", 30000))
        variant = (params.get("variant") or "k").lower()
        self.ensure_open(agent_id, variant, timeout_ms)
        page = self._page(agent_id)
        self._select_chat_if_requested(agent_id, params, timeout_ms)

        point = self._latest_message_point(page)
        if not point:
            return {"reacted": False, "error": "No visible Telegram message found.", "current_url": page.url}

        try:
            page.mouse.click(float(point["x"]), float(point["y"]), button="right")
            page.wait_for_timeout(500)
            react = page.locator(
                "[role='menuitem']:has-text('React'), .MenuItem:has-text('React'), button[aria-label*='React'], button:has-text('React')"
            ).first
            react.click(timeout=5000)
            page.wait_for_timeout(500)
        except Exception:
            try:
                page.mouse.dblclick(float(point["x"]), float(point["y"]))
                return {"reacted": True, "emoji": emoji, "target": "last", "fallback": "double_click", "current_url": page.url}
            except Exception as e:
                return {"reacted": False, "error": f"Could not open Telegram reaction UI: {e}", "current_url": page.url}

        try:
            result = page.evaluate(
                """(emoji) => {
                const buttons = Array.from(document.querySelectorAll("button, [role='button'], [role='menuitem']"));
                let btn = buttons.find(el => (el.textContent || "").includes(emoji));
                if (!btn) {
                    btn = buttons.find(el => {
                        const label = (el.getAttribute("aria-label") || el.getAttribute("title") || "").toLowerCase();
                        return label.includes("reaction") || label.includes("emoji");
                    });
                }
                if (!btn) return {clicked: false};
                btn.click();
                return {clicked: true, text: btn.textContent || "", label: btn.getAttribute("aria-label") || ""};
            }""",
                str(emoji),
            )
            if result.get("clicked"):
                return {"reacted": True, "emoji": emoji, "target": "last", "current_url": page.url}
            return {"reacted": False, "error": "Reaction picker opened but emoji button was not found.", "emoji": emoji, "current_url": page.url}
        except Exception as e:
            return {"reacted": False, "error": str(e), "emoji": emoji, "current_url": page.url}

    def action_delete_message(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        timeout_ms = int(params.get("timeout_ms", 30000))
        variant = (params.get("variant") or "k").lower()
        self.ensure_open(agent_id, variant, timeout_ms)
        page = self._page(agent_id)
        self._select_chat_if_requested(agent_id, params, timeout_ms)

        point = self._latest_message_point(page)
        if not point:
            return {"deleted": False, "error": "No visible Telegram message found.", "current_url": page.url}

        try:
            page.mouse.click(float(point["x"]), float(point["y"]), button="right")
            page.wait_for_timeout(500)
            delete = page.locator(
                "[role='menuitem']:has-text('Delete'), .MenuItem:has-text('Delete'), button:has-text('Delete')"
            ).first
            delete.click(timeout=5000)
            page.wait_for_timeout(500)
            confirm = page.locator("button:has-text('Delete'), .confirm-dialog button:has-text('Delete')").last
            if confirm.count() > 0:
                confirm.click(timeout=5000)
            return {"deleted": True, "target": "last", "current_url": page.url}
        except Exception as e:
            return {"deleted": False, "error": str(e), "current_url": page.url}

    def action_upload_file(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        params:
          - file_path (required)
          - chat (optional)
          - message (optional)
        """
        file_path = params["file_path"]
        timeout_ms = int(params.get("timeout_ms", 30000))
        self.ensure_open(agent_id, (params.get("variant") or "k"), timeout_ms)
        page = self._page(agent_id)

        if not os.path.exists(file_path):
            raise FileNotFoundError(file_path)

        self._select_chat_if_requested(agent_id, params, timeout_ms)

        # Telegram uses hidden input[type=file] after clicking attach.
        try:
            # try direct
            page.locator("input[type='file']").first.set_input_files(file_path, timeout=5000)
        except Exception:
            # click attach icon (best-effort)
            try:
                page.locator("button[aria-label*='Attach'], button[title*='Attach'], .attach, .Button.Attach").first.click(timeout=3000)
                page.locator("input[type='file']").first.set_input_files(file_path, timeout=5000)
            except Exception as e:
                raise RuntimeError(f"Telegram file upload failed: {e}")

        if params.get("message"):
            self.action_send_message(agent_id, {"text": params["message"], "timeout_ms": timeout_ms, "variant": params.get("variant", "k")})
        else:
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass

        return {"uploaded": True, "file_path": file_path, "current_url": page.url}

    def action_download_file(self, agent_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        timeout_ms = int(params.get("timeout_ms", 30000))
        variant = (params.get("variant") or "k").lower()
        self.ensure_open(agent_id, variant, timeout_ms)
        page = self._page(agent_id)
        self._select_chat_if_requested(agent_id, params, timeout_ms)

        download_dir = Path(params.get("download_dir") or os.path.join("C:\\", "PrZMA", "downloads", agent_id, "telegram"))
        download_dir.mkdir(parents=True, exist_ok=True)

        latest = self.action_get_latest_messages(agent_id, {"limit": 50, "timeout_ms": timeout_ms, "variant": variant})
        candidates: List[Dict[str, Any]] = []
        for msg in latest.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            for att in msg.get("attachments") or []:
                if isinstance(att, dict):
                    merged = dict(att)
                    merged["message_index"] = msg.get("message_index")
                    merged["message_text"] = msg.get("text")
                    merged["author"] = msg.get("author")
                    candidates.append(merged)

        attachment_id = params.get("attachment_id")
        message_index = params.get("message_index")
        attachment_index = params.get("attachment_index")
        candidate = None
        for item in candidates:
            if attachment_id and item.get("attachment_id") == attachment_id:
                candidate = item
                break
            if message_index is not None and attachment_index is not None:
                if int(item.get("message_index", -1)) == int(message_index) and int(item.get("attachment_index", -1)) == int(attachment_index):
                    candidate = item
                    break
        if candidate is None and candidates:
            candidate = candidates[-1]
        if not candidate:
            return {"downloaded": False, "error": "No visible Telegram downloadable control found.", "current_url": page.url}

        try:
            with page.expect_download(timeout=timeout_ms) as download_info:
                page.mouse.click(float(candidate["x"]), float(candidate["y"]))
            download = download_info.value
            suggested = download.suggested_filename or "telegram_download"
            save_path = download_dir / suggested
            download.save_as(str(save_path))
            return {
                "downloaded": True,
                "file_path": str(save_path),
                "suggested_filename": suggested,
                "source_href": candidate.get("href"),
                "current_url": page.url,
            }
        except Exception as first_error:
            href = candidate.get("href")
            if href:
                try:
                    response = page.context.request.get(href, timeout=timeout_ms)
                    if not response.ok:
                        raise RuntimeError(f"HTTP {response.status}: {response.status_text}")
                    filename = Path(href.split("?", 1)[0]).name or "telegram_download"
                    save_path = download_dir / filename
                    save_path.write_bytes(response.body())
                    return {
                        "downloaded": True,
                        "file_path": str(save_path),
                        "suggested_filename": filename,
                        "source_href": href,
                        "fallback": "context_request",
                        "current_url": page.url,
                    }
                except Exception as second_error:
                    return {
                        "downloaded": False,
                        "error": f"Download click failed: {first_error}; href fallback failed: {second_error}",
                        "source_href": href,
                        "current_url": page.url,
                    }
            return {"downloaded": False, "error": str(first_error), "current_url": page.url}

    def execute(self, agent_id: str, action_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        verb = action_name.split(".", 1)[1] if "." in action_name else action_name

        if verb == "open":
            return self.action_open(agent_id, params)
        if verb == "select_chat":
            return self.action_select_chat(agent_id, params)
        if verb == "send_message":
            return self.action_send_message(agent_id, params)
        if verb == "get_latest_messages":
            return self.action_get_latest_messages(agent_id, params)
        if verb == "reply_message":
            return self.action_reply_message(agent_id, params)
        if verb == "react_message":
            return self.action_react_message(agent_id, params)
        if verb == "delete_message":
            return self.action_delete_message(agent_id, params)
        if verb == "upload_file":
            return self.action_upload_file(agent_id, params)
        if verb == "download_file":
            return self.action_download_file(agent_id, params)

        raise ValueError(f"Unsupported telegram action: {action_name}")
