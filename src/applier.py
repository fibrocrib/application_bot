"""Form-filling agent built on claude-agent-sdk + async Playwright.

The SDK spawns the `claude` CLI as a subprocess, which authenticates via
CLAUDE_CODE_OAUTH_TOKEN and bills against the Max plan. Tools are defined
in-process and called by the agent as MCP tools."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                              ResultMessage, create_sdk_mcp_server, query, tool)
from playwright.async_api import (Page, TimeoutError as PWTimeout,
                                  async_playwright)

log = logging.getLogger(__name__)

# Real ATS application forms (Ashby, Greenhouse, Lever, Workday) need ~25-45
# productive turns including page snapshots between actions. 28 was hitting
# the cap on even clean Ashby forms; 60 gives real headroom for multi-page
# Workday-style flows while still bounding cost.
MAX_TURNS = 60
SCREENSHOT_DIR = Path("screenshots")


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
    screenshots: list[str] = field(default_factory=list)


SYSTEM = """You are a job-application agent driving a real web browser via tools.

Your task has two phases:
  Phase A — Locate the matching role on the careers page (use search/filter/links).
  Phase B — Open the application form, fill every required field truthfully using
            the candidate profile, upload the CV, paste the personal statement
            into the cover-letter field, and submit.

Rules:
- Always call `look` first, and again after any navigation or click.
- If a `look` result starts with `DEAD_PAGE:` (page is a 404, "no openings",
  "no longer available", "0 jobs found", etc.), immediately call
  `finish` with status='skipped' and note='role unavailable: <reason>'.
  Do not try to navigate further — the role is gone.
- Never invent answers to free-text questions. If a required question can't be
  answered from the profile + statement, call `finish` with status='skipped'.
- For dropdowns about work authorisation / sponsorship / location, answer
  using the profile fields exactly.
- For voluntary equal-opportunity questions, prefer 'Prefer not to say'.
- If you see a captcha, login wall, or 'create account', call finish status='skipped'.
- If you see a confirmation page ('thank you', 'application received', 'we got it'),
  call finish status='applied'.
- Be efficient — don't re-read the same page twice in a row.
- SUBMIT RULE: once every required field is filled and the only remaining
  action is to click a submit/apply/send button, your VERY NEXT action MUST
  be that click — do NOT call `look` again first. In dry-run mode, call
  `finish` with status='applied' instead of clicking submit."""


DRY_RUN_RULE = """

DRY-RUN MODE IS ACTIVE. Do every step normally — locate the role, open the
application form, fill every field, upload the CV, paste the statement — but
do NOT click the final submit/apply/send button. When the form is fully filled
and the only remaining action is to submit, call `finish` with
status='applied' and note='dry_run: form filled, did not submit'."""


def apply(inp: AgentInput) -> AgentOutput:
    """Sync entry point — runs the async agent loop and returns the result."""
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    return asyncio.run(_run(inp))


async def _run(inp: AgentInput) -> AgentOutput:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/127.0 Safari/537.36",
            viewport={"width": 1366, "height": 900},
        )
        page = await ctx.new_page()
        try:
            await page.goto(inp.careers_url, wait_until="domcontentloaded", timeout=30000)
        except PWTimeout:
            await browser.close()
            return AgentOutput("skipped", "careers page timeout",
                               inp.careers_url, 0)

        dead = await _detect_dead_page(page)
        if dead:
            await browser.close()
            return AgentOutput("skipped", f"role unavailable: {dead}",
                               inp.careers_url, 0)

        state = _State(page=page, inp=inp)
        server = _build_tool_server(state)
        sys_prompt = SYSTEM + (DRY_RUN_RULE if inp.dry_run else "")

        opts = ClaudeAgentOptions(
            system_prompt=sys_prompt,
            mcp_servers={"appbot": server},
            allowed_tools=[
                "mcp__appbot__look",
                "mcp__appbot__click",
                "mcp__appbot__fill",
                "mcp__appbot__select_option",
                "mcp__appbot__upload",
                "mcp__appbot__goto",
                "mcp__appbot__wait",
                "mcp__appbot__finish",
            ],
            permission_mode="bypassPermissions",
            model="sonnet",
            max_turns=MAX_TURNS,
        )

        try:
            async for msg in query(prompt=_kickoff(inp), options=opts):
                if isinstance(msg, AssistantMessage):
                    for block in getattr(msg, "content", []) or []:
                        name = getattr(block, "name", None)
                        if name:  # ToolUseBlock
                            args = getattr(block, "input", {}) or {}
                            log.debug("    tool %s %s",
                                      name.split("__")[-1],
                                      {k: str(v)[:80] for k, v in args.items()})
                if isinstance(msg, ResultMessage):
                    state.steps_used = getattr(msg, "num_turns", state.steps_used)
        except Exception as e:
            log.warning("agent session error: %s", e)
            if state.final_status == "failed" and state.final_note.startswith("agent did not"):
                state.final_note = f"session error: {e}"
            # Capture where the agent got stuck so we have forensics next time.
            try:
                shot = SCREENSHOT_DIR / f"sessionerr_{int(time.time())}.png"
                await page.screenshot(path=str(shot), full_page=True)
                state.shots.append(str(shot))
            except Exception:
                pass

        try:
            state.role_url = page.url
        finally:
            await browser.close()
        return AgentOutput(
            status=state.final_status,
            note=state.final_note,
            role_url=state.role_url,
            steps_used=state.steps_used,
            screenshots=state.shots,
        )


class _State:
    def __init__(self, page: Page, inp: AgentInput):
        self.page = page
        self.inp = inp
        self.final_status = "failed"
        self.final_note = "agent did not call finish"
        self.role_url = inp.careers_url
        self.shots: list[str] = []
        self.steps_used = 0


def _kickoff(inp: AgentInput) -> str:
    profile_lines = "\n".join(f"  {k}: {v}" for k, v in inp.profile.items())
    return (
        f"Company: {inp.company}\n"
        f"Target role: {inp.role_title}\n"
        f"Careers page (already loaded): {inp.careers_url}\n\n"
        f"Profile to use when filling the form:\n{profile_lines}\n\n"
        f"Personal statement to paste into the cover-letter field (also available\n"
        f"as a PDF for upload — call `upload` with file_kind='statement'):\n---\n"
        f"{inp.statement_text}\n---\n\n"
        "Begin by calling `look`."
    )


def _build_tool_server(state: _State):
    @tool("look",
          "Refresh your view of the current page. Returns URL, title, and a "
          "compact list of interactive elements (inputs, buttons, links, "
          "selects) with stable selectors.",
          {})
    async def look(args):
        snap = await _snapshot(state.page)
        return {"content": [{"type": "text", "text": snap}]}

    @tool("click",
          "Click the element matching the selector. Format: 'css=<css>' or "
          "'text=<visible text>'.",
          {"selector": str})
    async def click(args):
        try:
            await _resolve(state.page, args["selector"]).click(timeout=8000)
            try:
                await state.page.wait_for_load_state("domcontentloaded", timeout=8000)
            except PWTimeout:
                pass
            return {"content": [{"type": "text", "text": "clicked"}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"click error: {e}"}],
                    "is_error": True}

    @tool("fill",
          "Type into an input/textarea matching the selector.",
          {"selector": str, "value": str})
    async def fill(args):
        try:
            await _resolve(state.page, args["selector"]).fill(args["value"], timeout=8000)
            return {"content": [{"type": "text", "text": "filled"}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"fill error: {e}"}],
                    "is_error": True}

    @tool("select_option",
          "Pick an option from a <select>. Pass the visible label.",
          {"selector": str, "label": str})
    async def select_option(args):
        try:
            await _resolve(state.page, args["selector"]).select_option(
                label=args["label"], timeout=8000,
            )
            return {"content": [{"type": "text", "text": "selected"}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"select error: {e}"}],
                    "is_error": True}

    @tool("upload",
          "Set a file input. file_kind is 'cv' (CV PDF) or 'statement' "
          "(cover letter PDF).",
          {"selector": str, "file_kind": str})
    async def upload(args):
        try:
            kind = args["file_kind"]
            path = state.inp.cv_path if kind == "cv" else state.inp.statement_pdf_path
            await _resolve(state.page, args["selector"]).set_input_files(path, timeout=8000)
            return {"content": [{"type": "text", "text": f"uploaded {kind}"}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"upload error: {e}"}],
                    "is_error": True}

    @tool("goto",
          "Navigate to a URL within the company careers domain.",
          {"url": str})
    async def goto(args):
        try:
            await state.page.goto(args["url"], wait_until="domcontentloaded", timeout=20000)
            return {"content": [{"type": "text", "text": f"navigated to {args['url']}"}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"goto error: {e}"}],
                    "is_error": True}

    @tool("wait",
          "Wait up to 6 seconds for the page to settle.",
          {"seconds": float})
    async def wait(args):
        await asyncio.sleep(min(float(args.get("seconds", 1.0)), 6.0))
        try:
            await state.page.wait_for_load_state("domcontentloaded", timeout=5000)
        except PWTimeout:
            pass
        return {"content": [{"type": "text", "text": "waited"}]}

    @tool("finish",
          "Declare the task complete. status='applied' means the form was "
          "submitted (or fully filled in dry-run mode); 'skipped' means the "
          "role couldn't be found or the page is unsuitable; 'failed' means "
          "the agent got stuck.",
          {"status": str, "note": str})
    async def finish(args):
        state.final_status = args.get("status", "failed")
        state.final_note = args.get("note", "")
        state.role_url = state.page.url
        if state.inp.dry_run and state.final_status == "applied":
            shot = SCREENSHOT_DIR / f"dryrun_{int(time.time())}.png"
            try:
                await state.page.screenshot(path=str(shot), full_page=True)
                state.shots.append(str(shot))
            except Exception:
                pass
        return {"content": [{"type": "text", "text": "finishing"}]}

    return create_sdk_mcp_server(
        name="appbot",
        version="1.0.0",
        tools=[look, click, fill, select_option, upload, goto, wait, finish],
    )


def _resolve(page: Page, selector: str):
    if selector.startswith("css="):
        return page.locator(selector[4:])
    if selector.startswith("text="):
        return page.get_by_text(selector[5:], exact=False).first
    return page.locator(selector)


# Phrases that mean "this page is dead — there's nothing for you to apply to
# here." Ashby/Greenhouse/Workday all return 200 OK with these in the body
# when a posting has been pulled, so we have to text-match rather than rely
# on status codes.
_DEAD_PAGE_MARKERS = (
    "page not found",
    "page you requested was not found",
    "this job is no longer available",
    "this position has been filled",
    "this posting has been closed",
    "no longer accepting applications",
    "0 jobs found",
    "no openings",
    "no results found",
    "there are no job openings at this time",
)


async def _detect_dead_page(page: Page) -> str | None:
    """Return a short reason string if the current page looks like a stale /
    pulled / 404 role page, else None."""
    try:
        body = await page.evaluate(
            "() => (document.body && document.body.innerText) ? "
            "document.body.innerText.slice(0, 4000).toLowerCase() : ''"
        )
    except Exception:
        return None
    for marker in _DEAD_PAGE_MARKERS:
        if marker in body:
            return marker
    return None


async def _snapshot(page: Page) -> str:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=4000)
    except PWTimeout:
        pass

    dead = await _detect_dead_page(page)
    if dead:
        return (
            f"DEAD_PAGE: {dead}\n"
            f"URL: {page.url}\n"
            f"Title: {(await page.title())[:120]}\n"
            "The page indicates the role is unavailable. Call "
            "finish(status='skipped', note='role unavailable: " + dead + "') now."
        )

    elements = await page.evaluate(
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

    title = await page.title()
    lines = [f"URL: {page.url}", f"Title: {title[:120]}", "", "Elements:"]
    for el in elements:
        lines.append(f"  [{el['tag']}] css={el['sel']}  — {el['label']}")
    snap = "\n".join(lines)
    return snap[:7000]
