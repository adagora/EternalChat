"""Application core: one never-ending chat answered by fresh, view-based turns.

Engine owns turn orchestration, the zoom and date tools, steering and named
subagents. It reaches persistence and models only through the store and
provider it is given (see app.open_chat), the computer only through the machine
it may be given (shell, read, write, edit), and user interfaces reach the chat
only through Engine's public methods.
"""
import asyncio
import json
import random
from collections import deque
from dataclasses import dataclass

from .compaction import Compactor
from .config import Config, clip_output, utc_now
from .prompt import (AGENT_NAMES, BOSS_NAMES, DEVICE_NAMES, GOAL_NO, GOAL_SYSTEM, image_block, request_text_bytes,
                     system_prompt, text_block, tools_for, view_blocks)
from .tree import address, children, key_for
from .view import View

INTERRUPTED = "\n[interrupted: this reply did not finish]"
SPAWN_MAX = 8  # agents one spawn call may start, and at work at once: a model that spawns from
               # its agents' reports stops here instead of spending the account
HISTORY_CHARS = 60_000  # of an agent's own transcript, given back when it is told more
GOAL_NUDGES = 30  # turns a goal may ask for in a row before it waits for the user
GOAL_LATEST = 60_000  # characters of the latest messages a goal check reads whole


@dataclass(frozen=True)
class Retrieval:
    """What zoom or date found: text, plus references to original images."""
    text: str
    images: tuple = ()


@dataclass
class Input:
    kind: str
    text: str
    attachments: list
    ids: list
    future: asyncio.Future


def _consume(future):
    """Done-callback for fire-and-forget futures; errors were already reported."""
    if not future.cancelled():
        future.exception()


class Engine:
    def __init__(self, store, provider=None, config=None, on_event=None, machine=None):
        self.config = config or Config()
        self.store = store
        self.provider = provider
        self.machine = machine
        self.tools = tools_for(machine is not None, boss=True)
        self.agent_tools = tools_for(machine is not None)
        self.on_event = on_event or (lambda *args: None)
        self.usage = {"turns": {}, "agents": {}, "summaries": {}}
        try:
            self.view = View(store, self.config)
            self.compactor = Compactor(store, self.view, provider, self.config, self._summary_event)
            self.compactor.tools = self.tools  # one tool list: turns and summaries share the cached prefix
            if not store.readonly:
                self.view.advance()  # checkpoint built leaves that a crash left out of the view
        except BaseException:
            store.close()
            raise
        self.inbox = deque()
        self.turn_task = None
        self.agents = {}
        self.agent_inbox = {}  # name -> messages told to a running agent, read between its tool calls
        self.goal = (store.read_state("goal") or {}).get("text")  # what the chat works toward (/goal)
        self._nudges = 0  # turns the goal asked for since the user last wrote
        self.closed = False
        self._reports_resumed = False

    @property
    def readonly(self):
        return self.store.readonly

    def _check_open(self):
        if self.closed:
            raise RuntimeError("Chat is closed")
        if self.readonly:
            raise RuntimeError("This chat is open read-only")

    def _add_usage(self, bucket, usage):
        totals = self.usage[bucket]
        for key, count in usage.items():
            if isinstance(count, int):
                totals[key] = totals.get(key, 0) + count

    def _summary_event(self, kind, value):
        if kind == "usage":
            self._add_usage("summaries", value)
            self.on_event("usage", {"source": "summary", **value})
        else:
            self.on_event(kind, value)

    # Inputs ----------------------------------------------------------------------

    def record(self, kind, text, *, attachments=None, event_id=None):
        """Log a message and queue its summary; returns its permanent IDs."""
        ids = self.store.append(kind, text, attachments=attachments, event_id=event_id)
        self.compactor.enqueue(ids)
        self.compactor.start()
        self.on_event("message", {"ids": ids})
        return ids

    def imported(self):
        """{session key: its last date imported}, for history.find's `done`."""
        return dict(self.store.read_state("imported") or {})

    def import_chats(self, sessions):
        """Append past chats: each session (history.Session) as its note, then
        its messages with their own dates. Its last date is saved as each one
        lands, so an import cut short goes on where it stopped. Returns how many
        messages were added; their summaries build in the background, and the
        next reply waits for them."""
        self._check_open()
        done = self.imported()
        ids = []
        for session in sessions:
            ids += self.store.append("note", session.note, date=session.start)
            for kind, text, date in session.messages:
                ids += self.store.append(kind, text, date=date)
            done[session.key] = max([session.start] + [date for _, _, date in session.messages])
            self.store.write_state("imported", done)
        if ids:
            self.compactor.enqueue(ids)
            self.compactor.start()
            self.on_event("message", {"ids": [ids[0], ids[-1]], "imported": len(ids)})
        return len(ids)

    def note(self, text):
        """Save a memory without asking for a reply."""
        self._check_open()
        self.compactor.retry()
        return self.record("note", text)

    def attach(self, path):
        """Save an image for a later message; returns its asset reference."""
        self._check_open()
        return self.store.add_image(path)

    def submit(self, text, *, kind="user", attachments=None, event_id=None):
        """Log input at once and return a future for the reply.

        Idle input starts a turn. Input that arrives during a turn joins it after
        the next tool results, or starts its own turn when the reply ends.
        """
        self._check_open()
        self.resume_reports()
        if kind == "user":
            self._nudges = 0  # the user's own words: the goal may ask for turns again
        return self._enqueue(text, kind=kind, attachments=attachments, event_id=event_id)

    def _enqueue(self, text, *, kind="user", attachments=None, event_id=None):
        # Log before anything else can fail. Its first ID is the turn's historical
        # cutoff, so logging first never puts the input into its own view.
        ids = self.record(kind, text, attachments=attachments, event_id=event_id)
        self.compactor.retry()
        item = Input(kind, text, list(attachments or []), ids, asyncio.get_running_loop().create_future())
        self.inbox.append(item)
        if self.turn_task is None or self.turn_task.done():
            self.turn_task = asyncio.create_task(self._drive())
        return item.future

    async def ask(self, text, **kwargs):
        return await self.submit(text, **kwargs)

    def compact(self):
        """Retry failed summaries and resume building; finish() waits for them."""
        self._check_open()
        self.compactor.retry()
        self.compactor.start()

    # Turns -----------------------------------------------------------------------

    async def _drive(self):
        while self.inbox and not self.closed:
            item = self.inbox.popleft()
            delivered = [item]
            try:
                result = await self._turn(item, delivered)
            except asyncio.CancelledError:
                for entry in delivered:
                    entry.future.cancel()
                raise
            except Exception as error:
                for entry in delivered:
                    if not entry.future.done():
                        entry.future.set_exception(error)
                self.on_event("error", str(error))
            else:
                for entry in delivered:
                    if not entry.future.done():
                        entry.future.set_result(result)
                if self.goal and not self.inbox and not self.closed:
                    await self._check_goal()

    # Goal ------------------------------------------------------------------------

    def set_goal(self, text):
        """Work toward `text` until a check after a turn finds it met: the goal is
        saved, then sent as a message. Returns that message's reply future."""
        self._check_open()
        if not isinstance(text, str) or not text.strip():
            raise ValueError("A goal needs its text: /goal what to achieve")
        self._save_goal(text.strip())
        return self.submit(text.strip())

    def halt(self):
        """Drop the goal; a reply in progress goes on."""
        self._check_open()
        self._save_goal(None)

    def _save_goal(self, text):
        self.goal, self._nudges = text, 0
        self.store.write_state("goal", {"text": text})
        self.on_event("goal", {"text": text})

    async def _check_goal(self):
        """Ask, without tools, whether the chat shows the goal met: met, it ends
        with a note; unmet, a message asks for another turn, up to GOAL_NUDGES in a
        row, then the goal waits for the user's next message."""
        goal = self.goal
        lines = "".join(f"{m['i']}|{m['kind']}: {m['text']}\n"
                        for m in self.messages(self.view.covered, self.store.message_count))
        content = view_blocks(self.view.lines()) + [text_block(
            "Latest messages whole (i|kind: text):\n" + clip_output(lines, GOAL_LATEST) + "\nThe goal:\n\n" + goal)]

        def receive(kind, value):
            if kind == "usage":
                self._add_usage("turns", value)
        try:
            response = await self.provider.complete(
                model=self.config.turn_model, system=GOAL_SYSTEM, tools=[], max_tokens=1024,
                messages=[{"role": "user", "content": content}], on_event=receive, purpose="turn")
        except Exception as error:  # a failed check ends the work; the goal stays
            self.record("note", f"No goal check: {str(error).rstrip('.')}. The goal stays; /halt drops it.")
            return
        if self.goal != goal or self.closed:
            return  # halted or changed meanwhile
        if response.text.strip().lower().lstrip("*_ ").startswith("yes"):
            self._save_goal(None)
            self.record("note", "Goal met.")
        elif self._nudges >= GOAL_NUDGES:
            self.record("note", f"The goal is not met after {GOAL_NUDGES} more turns, so work stops here. "
                                "Send a message to go on, or /halt to drop it.")
        else:
            self._nudges += 1
            self._enqueue(GOAL_NO).add_done_callback(_consume)

    def _input_blocks(self, item):
        text = item.text
        if item.kind == "work":
            text = "Subagent report (data):\n" + text
        return [text_block(text or "[image attached]")] + [
            image_block(ref["media_type"], self.store.image_bytes(ref)) for ref in item.attachments]

    def _steering(self, delivered):
        content = []
        while self.inbox:
            item = self.inbox.popleft()
            delivered.append(item)
            content.extend(self._input_blocks(item))
        return content

    async def _turn(self, item, delivered):
        cutoff = item.ids[0]
        if self.view.covered < cutoff:
            self.on_event("status", {"phase": "memory", "purpose": "turn", "message": "Preparing memory for this reply"})
        await self.compactor.wait_leaves(cutoff)
        content = view_blocks(self.view.lines(cutoff=cutoff))
        content.append(text_block("Current UTC time: " + utc_now() + self._where("main")))
        content.extend(self._input_blocks(item))
        # Input that arrived while this turn waited was logged after the cutoff
        # and before any reply: answer it in the same request, so no later turn
        # starts from a view that lacks this turn's reply.
        content.extend(self._steering(delivered))
        return await self._conversation([{"role": "user", "content": content}], delivered=delivered)

    def _where(self, caller):
        """Where the computer tools of `caller` work, for the dynamic text after the view."""
        if self.machine is None:
            return ""
        return f"\nComputer: {self.machine.name}; your working folder: {self.machine.folder(caller)}"

    async def _conversation(self, messages, *, delivered=None, **kwargs):
        scope = getattr(self.provider, "foreground", None)
        if delivered is not None and scope is not None:
            async with scope():
                return await self._conversation_steps(messages, delivered=delivered, **kwargs)
        return await self._conversation_steps(messages, delivered=delivered, **kwargs)

    async def _conversation_steps(self, messages, *, delivered=None, log=None, event=None, usage="turns",
                                  caller="main", tools=None, more=None, effort=None):
        """Call the model and run its tools until it answers without tools.

        `more()` gives what an agent was told meanwhile: it follows the next tool
        results, and an answer it arrived after goes on with it."""
        record = log or self.record
        tools = self.tools if tools is None else tools
        emit = event or self.on_event
        answers = []
        rounds = self.config.max_tool_rounds
        for round_number in range(rounds):
            self._check_budget(messages, tools, first=round_number == 0)
            response = await self._call_model(messages, record, emit, usage, tools, effort)
            if response.text:
                answers.append(response.text)
            messages.append({"role": "assistant", "content": response.content})
            calls = response.tool_calls
            if not calls:
                told = more() if more else []
                if not told:
                    return "\n".join(answers)
                messages.append({"role": "user", "content": told})
                continue
            results = [await self._run_tool(call, record, emit, caller) for call in calls]
            if round_number + 1 == rounds:
                break  # queued input stays in the inbox and gets its own turn
            # All tool results must precede new user or report text in this message.
            steering = self._steering(delivered) if delivered is not None else more() if more else []
            messages.append({"role": "user", "content": results + steering})
        raise ValueError(f"Stopped after {rounds} model rounds; all completed work is saved")

    def _check_budget(self, messages, tools, *, first):
        size = request_text_bytes(system_prompt(self.config.instructions, self.config.summary_bytes),
                                  tools, messages)
        limit = self.config.request_bytes
        if size > limit:
            saved = "Input is saved" if first else "All completed work is saved"
            raise ValueError(f"Request is {size:,} bytes, over the {limit:,}-byte text budget. {saved}; "
                             "send smaller requests, or zoom into fewer or smaller pieces.")

    async def _call_model(self, messages, record, emit, usage, tools, effort=None):
        """One streamed model call. Finished blocks are logged as they arrive."""
        logged_text = False
        logged_tools = set()
        pending = []

        def receive(kind, value):
            nonlocal logged_text
            if kind == "text":
                pending.append(value)
                emit(kind, value)
            elif kind == "thinking":
                emit(kind, value)  # shown, never saved
            elif kind == "status":
                emit(kind, value)
            elif kind == "block" and value["type"] == "text":
                if value["text"]:
                    record("unii", value["text"])
                logged_text = True
                pending.clear()
            elif kind == "block" and value["type"] == "tool_use":
                self._log_call(value, record)
                logged_tools.add(value["id"])
            elif kind == "usage":
                self._add_usage(usage, value)
                emit(kind, value)

        # A provider that can think harder for one call says so; others ignore effort.
        extra = {"effort": effort} if effort and getattr(self.provider, "per_call_effort", False) else {}
        try:
            response = await self.provider.complete(
                model=self.config.turn_model,
                system=system_prompt(self.config.instructions, self.config.summary_bytes),
                tools=tools, messages=messages, max_tokens=self.config.max_tokens, on_event=receive,
                purpose="agent" if usage == "agents" else "turn", **extra)
        except BaseException:
            # Never let a cut-off reply read as finished, even when its text
            # blocks completed before the failure (e.g. max_tokens).
            if pending:
                record("unii", "".join(pending) + INTERRUPTED)
            elif logged_text:
                record("unii", INTERRUPTED.strip())
            raise
        if not response.content:
            raise ValueError("Provider returned an empty response")
        # Providers may return whole responses without streaming block events.
        for block in response.content:
            if block["type"] == "text" and block["text"] and not logged_text:
                record("unii", block["text"])
            elif block["type"] == "tool_use" and block["id"] not in logged_tools:
                self._log_call(block, record)
        return response

    @staticmethod
    def _log_call(block, record):
        record("tool", json.dumps({"name": block["name"], "input": block["input"]}, ensure_ascii=False))

    async def _run_tool(self, call, record, emit, caller="main"):
        emit("tool", {"name": call["name"], "input": call["input"]})
        try:
            if call["name"] in BOSS_NAMES and caller == "main":
                output, failed = [text_block(self._boss(call["name"], call["input"]))], False
            elif call["name"] in DEVICE_NAMES and self.machine is not None:
                # Awaited, so a long command never holds up the chat; a stop
                # cancels it, and the machine ends its process group.
                text, failed = await self.machine.run(call["name"], call["input"], caller)
                output = [text_block(clip_output(text))]
            else:
                output = self.tool(call["name"], call["input"])
                failed = False
        except (ValueError, OSError, KeyError, TypeError, RuntimeError) as e:
            output, failed = [text_block(f"Tool error: {e}")], True
        record("echo", "\n".join(b.get("text", "[image returned]") for b in output))
        await asyncio.sleep(0)  # let input typed meanwhile reach the inbox
        return {"type": "tool_result", "tool_use_id": call["id"], "content": output, "is_error": failed}

    # Memory tools ----------------------------------------------------------------

    def tool(self, name, args):
        """Run a model tool call; returns Messages API content blocks.

        Text is clipped exactly as the echo log clips it, so the model and the log agree.
        """
        found = self._tool(name, args)
        return [text_block(clip_output(found.text))] + [
            image_block(ref["media_type"], self.store.image_bytes(ref)) for ref in found.images]

    def _tool(self, name, args):
        if not isinstance(args, dict):
            raise ValueError("Tool input must be an object")
        if name == "date":
            if set(args) != {"id"}:
                raise ValueError("date requires only id")
            return Retrieval(self.date(args["id"]))
        if name != "zoom":
            raise ValueError(f"Unknown tool: {name}")
        if set(args) - {"id", "n", "name", "page"}:
            raise ValueError("Unknown zoom arguments")
        if "name" in args:
            if "id" in args or "n" in args:
                raise ValueError("Choose an agent name or a numeric node")
            return self.zoom(name=args["name"], page=args.get("page", 0))
        return self.zoom(args.get("id"), args.get("n", 1), page=args.get("page", 0))

    def zoom(self, id=None, n=1, *, name=None, page=0):
        """Open summary id+n into its two halves, read page `page` of message id
        (n=1, images on page 0), or page through agent `name`'s transcript."""
        if type(page) is not int or page < 0:
            raise ValueError("page must be a nonnegative integer")
        if name is not None:
            return Retrieval(self._agent_page(name, page))
        key = key_for(id, n)
        start, count = address(key)
        if start + count > self.store.message_count:
            raise ValueError("Node extends beyond the recorded chat")
        if count > 1:
            if page:
                raise ValueError("Summary nodes only have page 0")
            return Retrieval("".join(self.view.line(k) for k in children(key)))
        message = self.store.message(start)
        text = self._page(f"Message {start} ({message['kind']})", message["text"], page)
        return Retrieval(text, tuple(message.get("attachments", [])) if page == 0 else ())

    def date(self, id):
        """The recorded UTC date and time of a message."""
        return self.store.message(id)["date"]

    def _page(self, label, text, page):
        limit = self.config.page_chars
        pages = max(1, (len(text) + limit - 1) // limit)
        if page >= pages:
            raise ValueError(f"Page out of range; there are {pages} pages (starting at 0)")
        body = text[page * limit:(page + 1) * limit]
        return f"{label}; page {page}/{pages-1}\n{body}"

    # Subagents -------------------------------------------------------------------

    def start_agent(self, name, task, *, effort=None):
        """Start a named background job; its report arrives as a work message."""
        self._check_open()
        self.resume_reports()
        self.store.create_agent(name, task)
        return self._run_agent(name, task, task, effort=effort, replies=0)

    def tell(self, name, message):
        """Send agent `name` a message. A running agent reads it between its tool
        calls and its report answers it; a finished one goes on from its
        transcript, and its reply arrives as a work message. Returns what happened."""
        self._check_open()
        if not isinstance(message, str) or not message.strip():
            raise ValueError("tell needs a message")
        state = self.store.agent_state(name)
        if state is None:
            return f"No agent named {name}."
        job = self.agents.get(name)
        if job is not None and not job.done():
            self.agent_inbox[name].append(message)
            return f"Sent. {name} reads it between its tool calls, and its report answers it."
        self._run_agent(name, state.get("task", ""), message, effort=state.get("effort"),
                        replies=state.get("replies", 0) + 1)
        return f"Sent. {name}'s reply will reach you as a message."

    def _run_agent(self, name, task, message, *, effort, replies):
        cutoff = self.store.message_count
        self.agent_inbox.setdefault(name, deque())
        job = asyncio.create_task(self._agent(name, task, message, cutoff, effort, replies))
        self.agents[name] = job
        return job

    async def _agent(self, name, task, message, cutoff, effort, replies):
        # Each delivery has its own event: a reply is never mistaken for the report.
        event_id = "agent:" + name + (f":{replies}" if replies else "")
        inbox = self.agent_inbox[name]
        earlier = self.store.agent_records(name) if replies else []
        state = {"task": task, "effort": effort, "replies": replies}
        with self.store.open_agent_log(name) as transcript:
            transcript.append("user", message)
            if replies:
                self.store.save_agent_state(name, {"status": "running", **state})

            def told():
                blocks = []
                while inbox:
                    text = inbox.popleft()
                    transcript.append("user", text)
                    blocks.append(text_block("Eternal tells you:\n" + text))
                return blocks
            try:
                # Waiting here, inside the job, never stalls the parent chat; the
                # agent sees every message before its request, like a turn would.
                await self.compactor.wait_leaves(cutoff)
                content = view_blocks(self.view.lines(cutoff=cutoff))
                content.append(text_block("Current UTC time: " + utc_now() + self._where(name)))
                if earlier:
                    history = "".join(f"{m['i']}|{m['kind']}: {m['text']}\n" for m in earlier)
                    content.append(text_block("You are a subagent going on with your work. Your transcript so "
                                              "far (i|kind: text):\n" + clip_output(history, HISTORY_CHARS)))
                content.append(text_block(message))
                answer = await self._conversation(
                    [{"role": "user", "content": content}], log=transcript.append, usage="agents", caller=name,
                    tools=self.agent_tools, more=told, effort=effort,
                    event=lambda kind, value: self.on_event("agent_event", {"name": name, "event": kind}))
                status = "complete"
            except asyncio.CancelledError:
                self.store.save_agent_state(name, {"status": "interrupted", **state})
                raise
            except Exception as e:
                answer, status = f"Failed: {e}", "failed"
            # The saved report is the outbox: delivery resumes after a crash.
            self.store.save_agent_state(name, {"status": status, "report": answer, "event": event_id, **state})
        self.on_event("agent_report", {"name": name, "status": status})
        self.submit(f"[{name}] {answer}", kind="work", event_id=event_id).add_done_callback(_consume)

    def _boss(self, name, args):
        """spawn and tell, the chat's tools for its agents."""
        if not isinstance(args, dict):
            raise ValueError("Tool input must be an object")
        if name == "tell":
            if set(args) != {"name", "message"}:
                raise ValueError("tell takes name and message")
            return self.tell(args["name"], args["message"])
        tasks, effort = args.get("tasks"), args.get("effort")
        if set(args) - {"tasks", "effort"} or not isinstance(tasks, list) or not tasks:
            raise ValueError("spawn takes tasks, a list of one or more, and optionally effort")
        if len(tasks) > SPAWN_MAX or not all(isinstance(t, str) and t.strip() for t in tasks):
            raise ValueError(f"spawn takes 1 to {SPAWN_MAX} nonempty tasks")
        if effort is not None and not isinstance(effort, str):
            raise ValueError("effort must be text")
        working = sum(not job.done() for job in self.agents.values())
        if working + len(tasks) > SPAWN_MAX:
            return f"Not started: {working} agents are at work, and at most {SPAWN_MAX} may be. Wait for their reports."
        names = [self._new_agent_name() for _ in tasks]
        for agent, task in zip(names, tasks):
            self.start_agent(agent, task.strip(), effort=effort)
        return (f"Started {', '.join(names)}. Each one's report reaches you as a message "
                f"\"[Name] report\" when it finishes; tell sends one a message.")

    def _new_agent_name(self):
        while True:
            name = random.choice(AGENT_NAMES[0]).title() + random.choice(AGENT_NAMES[1]).title()
            if self.store.agent_state(name) is None and name not in self.agents:
                return name

    def resume_reports(self):
        """Deliver saved agent reports that a crash kept out of the chat. Runs once."""
        if self._reports_resumed or self.readonly:
            return
        self._reports_resumed = True
        for name, report in self.store.agent_reports():
            event_id = (self.store.agent_state(name) or {}).get("event", "agent:" + name)
            if self.store.event_complete(event_id):
                continue  # already durable; never rerun a parent action after restart
            self.submit(f"[{name}] {report}", kind="work", event_id=event_id).add_done_callback(_consume)

    def _agent_status(self, name):
        state = self.store.agent_state(name)
        if state is None:
            raise ValueError(f"Unknown agent: {name}")
        status = state.get("status", "unknown")
        job = self.agents.get(name)
        if status == "running" and (job is None or job.done()):
            status = "interrupted"
        return status, state

    def _agent_page(self, name, page):
        status, _ = self._agent_status(name)
        lines = "".join(f"{m['i']} {m['kind']}: {m['text']}\n" for m in self.store.agent_records(name))
        return self._page(f"Agent {name} ({status})", lines, page)

    def agent_jobs(self):
        """{name: still running} for each agent started in this session, oldest first."""
        return {name: not job.done() for name, job in self.agents.items()}

    def agent_log(self, name):
        """Agent `name`'s status, task and transcript records, oldest first."""
        status, state = self._agent_status(name)
        return {"name": name, "status": status, "task": state.get("task", ""),
                "records": self.store.agent_records(name)}

    # Inspection and lifetime -----------------------------------------------------

    def render_view(self):
        return self.view.render()

    def messages(self, start=0, stop=None):
        """Logged messages start..stop-1 (default: to the end), oldest first."""
        count = self.store.message_count
        stop = count if stop is None else min(stop, count)
        return [self.store.message(i) for i in range(max(0, start), stop)]

    def image(self, ref):
        """The verified bytes of an image that a message or zoom result refers to."""
        return self.store.image_bytes(ref)

    def stop(self):
        """Stop the running reply and drop queued ones. Their input stays logged, and
        a reply cut off mid-stream is saved marked as interrupted."""
        for item in self.inbox:
            item.future.cancel()
        self.inbox.clear()
        if self.turn_task and not self.turn_task.done():
            self.turn_task.cancel()

    def status(self):
        result = {"messages": self.store.message_count, "nodes": self.store.node_count,
                **self.view.stats(), **self.compactor.stats(),
                "replying": bool(self.turn_task and not self.turn_task.done()), "goal": self.goal,
                "agents_running": sum(not job.done() for job in self.agents.values()),
                "usage": {bucket: dict(totals) for bucket, totals in self.usage.items() if totals}}
        if self.provider is not None and hasattr(self.provider, "stats"):
            result["connection"] = self.provider.stats()
        return result

    async def finish(self):
        """Wait for submitted turns, then for every queued summary."""
        while True:
            while self.turn_task and not self.turn_task.done():
                await self.turn_task
            if not self.readonly:
                await self.compactor.drain()
            # An agent may have submitted a report while summaries drained.
            # Finish its accepted turn before closing, including its summaries.
            if self.turn_task is None or self.turn_task.done():
                return

    async def close(self):
        """Cancel running work; logged input stays saved for the next session."""
        self.closed = True
        tasks = [task for task in [*self.agents.values(), self.turn_task] if task and not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.machine is not None:
            self.machine.close()
        for item in self.inbox:
            item.future.cancel()
        self.inbox.clear()
        await self.compactor.close()
        self.store.close()
