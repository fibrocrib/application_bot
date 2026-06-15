"""Playwright + Claude agent that drives the company careers portal.

Two-phase loop sharing one tool surface:
  Phase A — find the role on the careers page.
  Phase B — fill and submit the application form.

Claude steers via tool calls; we never trust unbounded freeform output."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from .claude_client import MODEL_SMART, client

log = logging.getLogger(__name__)

MAX_STEPS = 28
SCREENSHOT_DIR = Path("screenshots")

TOOLS = [
    {"name": "look",
     "description": "Refresh your view of the current page. Returns URL, title, and a compact list of interactive elements (inputs, buttons, links, selects) with stable selectors.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "click",
     "description": "Click the element matching the given selector. Selector format: 'css=<css>' or 'text=<visible text>'.",
     "input_schema": {"type": "object",
                       "properties": {"selector": {"type": "string"}},
                       "required": ["selector"]}},
    {"name": "fill",
     "description": "Type into an input/textarea matching the selector.",
     "input_schema": {"type": "object",
                       "properties": {"selector": {"type": "string"},
                                      "value": {"type": "string"}},
                       "required": ["selector", "value"]}},
    {"name": "select_option",
     "description": "Pick an option from a <select>. Pass the visible label.",
     "input_schema": {"type": "object",
                       "properties": {"selector": {"type": "string"},
                                      "label": {"type": "string"}},
                       "required": ["selector", "label"]}},
    {"name": "upload",
     "description": "Set a file input. file_kind is 'cv' (CV PDF) or 'statement' (statement as .txt).",
     "input_schema": {"type": "object",
                       "properties": {"selector": {"type": "string"},
                                      "file_kind": {"type": "string",
                                                     "enum": ["cv", "statement"]}},
                       "required": ["selector", "file_kind"]}},
    {"name": "goto",
     "description": "Navigate to a URL within the company careers domain.",
     "input_schema": {"type": "object",
                       "properties": {"url": {"type": "string"}},
                       "required": ["url"]}},
    {"name": "wait",
     "description": "Wait up to 6 seconds for the page to settle.",
     "input_schema": {"type": "object",
                       "properties": {"seconds": {"type": "number"}},
                       "required": ["seconds"]}},
    {"name": "finish",
     "description": "Declare the task complete. status='applied' means the form was submitted; 'skipped' means the role couldn't be found or the page is unsuitable; 'failed' means we got stuck.",
     "input_schema": {"type": "object",
                       "properties": {"status": {"type": "string",
                                                  "enum": ["applied", "skipped", "failed"]},
                                       "note": {"type": "string"}},
                       "required": ["status", "note"]}},
]


DRY_RUN_RULE = """
DRY-RUN MODE IS ACTIVE. Do every step normally — locate the role, open the
application form, fill every field, upload the CV, paste the statement — but
do NOT click the final submit/apply/send button. When the form is fully filled
and the only remaining action is to submit, call `finish` with
status='applied' and note='dry_run: form filled, did not submit'."""


SYSTEM = """You are a job-application agent driving a real web browser via tools.

Your task has two phases:
  Phase A — Locate the matching role on the careers page (use search/filter/links).
  Phase B — Open the application form, fill every required field truthfully using
            the candidate profile, upload the CV, paste the personal statement
            into the cover-letter field, and submit.

Rules:
- Always call `look` first, and again after any navigation or click.
- Never invent answers to free-text questions. If a required question can't be
  answered from the profile + statement, call `finish` with status='skipped'.
- For dropdowns about work authorisation / sponsorship / location, answer
  using the profile fields exactly.
- For voluntary equal-opportunity questions, prefer 'Prefer not to say'.
- If you see a captcha, login wall, or 'create account', call finish status='skipped'.
- If you see a confirmation page ('thank you', 'application received', 'we got it'),
  call finish status='applied'.
- Max ~25 steps total — be efficient. Don't re-read the same page twice in a row."""


@dataclass
class AgentInput:
    careers_url: str
    role_title: str
    company: str
    profile: dict
    cv_path: str
    statement_text: str
    statement_pdf_path: str
    dry_run: bool = False


@dataclass
class AgentOutput:
    status: str  # applied | skipped | failed
    note: str
    role_url: str
    steps_used: int
    screenshots: list[str]


def apply(inp: AgentInput) -> AgentOutput:
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/127.0 Safari/537.36",
            viewport={"width": 1366, "height": 900},
        )
        page = ctx.new_page()
        try:
            page.goto(inp.careers_url, wait_until="domcontentloaded", timeout=30000)
        except PWTimeout:
            return AgentOutput("skipped", "careers page timeout",
                               inp.careers_url, 0, [])

        result = _agent_loop(page, inp)
        browser.close()
        return result


def _agent_loop(page: Page, inp: AgentInput) -> AgentOutput:
    shots: list[str] = []
    profile_lines = "\n".join(f"  {k}: {v}" for k, v in inp.profile.items())
    user_kickoff = (
        f"Company: {inp.company}\n"
        f"Target role: {inp.role_title}\n"
        f"Careers page (already loaded): {inp.careers_url}\n\n"
        f"Profile to use when filling the form:\n{profile_lines}\n\n"
        f"Personal statement to paste into the cover-letter field (also available\n"
        f"as a PDF for upload — call `upload` with file_kind='statement'):\n---\n"
        f"{inp.statement_text}\n---\n\n"
        "Begin by calling `look`."
    )
    messages = [{"role": "user", "content": user_kickoff}]

    final_status = "failed"
    final_note = "max steps reached without finish"
    role_url = inp.careers_url
    steps = 0

    system_prompt = SYSTEM + (DRY_RUN_RULE if inp.dry_run else "")

    for step in range(MAX_STEPS):
        steps = step + 1
        resp = client().messages.create(
            model=MODEL_SMART,
            max_tokens=800,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})

        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        if not tool_uses:
            final_status, final_note = "failed", "claude produced no tool call"
            break

        tool_results: list[dict] = []
        finished = False
        for tu in tool_uses:
            name, args = tu.name, tu.input or {}
            try:
                content = _run_tool(page, inp, name, args)
            except Exception as e:
                content = f"error: {e!s}"

            if name == "finish":
                final_status = args.get("status", "failed")
                final_note = args.get("note", "")
                role_url = page.url
                finished = True
                if inp.dry_run and final_status == "applied":
                    shot = SCREENSHOT_DIR / f"dryrun_{int(time.time())}.png"
                    try:
                        page.screenshot(path=str(shot), full_page=True)
                        shots.append(str(shot))
                    except Exception:
                        pass
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": content,
            })

        messages.append({"role": "user", "content": tool_results})

        if finished:
            break

        if step in (8, 16, 24):
            shot = SCREENSHOT_DIR / f"step_{step}_{int(time.time())}.png"
            try:
                page.screenshot(path=str(shot), full_page=False)
                shots.append(str(shot))
            except Exception:
                pass

    role_url = page.url
    return AgentOutput(final_status, final_note, role_url, steps, shots)


def _run_tool(page: Page, inp: AgentInput, name: str, args: dict) -> str:
    if name == "look":
        return _snapshot(page)
    if name == "click":
        _resolve(page, args["selector"]).click(timeout=8000)
        page.wait_for_load_state("domcontentloaded", timeout=8000)
        return "clicked"
    if name == "fill":
        _resolve(page, args["selector"]).fill(args["value"], timeout=8000)
        return "filled"
    if name == "select_option":
        _resolve(page, args["selector"]).select_option(label=args["label"], timeout=8000)
        return "selected"
    if name == "upload":
        kind = args["file_kind"]
        path = inp.cv_path if kind == "cv" else inp.statement_pdf_path
        _resolve(page, args["selector"]).set_input_files(path, timeout=8000)
        return f"uploaded {kind}"
    if name == "goto":
        url = args["url"]
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        return f"navigated to {url}"
    if name == "wait":
        time.sleep(min(float(args.get("seconds", 1.0)), 6.0))
        page.wait_for_load_state("domcontentloaded", timeout=5000)
        return "waited"
    if name == "finish":
        return "finishing"
    return f"unknown tool: {name}"


def _resolve(page: Page, selector: str):
    if selector.startswith("css="):
        return page.locator(selector[4:])
    if selector.startswith("text="):
        return page.get_by_text(selector[5:], exact=False).first
    return page.locator(selector)


def _snapshot(page: Page) -> str:
    """Compact view of interactive elements on the current page."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=4000)
    except PWTimeout:
        pass

    elements = page.evaluate(
        """() => {
        const out = [];
        const seen = new Set();
        const push = (el, tag, label) => {
            if (out.length > 120) return;
            const rect = el.getBoundingClientRect();
            if (rect.width < 1 || rect.height < 1) return;
            const sel = el.id
                ? `#${el.id}`
                : el.getAttribute('name')
                    ? `[name="${el.getAttribute('name')}"]`
                    : el.getAttribute('data-testid')
                        ? `[data-testid="${el.getAttribute('data-testid')}"]`
                        : null;
            if (!sel) return;
            const k = tag + '|' + sel;
            if (seen.has(k)) return;
            seen.add(k);
            out.push({tag, sel, label: (label||'').slice(0,120)});
        };
        for (const el of document.querySelectorAll('input,textarea,select')) {
            const type = el.getAttribute('type') || el.tagName.toLowerCase();
            let label = el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
            if (!label && el.id) {
                const lbl = document.querySelector(`label[for="${el.id}"]`);
                if (lbl) label = lbl.innerText;
            }
            push(el, type, label);
        }
        for (const el of document.querySelectorAll('button,a,[role=button]')) {
            const txt = (el.innerText || el.getAttribute('aria-label') || '').trim();
            if (txt) push(el, el.tagName.toLowerCase(), txt);
        }
        return out;
    }"""
    )

    lines = [f"URL: {page.url}", f"Title: {page.title()[:120]}", "", "Elements:"]
    for el in elements:
        lines.append(f"  [{el['tag']}] css={el['sel']}  — {el['label']}")
    snap = "\n".join(lines)
    return snap[:7000]
